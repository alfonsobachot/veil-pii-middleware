"""Reverse-HTTP-proxy gateway: whole-payload tokenization at ``ANTHROPIC_BASE_URL``.

Listens locally and stands in for ``https://api.anthropic.com``. For every
request: reads the full body, and if it is JSON, tokenizes every string leaf
via ``core/tokenizer.py::tokenize()`` against a per-process
``core/vault.py::Vault`` before forwarding it upstream with an
otherwise-untouched method/path/headers (including auth). The response is
relayed back to the client exactly as received -- streaming or not -- with
no rewriting at all.

That last point is a deliberate scope cut, not an oversight: BRIEF §6 flags
that an SSE response can split a ``PII_<TYPE>_<NNNN>`` token across two
chunks, which only matters if this gateway tried to rewrite the response.
Since the architectural requirement ("zero raw PII to an external API")
only constrains the *outbound* leg, this module never parses or rewrites
response bytes, and the split-token risk does not apply to it. A future
mission may add response-side rewriting with a real chunk-boundary-safe
buffer; this one does not attempt it.

Fails closed, matching every other adapter's doctrine: a request body that
is not valid JSON, or a tokenization/vault failure, aborts the request with
an error response of the gateway's own rather than forwarding the original
bytes upstream unscanned.

One ``Vault`` session per running gateway process: ``session_id`` is
generated once at startup and reused by every request this process handles.
Unlike ``PreToolUse``/``PostToolUse`` (a real per-Claude-Code-session id on
every hook invocation) or ``/veil`` (a ``CLAUDE_SESSION_ID`` env var), a
gateway process is a standalone long-running server with no per-request
session identity supplied by the caller -- there is no JSON envelope to read
one from, only a raw HTTP request against the Anthropic Messages API shape.
Treating "one running gateway instance" as "one session" is the simplest
correct choice for the target user (a single developer running one gateway
process); it means two concurrent gateway processes, or a restart mid
conversation, do not share a token vocabulary. That is an accepted scope
limitation, not a bug to fix here.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from veil_pii_middleware.core.tokenizer import JSONValue, tokenize
from veil_pii_middleware.core.vault import Vault

__all__ = ["make_handler", "main"]

#: Same fixed, git-ignored vault location every adapter uses.
VAULT_DB_PATH = Path(".veil") / "vault.db"

#: Overridable only for tests, which stand in a fake upstream in place of
#: the real Anthropic API -- never meant to be pointed anywhere else in
#: normal use.
UPSTREAM_ENV_VAR = "VEIL_GATEWAY_UPSTREAM"
_DEFAULT_UPSTREAM = "https://api.anthropic.com"

_DEFAULT_PORT = 8787
_CHUNK_SIZE = 8192

# Headers that describe *this* hop's framing (connection lifecycle, body
# length/encoding) rather than the payload itself -- recomputed or dropped
# on both the request and response side rather than forwarded verbatim,
# since this gateway re-frames every body it relays (tokenized request,
# rechunked response) instead of passing the original wire bytes through.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}


def _new_session_id() -> str:
    return f"gateway-{uuid.uuid4()}"


# core/vault.py's SQLite-backed counter is read-then-insert with no
# transaction spanning both statements -- fine for every other adapter,
# which only ever has one invocation touching a given session at a time,
# but ThreadingHTTPServer is the first place in this codebase that can run
# two requests for the *same* session concurrently. Without this lock, two
# simultaneous requests minting a token for the same entity type can both
# read the same next_seq and then collide inserting the same token for two
# different values (caught as a generic exception and failed closed, so no
# PII leak -- just an availability/flakiness bug under concurrency). Scoped
# to this process only; does not touch core/vault.py, which this mission's
# non-goals put out of scope.
_vault_lock = threading.Lock()


def _write_error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    # Shaped like an Anthropic API error object so an SDK client on the
    # other end has a recognizable error to surface, rather than an opaque
    # connection failure.
    body = json.dumps(
        {"type": "error", "error": {"type": "veil_gateway_error", "message": message}}
    ).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _tokenize_body(raw_body: bytes, session_id: str) -> bytes:
    """Tokenize a JSON request body. Raises on any failure -- callers must fail closed."""
    parsed = json.loads(raw_body)
    VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _vault_lock, Vault(VAULT_DB_PATH, session_id) as vault:
        tokenized: JSONValue = tokenize(parsed, vault)
    return json.dumps(tokenized).encode()


def make_handler(
    session_id: str, client: httpx.Client, upstream_base: str
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to one session id, HTTP client, and upstream base URL."""

    class GatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            # Tracks whether a status line has already been sent upstream's
            # way to this client. Once true, a failure must never call
            # _write_error -- send_response can only be called once per
            # response; calling it again mid-stream would splice a second
            # HTTP status line/header block into an already-in-flight
            # chunked body and corrupt the framing the client sees. Past
            # that point the only safe move on failure is to stop writing
            # and let the connection close, leaving a truncated body rather
            # than a corrupted one.
            response_started = False
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(length) if length else b""

                forward_body: bytes | None = None
                if raw_body:
                    try:
                        forward_body = _tokenize_body(raw_body, session_id)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        _write_error(
                            self,
                            400,
                            "request body is not valid JSON; blocked rather than forwarded"
                            " unscanned",
                        )
                        return
                    except Exception:
                        _write_error(
                            self,
                            500,
                            "sanitization failed; request blocked rather than forwarded",
                        )
                        return

                forward_headers = {
                    k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP
                }
                if forward_body is not None:
                    forward_headers["Content-Length"] = str(len(forward_body))

                url = upstream_base.rstrip("/") + self.path
                with client.stream(
                    self.command, url, content=forward_body, headers=forward_headers
                ) as upstream:
                    self.send_response(upstream.status_code)
                    for k, v in upstream.headers.items():
                        if k.lower() in _HOP_BY_HOP:
                            continue
                        self.send_header(k, v)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    response_started = True
                    for chunk in upstream.iter_bytes(_CHUNK_SIZE):
                        if not chunk:
                            continue
                        self.wfile.write(f"{len(chunk):X}\r\n".encode())
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
            except Exception:
                if not response_started:
                    _write_error(self, 502, "upstream request failed")
                # else: the status line and possibly some chunks are
                # already on the wire -- nothing left to do but stop.

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def do_PUT(self) -> None:
            self._handle()

        def do_DELETE(self) -> None:
            self._handle()

        def log_message(self, format: str, *args: object) -> None:
            pass

    return GatewayHandler


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    port = int(argv[0]) if argv else _DEFAULT_PORT
    upstream = os.environ.get(UPSTREAM_ENV_VAR, _DEFAULT_UPSTREAM)
    session_id = _new_session_id()

    client = httpx.Client(timeout=httpx.Timeout(300.0))
    handler_cls = make_handler(session_id, client, upstream)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    print(
        f"veil gateway: listening on http://127.0.0.1:{port} -> {upstream} (session {session_id})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
