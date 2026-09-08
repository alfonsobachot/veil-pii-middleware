"""Tests for veil_pii_middleware.adapters.gateway.server.

Runs the real gateway handler in-process against a real (fake) upstream
HTTP server on localhost -- no mocked internals -- so these exercise the
actual request/response bytes that would cross the wire, mirroring the
"never assume, verify the real behavior" approach the hooks/command
adapters' tests already use.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from veil_pii_middleware.adapters.gateway import server as gateway_server
from veil_pii_middleware.core.vault import Vault
from veil_pii_middleware.demo import eval as eval_module
from veil_pii_middleware.demo.eval import Metrics, Report

_TEST_KEY_B64 = base64.b64encode(os.urandom(32)).decode()
VALID_EMAIL = "alice@example.com"

#: Hook-shaped test session id (deliberately not `gateway-<uuid>`-shaped) --
#: mission-9's fix reconciles the gateway's vault keying with exactly this
#: kind of id, the same one `pre_tool_use.py`/`post_tool_use.py` read from a
#: real hook payload's `session_id`.
HOOK_SESSION_ID = "hooks-11111111-1111-1111-1111-111111111111"


def _post(
    url: str,
    *,
    json: Any = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
    session_id: str | None = HOOK_SESSION_ID,
) -> httpx.Response:
    """``httpx.post`` that attaches ``X-Claude-Code-Session-Id`` by default.

    Real Claude Code traffic always carries this header (mission-7's live
    spike); defaulting it here keeps every test in this file representative
    of that, without individually editing each call site. Pass
    ``session_id=None`` to test the header-missing fallback path.
    """
    merged_headers = dict(headers or {})
    if session_id is not None:
        merged_headers.setdefault(gateway_server.SESSION_ID_HEADER, session_id)
    return httpx.post(
        url, json=json, content=content, headers=merged_headers or None, timeout=timeout
    )


def _get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
    session_id: str | None = HOOK_SESSION_ID,
) -> httpx.Response:
    merged_headers = dict(headers or {})
    if session_id is not None:
        merged_headers.setdefault(gateway_server.SESSION_ID_HEADER, session_id)
    return httpx.get(url, headers=merged_headers or None, timeout=timeout)


def _sse_body(events: list[dict[str, Any]]) -> bytes:
    """Build a raw SSE response body from a list of Anthropic-shaped events."""
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


def _extract_text_deltas(sse_text: str, index: int = 0) -> str:
    """Reconstruct one content block's accumulated text the way a real client would.

    Concatenates every ``content_block_delta`` text-bearing field for the
    given ``index``, in order -- the only correct way to check a value that
    may have been split across two separate delta events by the rewrite.
    """
    out = []
    for raw_event in sse_text.split("\n\n"):
        for line in raw_event.split("\n"):
            if not line.startswith("data:"):
                continue
            obj = json.loads(line[len("data:") :].strip())
            if obj.get("type") != "content_block_delta" or obj.get("index") != index:
                continue
            delta = obj.get("delta", {})
            out.append(
                delta.get("text") or delta.get("thinking") or delta.get("partial_json") or ""
            )
    return "".join(out)


class _RecordingUpstream:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response_status = 200
        self.response_headers: dict[str, str] = {"Content-Type": "application/json"}
        self.response_body = b'{"ok": true}'
        #: When set, the handler declares a Content-Length larger than what
        #: it actually writes, then drops the connection -- simulating a
        #: real upstream dying mid-stream, after the gateway has already
        #: committed a status line to its own client.
        self.truncate_response = False


def _make_upstream_handler(state: _RecordingUpstream) -> type[BaseHTTPRequestHandler]:
    class UpstreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            state.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
            )
            self.send_response(state.response_status)
            for k, v in state.response_headers.items():
                self.send_header(k, v)
            if state.truncate_response:
                # Promise more bytes than we send, then vanish -- forces
                # the client (httpx, inside the gateway) to see a
                # mid-stream failure after headers were already relayed.
                self.send_header("Content-Length", str(len(state.response_body) + 10_000))
                self.end_headers()
                self.wfile.write(state.response_body)
                self.close_connection = True
                return
            self.send_header("Content-Length", str(len(state.response_body)))
            self.end_headers()
            self.wfile.write(state.response_body)

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def log_message(self, format: str, *args: object) -> None:
            pass

    return UpstreamHandler


class _Servers:
    def __init__(self, upstream_state: _RecordingUpstream, gateway_url: str) -> None:
        self.upstream_state = upstream_state
        self.gateway_url = gateway_url


@pytest.fixture
def servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Servers]:
    monkeypatch.setenv("VEIL_VAULT_KEY", _TEST_KEY_B64)
    monkeypatch.setattr(gateway_server, "VAULT_DB_PATH", tmp_path / ".veil" / "vault.db")
    monkeypatch.setattr(gateway_server, "AUDIT_LOG_PATH", tmp_path / ".veil" / "audit.jsonl")

    upstream_state = _RecordingUpstream()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _make_upstream_handler(upstream_state))
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    upstream_base = f"http://127.0.0.1:{upstream.server_address[1]}"

    client = httpx.Client(timeout=httpx.Timeout(10.0))
    fallback_session_id = gateway_server._new_session_id()
    handler_cls = gateway_server.make_handler(fallback_session_id, client, upstream_base)
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    gateway_url = f"http://127.0.0.1:{gateway.server_address[1]}"

    try:
        yield _Servers(upstream_state, gateway_url)
    finally:
        gateway.shutdown()
        upstream.shutdown()
        client.close()


class TestRequestTokenization:
    def test_pii_in_body_reaches_upstream_as_a_token(self, servers: _Servers) -> None:
        body = {
            "model": "claude-x",
            "messages": [{"role": "user", "content": "email me at alice@example.com"}],
        }
        resp = _post(f"{servers.gateway_url}/v1/messages?beta=true", json=body)
        assert resp.status_code == 200

        assert len(servers.upstream_state.requests) == 1
        upstream_req = servers.upstream_state.requests[0]
        assert upstream_req["path"] == "/v1/messages?beta=true"
        forwarded_body = json.loads(upstream_req["body"])
        content = forwarded_body["messages"][0]["content"]
        assert "alice@example.com" not in content
        assert "PII_EMAIL_0001" in content

    def test_same_value_gets_same_token_within_one_process(self, servers: _Servers) -> None:
        body = {"messages": [{"role": "user", "content": "bob@example.com"}]}
        _post(f"{servers.gateway_url}/v1/messages", json=body)
        _post(f"{servers.gateway_url}/v1/messages", json=body)

        assert len(servers.upstream_state.requests) == 2
        first = json.loads(servers.upstream_state.requests[0]["body"])
        second = json.loads(servers.upstream_state.requests[1]["body"])
        assert first == second
        assert "PII_EMAIL_0001" in first["messages"][0]["content"]

    def test_accept_encoding_forced_to_a_decodable_value(self, servers: _Servers) -> None:
        """Found via a live test against the real Anthropic API: it replies

        Brotli-compressed (`content-encoding: br`) when the client's own
        Accept-Encoding offers it, and httpx cannot decode Brotli without an
        optional package this project does not install -- the gateway then
        relayed still-compressed bytes to the client as if they were plain
        text. The upstream request must always ask for something httpx's
        stdlib-only decoders can handle, regardless of what the real client
        (which may support Brotli itself) sent.
        """
        resp = _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": []},
            headers={"Accept-Encoding": "br, gzip, deflate"},
        )
        assert resp.status_code == 200
        upstream_req = servers.upstream_state.requests[0]
        assert upstream_req["headers"]["accept-encoding"] == "gzip, deflate"

    def test_auth_header_forwarded_unchanged(self, servers: _Servers) -> None:
        resp = _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": []},
            headers={"Authorization": "Bearer test-secret-token", "x-api-key": "test-key"},
        )
        assert resp.status_code == 200
        upstream_req = servers.upstream_state.requests[0]
        assert upstream_req["headers"]["authorization"] == "Bearer test-secret-token"
        assert upstream_req["headers"]["x-api-key"] == "test-key"

    def test_get_with_no_body_passes_through_without_touching_the_vault(
        self, servers: _Servers, tmp_path: Path
    ) -> None:
        resp = _get(f"{servers.gateway_url}/v1/models")
        assert resp.status_code == 200
        assert servers.upstream_state.requests[0]["body"] == b""
        assert not (tmp_path / ".veil" / "vault.db").exists()


class TestResponseWithNoRecognizedTokenStructure:
    """Content with no ``PII_`` substring (non-JSON) or that never touched the
    vault at all is relayed exactly as mission-7 always did -- this mission
    only changes behavior for content that actually carries a token.
    """

    def test_large_response_relayed_byte_for_byte(self, servers: _Servers) -> None:
        big = ("x" * 50_000).encode()
        servers.upstream_state.response_body = big
        servers.upstream_state.response_headers = {"Content-Type": "text/plain"}
        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})
        assert resp.status_code == 200
        assert resp.content == big

    def test_upstream_status_code_is_preserved(self, servers: _Servers) -> None:
        servers.upstream_state.response_status = 429
        servers.upstream_state.response_body = b'{"type":"error"}'
        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})
        assert resp.status_code == 429

    def test_get_with_json_response_but_no_pii_never_touches_the_vault(
        self, servers: _Servers, tmp_path: Path
    ) -> None:
        resp = _get(f"{servers.gateway_url}/v1/models")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert not (tmp_path / ".veil" / "vault.db").exists()


class TestResponseSideDetokenization:
    """Mission-8: a token the model echoes back must resolve to its real
    value before it reaches the client, in both response shapes.
    """

    def _mint_email_token(self, servers: _Servers) -> str:
        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
        )
        forwarded = json.loads(servers.upstream_state.requests[-1]["body"])
        token = str(forwarded["messages"][0]["content"]).split()[-1]
        assert token == "PII_EMAIL_0001"
        return token

    def test_buffered_json_response_resolves_a_previously_minted_token(
        self, servers: _Servers
    ) -> None:
        token = self._mint_email_token(servers)
        servers.upstream_state.response_body = json.dumps(
            {"content": [{"type": "text", "text": f"on file: {token}"}]}
        ).encode()

        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})

        assert resp.status_code == 200
        assert resp.json()["content"][0]["text"] == f"on file: {VALID_EMAIL}"

    def test_buffered_json_response_with_unminted_token_gets_placeholder(
        self, servers: _Servers
    ) -> None:
        servers.upstream_state.response_body = json.dumps(
            {"content": [{"type": "text", "text": "your token is PII_EMAIL_0001"}]}
        ).encode()

        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})

        assert resp.status_code == 200
        assert resp.json()["content"][0]["text"] == "your token is [unresolved token]"

    def test_streaming_response_resolves_a_previously_minted_token(self, servers: _Servers) -> None:
        token = self._mint_email_token(servers)
        servers.upstream_state.response_headers = {
            "Content-Type": "text/event-stream; charset=utf-8"
        }
        servers.upstream_state.response_body = _sse_body(
            [
                {"type": "message_start"},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": f"on file: {token}"},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_stop"},
            ]
        )

        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})

        assert resp.status_code == 200
        assert _extract_text_deltas(resp.text) == f"on file: {VALID_EMAIL}"
        assert token not in resp.text

    def test_streaming_response_resolves_a_token_split_across_two_delta_events(
        self, servers: _Servers
    ) -> None:
        token = self._mint_email_token(servers)
        split_at = len(token) // 2
        servers.upstream_state.response_headers = {
            "Content-Type": "text/event-stream; charset=utf-8"
        }
        servers.upstream_state.response_body = _sse_body(
            [
                {"type": "message_start"},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": f"on file: {token[:split_at]}"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": token[split_at:]},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_stop"},
            ]
        )

        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})

        assert resp.status_code == 200
        assert _extract_text_deltas(resp.text) == f"on file: {VALID_EMAIL}"
        assert token not in resp.text
        # The first half alone must never appear as a dangling fragment --
        # proof the gateway actually held it back rather than flushing early.
        assert token[:split_at] not in resp.text

    def test_streaming_response_with_unresolved_token_gets_placeholder(
        self, servers: _Servers
    ) -> None:
        servers.upstream_state.response_headers = {
            "Content-Type": "text/event-stream; charset=utf-8"
        }
        servers.upstream_state.response_body = _sse_body(
            [
                {"type": "message_start"},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "your token is PII_EMAIL_0001"},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_stop"},
            ]
        )

        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})

        assert resp.status_code == 200
        assert _extract_text_deltas(resp.text) == "your token is [unresolved token]"

    def test_streaming_response_with_unresolved_token_has_valid_chunk_framing(
        self, servers: _Servers
    ) -> None:
        """The unresolved-token path must never corrupt the chunked response --
        exactly one status line, and the stream ends with a proper terminator.
        """
        servers.upstream_state.response_headers = {
            "Content-Type": "text/event-stream; charset=utf-8"
        }
        servers.upstream_state.response_body = _sse_body(
            [
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "your token is PII_EMAIL_0001"},
                },
                {"type": "content_block_stop", "index": 0},
            ]
        )
        host_port = urlsplit(servers.gateway_url)
        request = (
            (b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n")
            + f"X-Claude-Code-Session-Id: {HOOK_SESSION_ID}\r\n".encode()
            + (b'Content-Length: 16\r\nConnection: close\r\n\r\n{"messages": []}')
        )
        with socket.create_connection((host_port.hostname, host_port.port), timeout=10) as sock:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        raw_response = b"".join(chunks)

        assert raw_response.count(b"HTTP/1.1") == 1
        assert raw_response.endswith(b"0\r\n\r\n")
        assert b"PII_EMAIL_0001" not in raw_response
        assert b"[unresolved token]" in raw_response

    def test_streaming_response_with_no_tokens_is_unaffected(self, servers: _Servers) -> None:
        servers.upstream_state.response_headers = {
            "Content-Type": "text/event-stream; charset=utf-8"
        }
        servers.upstream_state.response_body = _sse_body(
            [
                {"type": "message_start"},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "nothing sensitive here"},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_stop"},
            ]
        )

        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})

        assert resp.status_code == 200
        assert _extract_text_deltas(resp.text) == "nothing sensitive here"


class TestSessionIdReconciliation:
    """Mission-9 (BRIEF item 10c): a token minted by the gateway under
    ``X-Claude-Code-Session-Id: <sid>`` must be resolvable by anyone who
    opens a ``Vault`` with that same ``sid`` -- the same call shape
    ``pre_tool_use.py`` uses from a hook payload's ``session_id`` -- proving
    real cross-adapter reconciliation, not just that the gateway reads the
    header.
    """

    def test_gateway_minted_token_resolves_via_independent_vault_with_header_session_id(
        self, servers: _Servers, tmp_path: Path
    ) -> None:
        resp = _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
            session_id=HOOK_SESSION_ID,
        )
        assert resp.status_code == 200
        forwarded = json.loads(servers.upstream_state.requests[-1]["body"])
        token = str(forwarded["messages"][0]["content"]).split()[-1]
        assert token == "PII_EMAIL_0001"

        # Independent Vault instance, opened the same way pre_tool_use.py
        # opens one from a hook payload's session_id -- not the gateway's
        # own in-process Vault object.
        with Vault(tmp_path / ".veil" / "vault.db", HOOK_SESSION_ID) as vault:
            assert vault.resolve(token) == VALID_EMAIL

    def test_two_different_header_session_ids_mint_independently_keyed_tokens(
        self, servers: _Servers, tmp_path: Path
    ) -> None:
        other_session_id = "hooks-22222222-2222-2222-2222-222222222222"
        other_email = "carol@example.com"

        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
            session_id=HOOK_SESSION_ID,
        )
        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {other_email}"}]},
            session_id=other_session_id,
        )

        first = json.loads(servers.upstream_state.requests[-2]["body"])
        second = json.loads(servers.upstream_state.requests[-1]["body"])
        first_token = str(first["messages"][0]["content"]).split()[-1]
        second_token = str(second["messages"][0]["content"]).split()[-1]
        # Each session starts its own per-entity-type counter at 1
        # (core/vault.py) -- same token string, two different sessions, two
        # different real values behind it.
        assert first_token == second_token == "PII_EMAIL_0001"

        db_path = tmp_path / ".veil" / "vault.db"
        with Vault(db_path, HOOK_SESSION_ID) as vault:
            assert vault.resolve(first_token) == VALID_EMAIL
        with Vault(db_path, other_session_id) as vault:
            assert vault.resolve(second_token) == other_email


class TestSessionIdFallback:
    """Mission-9: a request with no usable ``X-Claude-Code-Session-Id``
    header must still succeed (mission-7/8's original behavior, unchanged),
    falling back to the gateway's own per-process UUID rather than blocking
    -- universality of this header across every ``ANTHROPIC_BASE_URL``
    client, not just Claude Code, is unconfirmed, and no PII-leak risk is at
    stake either way.
    """

    def test_missing_header_falls_back_and_still_succeeds(self, servers: _Servers) -> None:
        resp = _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
            session_id=None,
        )
        assert resp.status_code == 200
        forwarded = json.loads(servers.upstream_state.requests[-1]["body"])
        assert "PII_EMAIL_0001" in forwarded["messages"][0]["content"]

    def test_empty_header_falls_back_same_as_missing(self, servers: _Servers) -> None:
        # A pure-whitespace header value can't be produced via a spec-compliant
        # HTTP client (h11 rejects leading/trailing OWS in a header value before
        # it's even sent), so an empty string is the realistic "present but
        # unusable" case reachable over the wire. `_resolve_session_id`'s own
        # `.strip()` check is defense in depth for the unreachable-via-HTTP case.
        resp = httpx.post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
            headers={gateway_server.SESSION_ID_HEADER: ""},
            timeout=10,
        )
        assert resp.status_code == 200
        forwarded = json.loads(servers.upstream_state.requests[-1]["body"])
        assert "PII_EMAIL_0001" in forwarded["messages"][0]["content"]

    def test_fallback_warning_contains_no_pii_or_secret(
        self, servers: _Servers, capfd: pytest.CaptureFixture[str]
    ) -> None:
        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
            session_id=None,
        )
        out, err = capfd.readouterr()
        assert VALID_EMAIL not in out
        assert VALID_EMAIL not in err
        assert _TEST_KEY_B64 not in out
        assert _TEST_KEY_B64 not in err
        assert gateway_server.SESSION_ID_HEADER in err


class TestMidStreamUpstreamFailure:
    def test_no_second_status_line_when_upstream_dies_mid_stream(self, servers: _Servers) -> None:
        """Regression test for a fixed bug: a mid-stream upstream failure used to
        trigger a second ``send_response``, splicing a fresh status line/header
        block into the already-in-flight chunked body. Asserts directly on the
        raw wire bytes -- the only way to see that corruption -- rather than
        going through a client library that would just raise on the malformed
        response either way.
        """
        servers.upstream_state.truncate_response = True
        host_port = urlsplit(servers.gateway_url)
        request = (
            (b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n")
            + f"X-Claude-Code-Session-Id: {HOOK_SESSION_ID}\r\n".encode()
            + (b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")
        )
        with socket.create_connection((host_port.hostname, host_port.port), timeout=10) as sock:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        raw_response = b"".join(chunks)

        assert raw_response.count(b"HTTP/1.1") == 1

    def test_gateway_keeps_serving_after_a_mid_stream_upstream_failure(
        self, servers: _Servers
    ) -> None:
        servers.upstream_state.truncate_response = True
        with contextlib.suppress(httpx.HTTPError):
            _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})

        servers.upstream_state.truncate_response = False
        servers.upstream_state.response_body = b'{"ok": true}'
        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})
        assert resp.status_code == 200


class TestSkipReservedAnthropicMetadataPredicate:
    def test_matches_the_reserved_prefix(self) -> None:
        assert gateway_server._skip_reserved_anthropic_metadata(
            "x-anthropic-billing-header: cc_version=1.0.0; cc_entrypoint=sdk-cli;"
        )

    def test_does_not_match_ordinary_text(self) -> None:
        assert not gateway_server._skip_reserved_anthropic_metadata(
            "please don't share my card 4111111111111111"
        )


class TestReservedAnthropicMetadataExclusion:
    """Claude Code injects a reserved system-prompt block on every request

    ("x-anthropic-billing-header: cc_version=...; cc_entrypoint=...;"). Found
    live: the LLM layer mistook its "cc_" prefix for a credit-card indicator
    and tokenized it, and the real Anthropic API rejects the whole request
    if that block's value is altered at all. The gateway must exclude it
    from detection entirely while still scanning everything else normally.
    """

    def test_reserved_system_block_passes_through_even_with_card_shaped_content(
        self, servers: _Servers
    ) -> None:
        body = {
            "system": [
                {
                    "type": "text",
                    "text": "x-anthropic-billing-header: cc_version=2.1.263.325; 4111111111111111",
                }
            ],
            "messages": [{"role": "user", "content": "nothing sensitive here"}],
        }

        resp = _post(f"{servers.gateway_url}/v1/messages", json=body)

        assert resp.status_code == 200
        forwarded = json.loads(servers.upstream_state.requests[0]["body"])
        assert forwarded["system"][0]["text"] == body["system"][0]["text"]
        assert "PII_CARD_" not in forwarded["system"][0]["text"]

    def test_other_content_is_still_tokenized_normally(self, servers: _Servers) -> None:
        body = {
            "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=1.0.0;"}],
            "messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}],
        }

        resp = _post(f"{servers.gateway_url}/v1/messages", json=body)

        assert resp.status_code == 200
        forwarded = json.loads(servers.upstream_state.requests[0]["body"])
        assert forwarded["system"][0]["text"] == body["system"][0]["text"]
        assert VALID_EMAIL not in forwarded["messages"][0]["content"]
        assert "PII_EMAIL_" in forwarded["messages"][0]["content"]


class TestFailsClosed:
    def test_malformed_json_body_is_blocked_not_forwarded(self, servers: _Servers) -> None:
        resp = _post(
            f"{servers.gateway_url}/v1/messages",
            content=b"{not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert servers.upstream_state.requests == []

    def test_vault_failure_blocks_the_request_entirely(
        self, servers: _Servers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VEIL_VAULT_KEY", raising=False)
        body = {"messages": [{"role": "user", "content": "carol@example.com"}]}
        resp = _post(f"{servers.gateway_url}/v1/messages", json=body)
        assert resp.status_code == 500
        assert servers.upstream_state.requests == []

    def test_no_secret_or_pii_appears_in_gateway_stderr(
        self, servers: _Servers, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("VEIL_VAULT_KEY", raising=False)
        body = {"messages": [{"role": "user", "content": "dave@example.com"}]}
        _post(f"{servers.gateway_url}/v1/messages", json=body)
        out, err = capfd.readouterr()
        assert "dave@example.com" not in out
        assert "dave@example.com" not in err
        assert _TEST_KEY_B64 not in out
        assert _TEST_KEY_B64 not in err


def _read_one_sse_chunk(url: str, *, timeout: float = 5.0) -> str:
    """Block until one non-empty chunk of an SSE response arrives, then return it."""
    with httpx.stream("GET", url, timeout=timeout) as response:
        for chunk in response.iter_text():
            if chunk.strip():
                return chunk
    raise AssertionError(f"no SSE data received from {url} within {timeout}s")


class TestPanelDoesNotShadowTheProxy:
    def test_non_panel_path_still_proxies(self, servers: _Servers) -> None:
        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})
        assert resp.status_code == 200
        assert len(servers.upstream_state.requests) == 1

    def test_path_merely_containing_veil_panel_as_a_substring_still_proxies(
        self, servers: _Servers
    ) -> None:
        resp = _get(f"{servers.gateway_url}/v1/not-veil-panel-at-all")
        assert resp.status_code == 200
        assert servers.upstream_state.requests[0]["path"] == "/v1/not-veil-panel-at-all"

    def test_wrong_verb_on_a_panel_route_404s_rather_than_proxying(self, servers: _Servers) -> None:
        # GET on a POST-only panel route -- must never fall through to the
        # real upstream with a nonsensical path.
        resp = _get(f"{servers.gateway_url}/veil-panel/vault/reveal")
        assert resp.status_code == 404
        assert servers.upstream_state.requests == []

    def test_unknown_panel_sub_path_404s_rather_than_proxying(self, servers: _Servers) -> None:
        resp = _get(f"{servers.gateway_url}/veil-panel/does-not-exist")
        assert resp.status_code == 404
        assert servers.upstream_state.requests == []

    def test_favicon_request_never_reaches_the_upstream(self, servers: _Servers) -> None:
        """A browser tab open on the panel fetches /favicon.ico automatically.

        Found during manual panel verification: without this, the favicon
        request fell through to the proxy and was silently forwarded to the
        real upstream API, polluting the pipeline-stream with an unrelated
        response that looked like noise in the demo.
        """
        resp = _get(f"{servers.gateway_url}/favicon.ico")
        assert resp.status_code == 204
        assert servers.upstream_state.requests == []


class TestPanelStaticPage:
    def test_serves_html_with_no_build_step(self, servers: _Servers) -> None:
        resp = _get(f"{servers.gateway_url}/veil-panel")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        body = resp.text
        assert "react" in body.lower()
        assert "babel" in body.lower()
        assert "node_modules" not in body
        assert "package.json" not in body


class TestAuditStream:
    def test_emits_a_new_entry_as_it_is_appended(self, servers: _Servers) -> None:
        results: list[str] = []

        def _listen() -> None:
            results.append(_read_one_sse_chunk(f"{servers.gateway_url}/veil-panel/audit-stream"))

        listener = threading.Thread(target=_listen, daemon=True)
        listener.start()
        time.sleep(0.3)  # let the tail seek to EOF before the write happens

        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
        )

        listener.join(timeout=5)
        assert results, "expected one SSE chunk from the audit stream"
        assert "PII_EMAIL_" in results[0]
        assert "gateway" in results[0]
        assert VALID_EMAIL not in results[0]


def _read_sse_chunks_until_idle(
    url: str, *, idle_timeout: float = 1.5, overall_timeout: float = 8.0
) -> list[str]:
    """Collect SSE chunks until the stream goes quiet for ``idle_timeout``.

    Used where a single request is expected to publish *several* events in
    quick succession (the pipeline-stage stream) and the test needs all of
    them, not just the first -- `httpx.ReadTimeout` after a quiet stretch is
    the natural "no more events coming soon" signal here, not a failure.
    """
    chunks: list[str] = []
    deadline = time.monotonic() + overall_timeout
    with httpx.stream("GET", url, timeout=idle_timeout) as response:
        try:
            for chunk in response.iter_text():
                if chunk.strip():
                    chunks.append(chunk)
                if time.monotonic() > deadline:
                    break
        except httpx.ReadTimeout:
            pass
    return chunks


class TestPipelineStream:
    def test_emits_every_documented_stage_never_persisted_to_the_audit_log(
        self, servers: _Servers, tmp_path: Path
    ) -> None:
        results: list[list[str]] = []

        def _listen() -> None:
            results.append(
                _read_sse_chunks_until_idle(f"{servers.gateway_url}/veil-panel/pipeline-stream")
            )

        listener = threading.Thread(target=_listen, daemon=True)
        listener.start()
        time.sleep(0.3)  # let the stream subscribe before the write happens

        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
        )

        listener.join(timeout=10)
        assert results and results[0], "expected SSE chunks from the pipeline stream"
        combined = "".join(results[0])
        assert '"request_id"' in combined
        for stage in gateway_server.PIPELINE_STAGES:
            assert f'"stage": "{stage}"' in combined, f"missing stage {stage!r}: {combined!r}"

        audit_path = tmp_path / ".veil" / "audit.jsonl"
        if audit_path.exists():
            assert "pipeline" not in audit_path.read_text(encoding="utf-8")
            assert "stage" not in audit_path.read_text(encoding="utf-8")


class TestVaultInspectorEndpoints:
    def _mint_email_token(self, servers: _Servers) -> str:
        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
        )
        forwarded = json.loads(servers.upstream_state.requests[-1]["body"])
        token = str(forwarded["messages"][0]["content"]).split()[-1]
        assert token == "PII_EMAIL_0001"
        return token

    def test_list_never_includes_a_decrypted_value(self, servers: _Servers) -> None:
        self._mint_email_token(servers)

        resp = _get(f"{servers.gateway_url}/veil-panel/vault")

        assert resp.status_code == 200
        data = resp.json()
        assert VALID_EMAIL not in json.dumps(data)
        mapping = next(m for m in data["mappings"] if m["token"] == "PII_EMAIL_0001")
        assert mapping["entity_type"] == "EMAIL"
        assert "ciphertext_b64" in mapping
        assert "created_at" in mapping

    def test_reveal_returns_the_value_for_the_right_token(self, servers: _Servers) -> None:
        token = self._mint_email_token(servers)

        # No session header needed: the panel defaults to the most-recently-
        # seen real session, already set by `_mint_email_token`'s own POST.
        resp = httpx.post(
            f"{servers.gateway_url}/veil-panel/vault/reveal",
            json={"token": token},
            timeout=10,
        )

        assert resp.status_code == 200
        assert resp.json()["value"] == VALID_EMAIL

    def test_reveal_logs_a_forensic_trail_without_the_value(
        self, servers: _Servers, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """The one endpoint whose job is decrypting PII on request should at
        least leave a record of when/which token, even with no auth layer.
        """
        token = self._mint_email_token(servers)
        capfd.readouterr()  # discard output from minting itself

        httpx.post(
            f"{servers.gateway_url}/veil-panel/vault/reveal", json={"token": token}, timeout=10
        )

        out, err = capfd.readouterr()
        assert token in err
        assert VALID_EMAIL not in err
        assert VALID_EMAIL not in out

    def test_reveal_rejects_an_unknown_token(self, servers: _Servers) -> None:
        self._mint_email_token(servers)

        resp = httpx.post(
            f"{servers.gateway_url}/veil-panel/vault/reveal",
            json={"token": "PII_EMAIL_9999"},
            timeout=10,
        )

        assert resp.status_code == 404
        assert "value" not in resp.json()

    def test_reveal_rejects_a_malformed_body(self, servers: _Servers) -> None:
        resp = httpx.post(
            f"{servers.gateway_url}/veil-panel/vault/reveal",
            content=b"not json",
            timeout=10,
        )
        assert resp.status_code == 400


class TestMetricsEndpoint:
    def test_reports_unavailable_when_demo_data_missing(
        self, servers: _Servers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*args: Any, **kwargs: Any) -> Report:
            raise FileNotFoundError("no manifest.json")

        monkeypatch.setattr(eval_module, "evaluate", _raise)

        resp = _get(f"{servers.gateway_url}/veil-panel/metrics")

        assert resp.status_code == 200
        assert resp.json()["available"] is False

    def test_reports_recall_and_precision_when_available(
        self, servers: _Servers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_report = Report(
            by_type={"EMAIL": Metrics(support=2, true_positives=2, false_positives=0)},
            by_detector={},
            misses=[],
        )
        monkeypatch.setattr(eval_module, "evaluate", lambda *a, **k: fake_report)

        resp = _get(f"{servers.gateway_url}/veil-panel/metrics")

        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["by_type"]["EMAIL"]["recall"] == 1.0
        assert data["by_type"]["EMAIL"]["precision"] == 1.0
