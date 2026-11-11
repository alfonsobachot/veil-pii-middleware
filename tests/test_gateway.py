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
import queue
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
from veil_pii_middleware.demo.eval import Metrics, Miss, Report

_TEST_KEY_B64 = base64.b64encode(os.urandom(32)).decode()
#: serve_forever() checks for shutdown at this interval; the 0.5 s default made
#: every server fixture take half a second to tear down.
_POLL_INTERVAL = 0.02
VALID_EMAIL = "alice@example.com"

#: Hook-shaped test session id (deliberately not `gateway-<uuid>`-shaped) --
#: the session-id fix reconciles the gateway's vault keying with exactly this
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

    Real Claude Code traffic always carries this header (confirmed in live
    testing); defaulting it here keeps every test in this file representative
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


def _parse_sse_events(combined: str) -> list[dict[str, Any]]:
    """Parse every ``data: {...}`` line out of a combined SSE text blob."""
    events: list[dict[str, Any]] = []
    for line in combined.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


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
    monkeypatch.setattr(
        gateway_server, "METRICS_COMPARISON_PATH", tmp_path / ".veil" / "metrics_comparison.json"
    )

    upstream_state = _RecordingUpstream()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _make_upstream_handler(upstream_state))
    upstream_thread = threading.Thread(
        target=upstream.serve_forever, kwargs={"poll_interval": _POLL_INTERVAL}, daemon=True
    )
    upstream_thread.start()
    upstream_base = f"http://127.0.0.1:{upstream.server_address[1]}"

    client = httpx.Client(timeout=httpx.Timeout(10.0))
    fallback_session_id = gateway_server._new_session_id()
    handler_cls = gateway_server.make_handler(fallback_session_id, client, upstream_base)
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    gateway_thread = threading.Thread(
        target=gateway.serve_forever, kwargs={"poll_interval": _POLL_INTERVAL}, daemon=True
    )
    gateway_thread.start()
    gateway_url = f"http://127.0.0.1:{gateway.server_address[1]}"

    try:
        yield _Servers(upstream_state, gateway_url)
    finally:
        gateway.shutdown()
        gateway.server_close()
        upstream.shutdown()
        upstream.server_close()
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
    vault at all is relayed exactly as the original gateway always did --
    detokenization only changes behavior for content that actually carries a token.
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
    """A token the model echoes back must resolve to its real
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
    """A token minted by the gateway under
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
    """A request with no usable ``X-Claude-Code-Session-Id``
    header must still succeed (the original behavior, unchanged),
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
        # 400, not 500: Claude Code retries 5xx, re-running the scan each time.
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert error["type"] == "invalid_request_error"
        assert error["message"].startswith("veil blocked")
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


def _run_request_and_collect_pipeline_events(
    servers: _Servers, body: dict[str, Any]
) -> list[dict[str, Any]]:
    """POST ``body`` through the gateway, returning *this* request's own pipeline-stage events.

    The pipeline ring buffer means a subscriber connecting now may also
    receive backlog from earlier, unrelated cycles this same test already
    ran (e.g. to mint a token first) -- so this filters down to the events
    sharing the request id of the very last event captured, which is always
    this call's own cycle: the listener only goes idle, and this function
    only returns, once every one of this request's stages (published right
    up through the gateway finishing the response) has already arrived.
    """
    results: list[list[str]] = []

    def _listen() -> None:
        results.append(
            _read_sse_chunks_until_idle(f"{servers.gateway_url}/veil-panel/pipeline-stream")
        )

    listener = threading.Thread(target=_listen, daemon=True)
    listener.start()
    time.sleep(0.3)  # let the stream subscribe before the write happens

    _post(f"{servers.gateway_url}/v1/messages", json=body)

    listener.join(timeout=10)
    assert results and results[0], "expected SSE chunks from the pipeline stream"
    events = _parse_sse_events("".join(results[0]))
    own_request_id = events[-1]["request_id"]
    return [event for event in events if event["request_id"] == own_request_id]


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

    def test_list_shows_every_session_not_just_the_most_recent(self, servers: _Servers) -> None:
        """Live-reported regression: a gateway restart mints a fresh fallback
        session id, and the vault inspector's default listing was scoped to
        only that one -- silently hiding every earlier session's tokens even
        though the rows are still in ``VAULT_DB_PATH``.
        """
        other_session_id = "other-22222222-2222-2222-2222-222222222222"
        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
            session_id=HOOK_SESSION_ID,
        )
        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": "my dni is 12345678Z"}]},
            session_id=other_session_id,
        )

        resp = _get(f"{servers.gateway_url}/veil-panel/vault", session_id=None)

        assert resp.status_code == 200
        data = resp.json()
        seen_sessions = {m["session_id"] for m in data["mappings"]}
        assert HOOK_SESSION_ID in seen_sessions
        assert other_session_id in seen_sessions

    def test_reveal_disambiguates_identically_named_tokens_across_sessions(
        self, servers: _Servers
    ) -> None:
        """Two sessions each mint their own `PII_EMAIL_0001` -- for different
        real values. Listing must tell them apart (via `session_id` per row)
        and reveal must resolve each to its own value, not the other's.
        """
        other_session_id = "other-33333333-3333-3333-3333-333333333333"
        other_email = "someone.else@example.com"
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

        listed = _get(f"{servers.gateway_url}/veil-panel/vault", session_id=None).json()
        rows = [m for m in listed["mappings"] if m["token"] == "PII_EMAIL_0001"]
        assert len(rows) == 2
        assert {r["session_id"] for r in rows} == {HOOK_SESSION_ID, other_session_id}

        for row, expected_value in (
            (HOOK_SESSION_ID, VALID_EMAIL),
            (other_session_id, other_email),
        ):
            resp = httpx.post(
                f"{servers.gateway_url}/veil-panel/vault/reveal",
                json={"token": "PII_EMAIL_0001", "session_id": row},
                timeout=10,
            )
            assert resp.json()["value"] == expected_value


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


class TestCycleKindClassification:
    """Label a cycle so the panel can hide non-conversational ones.

    Found via a throwaway raw-capture proxy (bypassing this module's own
    extraction entirely) against real Claude Code traffic: a session-
    title-generation request and an agent-loop continuation turn (whose
    only "new" content is a Claude-Code-injected `<system-reminder>` block)
    are both real requests, not bugs -- but neither is something a human
    typed, so the panel's cycle selector should default to hiding them.
    """

    def test_title_generation_request_is_classified(self, servers: _Servers) -> None:
        body = {
            "model": "claude-x",
            "system": [
                {"type": "text", "text": "You are a Claude agent."},
                {
                    "type": "text",
                    "text": "You are naming a coding session so the user can pick it"
                    " out of a long list of sessions. The title is...",
                },
            ],
            "messages": [{"role": "user", "content": "<session>\nfoo\n</session>"}],
        }

        events = _run_request_and_collect_pipeline_events(servers, body)

        for stage in gateway_server.PIPELINE_STAGES:
            event = next(e for e in events if e["stage"] == stage)
            assert event["kind"] == "title_generation", stage

    def test_internal_continuation_turn_is_classified(self, servers: _Servers) -> None:
        body = {
            "model": "claude-x",
            "messages": [
                {"role": "user", "content": "the real question"},
                {"role": "assistant", "content": "the real answer"},
                {
                    "role": "user",
                    "content": "<system-reminder>\ntoken budget: 42\n</system-reminder>",
                },
            ],
        }

        events = _run_request_and_collect_pipeline_events(servers, body)

        for stage in gateway_server.PIPELINE_STAGES:
            event = next(e for e in events if e["stage"] == stage)
            assert event["kind"] == "internal_continuation", stage

    def test_an_ordinary_conversational_turn_is_classified_as_conversation(
        self, servers: _Servers
    ) -> None:
        events = _run_request_and_collect_pipeline_events(
            servers, {"messages": [{"role": "user", "content": "hello there"}]}
        )

        for stage in gateway_server.PIPELINE_STAGES:
            event = next(e for e in events if e["stage"] == stage)
            assert event["kind"] == "conversation", stage

    def test_a_real_turn_prefixed_with_a_system_reminder_is_not_hidden(
        self, servers: _Servers
    ) -> None:
        """Regression guard: this was the actual over-hiding bug reported live.

        Claude Code prepends a `<system-reminder>` block before the human's
        own typed text on essentially every real conversational turn (not
        just on agent-loop continuation turns whose *only* content is a
        reminder) -- a plain `str.startswith("<system-reminder")` check
        mislabeled every one of these as `"internal_continuation"`, hiding
        virtually all real conversation from the panel's default view.
        """
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "<system-reminder>\ntoken budget: 42\n</system-reminder>\n"
                    "I'm Alfonso, my DNI is 12345678A",
                },
            ],
        }

        events = _run_request_and_collect_pipeline_events(servers, body)

        for stage in gateway_server.PIPELINE_STAGES:
            event = next(e for e in events if e["stage"] == stage)
            assert event["kind"] == "conversation", stage

    def test_session_tag_around_real_content_does_not_force_title_generation(
        self, servers: _Servers
    ) -> None:
        """`<session>` alone must not be the title-generation signal.

        A human tester live-reported an ordinary conversational turn whose
        content was also wrapped in `<session>...</session>` -- distinct
        from Claude Code's dedicated title-generation request, which is
        identified by its own system prompt (`_TITLE_GENERATION_SYSTEM_MARKER`),
        not by the presence of a `<session>` tag.
        """
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "<session>\nprior context\n</session>\n"
                    "I'm Alfonso, my DNI is 12345678A",
                },
            ],
        }

        events = _run_request_and_collect_pipeline_events(servers, body)

        for stage in gateway_server.PIPELINE_STAGES:
            event = next(e for e in events if e["stage"] == stage)
            assert event["kind"] == "conversation", stage

    def test_internal_safety_classifier_request_is_classified(self, servers: _Servers) -> None:
        """Found live against a real ``claude -p --demo-db`` session.

        Claude Code's own harm/safety classifier wraps a JSON transcript
        summary in ``<transcript>...</transcript>`` as the *last user
        message* and asks for a bare ``<severity>N</severity>`` reply --
        the instruction text sits *outside* the tag, so stripping known
        wrapper tags alone does not empty this message the way it does for
        ``<system-reminder>``. Before this fix it read as an ordinary
        ``"conversation"`` and stole the turn-tracking pointer
        away from the real conversation.
        """
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": '<transcript>\n{"user":"query the database"}\n</transcript>\n'
                    "Stage 1 does NOT apply user intent. Respond with"
                    " <severity>N</severity> ONLY. No other text.",
                },
            ],
        }

        events = _run_request_and_collect_pipeline_events(servers, body)

        for stage in gateway_server.PIPELINE_STAGES:
            event = next(e for e in events if e["stage"] == stage)
            assert event["kind"] == "safety_check", stage


class TestExcludedMetadataNeverTokenized:
    """Only Anthropic's fixed commit-trailer address is excluded from tokenization.

    Wrapper tags Claude Code injects (`<system-reminder>`, `<session>`, ...)
    are stripped for display only. Their contents are tokenized: Claude Code
    puts real file content inside `<system-reminder>`, and a tool result can
    contain any of these tags verbatim.
    """

    def test_email_inside_a_system_reminder_is_tokenized(self, servers: _Servers) -> None:
        real_email = "customer@example.com"
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": f"<system-reminder>\naccount email: {VALID_EMAIL}\n"
                    f"</system-reminder>\ncontact {real_email}",
                },
            ],
        }

        resp = _post(f"{servers.gateway_url}/v1/messages", json=body)
        assert resp.status_code == 200
        forwarded = json.loads(servers.upstream_state.requests[-1]["body"])
        forwarded_text = str(forwarded["messages"][0]["content"])

        assert VALID_EMAIL not in forwarded_text
        assert real_email not in forwarded_text
        assert "PII_EMAIL_0001" in forwarded_text
        assert "PII_EMAIL_0002" in forwarded_text

    @pytest.mark.parametrize(
        "wrapped",
        [
            "<session>row: maria.lopez@example.com 12345678Z</session>",
            "<Session id=1>row: maria.lopez@example.com 12345678Z</session>",
            "<system-reminder>row: maria.lopez@example.com 12345678Z</system-reminder>",
        ],
    )
    def test_pii_inside_wrapper_tags_in_a_tool_result_is_tokenized(
        self, servers: _Servers, wrapped: str
    ) -> None:
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": wrapped}
                    ],
                },
            ],
        }

        resp = _post(f"{servers.gateway_url}/v1/messages", json=body)
        assert resp.status_code == 200
        forwarded = servers.upstream_state.requests[-1]["body"].decode("utf-8")

        assert "maria.lopez@example.com" not in forwarded
        assert "12345678Z" not in forwarded
        assert "PII_EMAIL_" in forwarded
        assert "PII_DNI_" in forwarded

    def test_anthropic_commit_trailer_address_is_never_tokenized(self, servers: _Servers) -> None:
        body = {
            "system": (
                "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n"
                "Some other static tool instruction text."
            ),
            "messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}],
        }

        resp = _post(f"{servers.gateway_url}/v1/messages", json=body)
        assert resp.status_code == 200
        forwarded = json.loads(servers.upstream_state.requests[-1]["body"])

        assert "noreply@anthropic.com" in str(forwarded["system"])
        assert VALID_EMAIL not in str(forwarded["messages"])
        assert "PII_EMAIL_0001" in str(forwarded["messages"])


class TestMessageTextExtraction:
    """Panes must show the human message, not the raw JSON document.

    ``core/tokenizer.py``'s ``Span`` offsets are per string-leaf, not per
    whole-document -- these tests exist because the earlier code
    sliced them into the whole document text anyway, which is the concrete
    reason the panel read as illegible garbage for any real payload.
    """

    def test_input_pane_shows_only_the_latest_user_turn_with_correctly_aligned_spans(
        self, servers: _Servers
    ) -> None:
        last_turn = f"contact {VALID_EMAIL} please"
        body = {
            "model": "claude-x",
            "system": "You are a careful assistant. " * 5,
            "tools": [{"name": "get_weather", "description": "Get the weather for a city"}],
            "messages": [
                {"role": "user", "content": "hi there"},
                {"role": "assistant", "content": "Hello! How can I help?"},
                {"role": "user", "content": last_turn},
            ],
        }

        events = _run_request_and_collect_pipeline_events(servers, body)

        received = next(e for e in events if e["stage"] == "received")
        assert received["text"] == last_turn
        assert "careful assistant" not in received["text"]
        assert "get_weather" not in received["text"]
        assert "hi there" not in received["text"]

        span = next(s for s in received["spans"] if s["entity_type"] == "EMAIL")
        assert received["text"][span["start"] : span["end"]] == VALID_EMAIL

    def test_naive_whole_document_offset_would_have_been_wrong(self, servers: _Servers) -> None:
        """Regression guard for the actual bug being fixed.

        The email's offset *within the whole raw JSON document* differs
        from its offset within the extracted last-user-turn text alone,
        proving this isn't a no-op for a payload where the two would
        coincidentally agree.
        """
        last_turn = f"contact {VALID_EMAIL} please"
        body = {
            "model": "claude-x",
            "system": "You are a careful assistant. " * 20,
            "messages": [{"role": "user", "content": last_turn}],
        }
        whole_document_offset = json.dumps(body).index(VALID_EMAIL)
        last_turn_offset = last_turn.index(VALID_EMAIL)
        assert whole_document_offset != last_turn_offset  # the bug's precondition

        events = _run_request_and_collect_pipeline_events(servers, body)

        received = next(e for e in events if e["stage"] == "received")
        span = next(s for s in received["spans"] if s["entity_type"] == "EMAIL")
        assert span["start"] == last_turn_offset
        assert received["text"][span["start"] : span["end"]] == VALID_EMAIL

    def test_non_text_last_turn_content_renders_a_placeholder_not_an_empty_pane(
        self, servers: _Servers
    ) -> None:
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "42"}],
                },
            ],
        }

        events = _run_request_and_collect_pipeline_events(servers, body)

        received = next(e for e in events if e["stage"] == "received")
        assert received["text"]
        assert "[tool_result]" in received["text"]

    def test_input_pane_shows_exactly_the_human_typed_text_no_more_no_less(
        self, servers: _Servers
    ) -> None:
        """Live-reported regression: injected wrapper tags must not leak into the pane.

        A real Claude Code turn interleaves `<system-reminder>` (and
        occasionally `<session>`) framing with the human's own text in the
        very same message. The Input pane must show exactly what the human
        typed -- "no more, no less" -- not that framing.
        """
        human_text = "I'm Alfonso, my DNI is 12345678A"
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "<system-reminder>\nsome budget note\n</system-reminder>\n"
                    f"<session>\nprior context\n</session>\n{human_text}",
                },
            ],
        }

        events = _run_request_and_collect_pipeline_events(servers, body)

        received = next(e for e in events if e["stage"] == "received")
        assert received["text"] == human_text
        assert "system-reminder" not in received["text"]
        assert "session" not in received["text"]


class TestResponseTextExtraction:
    """The response panes reconstruct display text, never wire framing."""

    def _mint_email_token(self, servers: _Servers) -> str:
        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"my email is {VALID_EMAIL}"}]},
        )
        forwarded = json.loads(servers.upstream_state.requests[-1]["body"])
        token = str(forwarded["messages"][0]["content"]).split()[-1].rstrip(".")
        assert token == "PII_EMAIL_0001"
        return token

    def test_buffered_json_response_shows_only_reconstructed_text(self, servers: _Servers) -> None:
        token = self._mint_email_token(servers)
        servers.upstream_state.response_headers = {"Content-Type": "application/json"}
        servers.upstream_state.response_body = json.dumps(
            {
                "id": "msg_123",
                "role": "assistant",
                "content": [{"type": "text", "text": f"Sure, contact {token}."}],
                "stop_reason": "end_turn",
            }
        ).encode()

        events = _run_request_and_collect_pipeline_events(servers, {"messages": []})

        response_received = next(e for e in events if e["stage"] == "response_received")
        assert response_received["text"] == f"Sure, contact {token}."
        assert "msg_123" not in response_received["text"]
        assert "stop_reason" not in response_received["text"]

        displayed = next(e for e in events if e["stage"] == "displayed")
        assert displayed["text"] == f"Sure, contact {VALID_EMAIL}."

    def test_sse_response_shows_only_reconstructed_text_across_blocks(
        self, servers: _Servers
    ) -> None:
        token = self._mint_email_token(servers)
        servers.upstream_state.response_headers = {
            "Content-Type": "text/event-stream; charset=utf-8"
        }
        servers.upstream_state.response_body = _sse_body(
            [
                {"type": "message_start", "message": {"id": "msg_1"}},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "hmm, "},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "let me check."},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "Sure, contact "},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": f"{token}."},
                },
                {"type": "content_block_stop", "index": 1},
                {"type": "message_stop"},
            ]
        )

        events = _run_request_and_collect_pipeline_events(servers, {"messages": []})

        response_received = next(e for e in events if e["stage"] == "response_received")
        assert response_received["text"] == f"hmm, let me check.Sure, contact {token}."
        assert "content_block_start" not in response_received["text"]
        assert "message_start" not in response_received["text"]
        assert "event:" not in response_received["text"]

        displayed = next(e for e in events if e["stage"] == "displayed")
        assert displayed["text"] == f"hmm, let me check.Sure, contact {VALID_EMAIL}."


class TestPipelineHistoryBuffer:
    """A panel opened after cycles already ran must still see them."""

    def test_late_subscriber_receives_already_completed_cycles_oldest_first(
        self, servers: _Servers
    ) -> None:
        for i in range(3):
            _post(
                f"{servers.gateway_url}/v1/messages",
                json={"messages": [{"role": "user", "content": f"turn {i}"}]},
            )

        chunks = _read_sse_chunks_until_idle(f"{servers.gateway_url}/veil-panel/pipeline-stream")
        events = _parse_sse_events("".join(chunks))

        received_texts = [e["text"] for e in events if e["stage"] == "received"]
        assert received_texts == ["turn 0", "turn 1", "turn 2"]

    def test_ring_buffer_is_bounded_and_evicts_oldest_first(self, servers: _Servers) -> None:
        total = gateway_server.PIPELINE_HISTORY_LIMIT + 2
        for i in range(total):
            _post(
                f"{servers.gateway_url}/v1/messages",
                json={"messages": [{"role": "user", "content": f"turn {i}"}]},
            )

        chunks = _read_sse_chunks_until_idle(f"{servers.gateway_url}/veil-panel/pipeline-stream")
        events = _parse_sse_events("".join(chunks))

        received_texts = [e["text"] for e in events if e["stage"] == "received"]
        assert len(received_texts) == gateway_server.PIPELINE_HISTORY_LIMIT
        assert received_texts == [f"turn {i}" for i in range(2, total)]

    def test_auxiliary_kind_burst_never_evicts_a_real_turn(self) -> None:
        """Found live: a single real conversation with several

        tool round-trips produced enough interleaved ``"safety_check"``
        cycles (Claude Code's own harm classifier, observed firing roughly
        once per tool call) to evict the real conversation cycle from a
        single shared 20-slot ring buffer before the panel ever connected.
        ``"safety_check"``/``"title_generation"``/
        ``"internal_continuation"`` now evict from their own separate
        buffer, so a real ``"conversation"`` cycle published once, however
        many auxiliary-kind cycles follow it, must always survive.
        """
        bus = gateway_server.PipelineEventBus()
        bus.publish(
            {
                "request_id": "real",
                "stage": "received",
                "text": "real turn",
                "kind": "conversation",
            },
            session_id="s1",
        )
        for i in range(gateway_server.PIPELINE_HISTORY_LIMIT * 2):
            bus.publish(
                {
                    "request_id": f"noise-{i}",
                    "stage": "received",
                    "text": "",
                    "kind": "safety_check",
                }
            )

        _, backlog = bus.subscribe()
        assert any(e["request_id"] == "real" for e in backlog)


class TestPipelineEventBusConcurrency:
    """Direct unit test of the ring buffer's lock discipline (no HTTP layer).

    Exercises this exact race:
    ``publish()``'s history/subscriber-snapshot and ``subscribe()``'s
    backlog-snapshot/registration must be mutually exclusive, or an event
    could be delivered to one subscriber twice (once via backlog, once
    live). "Never dropped" is not asserted here -- a subscriber connecting
    mid-burst legitimately misses events published before it existed; what
    must never happen, for any cycle a given subscriber's snapshot *did*
    observe, is seeing one of its stages more than once.
    """

    def test_subscribe_publish_race_never_duplicates_a_stage_for_one_subscriber(self) -> None:
        bus = gateway_server.PipelineEventBus()
        stages = gateway_server.PIPELINE_STAGES
        num_cycles = 200

        def _publish_cycles() -> None:
            for i in range(num_cycles):
                request_id = f"req-{i}"
                for stage in stages:
                    bus.publish({"request_id": request_id, "stage": stage, "text": ""})

        collected: list[list[dict[str, Any]]] = []
        collected_lock = threading.Lock()
        stop = threading.Event()

        def _subscribe_repeatedly() -> None:
            while not stop.is_set():
                subscriber, backlog = bus.subscribe()
                live: list[dict[str, Any]] = []
                try:
                    while True:
                        live.append(subscriber.get_nowait())
                except queue.Empty:
                    pass
                bus.unsubscribe(subscriber)
                with collected_lock:
                    collected.append(backlog + live)

        publisher = threading.Thread(target=_publish_cycles)
        subscriber_thread = threading.Thread(target=_subscribe_repeatedly)
        publisher.start()
        subscriber_thread.start()
        publisher.join(timeout=10)
        stop.set()
        subscriber_thread.join(timeout=10)

        assert collected, "expected at least one subscribe snapshot during the publish burst"
        for snapshot in collected:
            by_request: dict[str, list[str]] = {}
            for event in snapshot:
                by_request.setdefault(event["request_id"], []).append(event["stage"])
            for request_id, seen_stages in by_request.items():
                assert len(seen_stages) == len(set(seen_stages)), (
                    f"{request_id} delivered a duplicate stage to one subscriber: {seen_stages}"
                )


class TestMetricsComparisonEndpoint:
    """The persisted regex/LLM/union comparison the Metrics tab shows."""

    def test_cache_miss_before_any_run(self, servers: _Servers) -> None:
        resp = _get(f"{servers.gateway_url}/veil-panel/metrics/comparison")

        assert resp.status_code == 200
        assert resp.json() == {"available": False}

    def test_post_persists_and_a_second_independent_handler_reads_it_back(
        self, servers: _Servers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_report = Report(
            by_type={"EMAIL": Metrics(support=1, true_positives=1, false_positives=0)},
            by_detector={"REGEX": Metrics(support=1, true_positives=1, false_positives=0)},
            misses=[],
        )
        monkeypatch.setattr(eval_module, "evaluate", lambda *a, **k: fake_report)
        monkeypatch.setattr(eval_module, "llm_client_from_env", lambda: _StubLLMClient())

        resp = httpx.post(f"{servers.gateway_url}/veil-panel/metrics/comparison", timeout=10)

        assert resp.status_code == 200
        posted = resp.json()
        assert posted["available"] is True
        for mode in ("regex", "llm", "union"):
            assert posted["modes"][mode]["available"] is True
            assert posted["modes"][mode]["by_type"]["EMAIL"]["recall"] == 1.0

        # Simulate a gateway restart: a second, independently-constructed
        # handler pointed at the same (monkeypatched) cache path must read
        # back the same persisted result, with no run of its own.
        client2 = httpx.Client(timeout=httpx.Timeout(10.0))
        handler_cls2 = gateway_server.make_handler(
            gateway_server._new_session_id(), client2, "http://127.0.0.1:1"
        )
        gateway2 = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls2)
        gateway2_thread = threading.Thread(
            target=gateway2.serve_forever, kwargs={"poll_interval": _POLL_INTERVAL}, daemon=True
        )
        gateway2_thread.start()
        try:
            resp2 = _get(
                f"http://127.0.0.1:{gateway2.server_address[1]}/veil-panel/metrics/comparison"
            )
            assert resp2.status_code == 200
            assert resp2.json() == posted
        finally:
            gateway2.shutdown()
            gateway2.server_close()
            gateway2_thread.join(timeout=5)
            client2.close()

    def test_llm_and_union_failing_still_returns_a_real_regex_result(
        self, servers: _Servers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        regex_report = Report(
            by_type={"EMAIL": Metrics(support=3, true_positives=3, false_positives=0)},
            by_detector={},
            misses=[],
        )
        monkeypatch.setattr(eval_module, "evaluate", lambda *a, **k: regex_report)
        # `llm_client_from_env()` is left un-mocked: with no VEIL_LLM_BASE_URL/
        # VEIL_LLM_MODEL set (conftest strips them for every test), it raises
        # `SystemExit` for both non-regex modes before `evaluate()` is even
        # reached -- the realistic "LM Studio not configured" shape.

        resp = httpx.post(f"{servers.gateway_url}/veil-panel/metrics/comparison", timeout=10)

        assert resp.status_code == 200
        data = resp.json()
        assert data["modes"]["regex"]["available"] is True
        assert data["modes"]["regex"]["by_type"]["EMAIL"]["recall"] == 1.0
        assert data["modes"]["llm"]["available"] is False
        assert "reason" in data["modes"]["llm"]
        assert data["modes"]["union"]["available"] is False

    def test_persisted_cache_holds_no_raw_manifest_value(
        self, servers: _Servers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report_with_a_miss = Report(
            by_type={"EMAIL": Metrics(support=1, true_positives=0, false_positives=0)},
            by_detector={},
            misses=[
                Miss(
                    table="customers",
                    column="email",
                    row_id=1,
                    entity_type="EMAIL",
                    value=VALID_EMAIL,
                )
            ],
        )
        monkeypatch.setattr(eval_module, "evaluate", lambda *a, **k: report_with_a_miss)

        httpx.post(f"{servers.gateway_url}/veil-panel/metrics/comparison", timeout=10)

        cached_text = gateway_server.METRICS_COMPARISON_PATH.read_text(encoding="utf-8")
        assert VALID_EMAIL not in cached_text
        assert '"value"' not in cached_text


class TestToolContinuationClassification:
    """A pure tool_result continuation must not read as a fresh conversation.

    ``_extract_last_user_message_text`` renders a bare ``tool_result`` block
    as the non-empty placeholder ``"[tool_result]"`` -- non-empty enough to
    fail the existing ``internal_continuation`` check, which is exactly why
    this used to be misclassified as an ordinary ``"conversation"`` and
    given its own chip in the panel's selector.
    """

    def test_pure_tool_result_message_is_tool_continuation(self, servers: _Servers) -> None:
        body = {
            "messages": [
                {"role": "user", "content": "run the query"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "rows..."}],
                },
            ],
        }

        events = _run_request_and_collect_pipeline_events(servers, body)

        for stage in gateway_server.PIPELINE_STAGES:
            event = next(e for e in events if e["stage"] == stage)
            assert event["kind"] == "tool_continuation", stage

    def test_real_text_alongside_a_tool_result_stays_conversation(self, servers: _Servers) -> None:
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "rows..."},
                        {"type": "text", "text": "also, here is a follow-up question"},
                    ],
                },
            ],
        }

        events = _run_request_and_collect_pipeline_events(servers, body)

        for stage in gateway_server.PIPELINE_STAGES:
            event = next(e for e in events if e["stage"] == stage)
            assert event["kind"] == "conversation", stage


class TestTurnTracking:
    """``PipelineEventBus`` attaches a tool_continuation cycle to its open turn.

    Direct unit tests of ``PipelineEventBus`` itself (no HTTP layer,
    matching ``TestPipelineEventBusConcurrency``'s existing pattern) --
    this is pure bus bookkeeping, independent of how a cycle's ``kind`` was
    ever decided.
    """

    def test_tool_continuation_attaches_to_the_open_turn(self) -> None:
        bus = gateway_server.PipelineEventBus()
        bus.publish(
            {"request_id": "r1", "stage": "received", "text": "", "kind": "conversation"},
            session_id="s1",
        )
        bus.publish(
            {"request_id": "r2", "stage": "received", "text": "", "kind": "tool_continuation"},
            session_id="s1",
        )

        _, backlog = bus.subscribe()
        r2_events = [e for e in backlog if e["request_id"] == "r2"]
        assert r2_events and all(e["parent_request_id"] == "r1" for e in r2_events)

    def test_a_later_conversation_cycle_with_different_text_supersedes_the_open_turn(
        self,
    ) -> None:
        """A genuinely new turn (different text) opens its own turn, not a merge."""
        bus = gateway_server.PipelineEventBus()
        bus.publish(
            {
                "request_id": "rA",
                "stage": "received",
                "text": "first question",
                "kind": "conversation",
            },
            session_id="s1",
        )
        bus.publish(
            {
                "request_id": "rB",
                "stage": "received",
                "text": "second question",
                "kind": "conversation",
            },
            session_id="s1",
        )
        bus.publish(
            {"request_id": "rC", "stage": "received", "text": "", "kind": "tool_continuation"},
            session_id="s1",
        )

        _, backlog = bus.subscribe()
        rb_events = [e for e in backlog if e["request_id"] == "rB"]
        rc_events = [e for e in backlog if e["request_id"] == "rC"]
        assert rb_events and all("parent_request_id" not in e for e in rb_events)
        assert rc_events and all(e["parent_request_id"] == "rB" for e in rc_events)

    def test_same_text_conversation_cycles_merge_under_one_turn(self) -> None:
        """Revised after live testing: two real, independent

        gateway round-trips with byte-identical received text -- observed
        live against a real ``claude -p`` session --
        must fold into the *same* turn rather
        than opening a second one and splitting a single logical request
        across two chips.
        """
        bus = gateway_server.PipelineEventBus()
        bus.publish(
            {
                "request_id": "rA",
                "stage": "received",
                "text": "same question",
                "kind": "conversation",
            },
            session_id="s1",
        )
        bus.publish(
            {
                "request_id": "rB",
                "stage": "received",
                "text": "same question",
                "kind": "conversation",
            },
            session_id="s1",
        )
        bus.publish(
            {"request_id": "rC", "stage": "received", "text": "", "kind": "tool_continuation"},
            session_id="s1",
        )

        _, backlog = bus.subscribe()
        ra_events = [e for e in backlog if e["request_id"] == "rA"]
        rb_events = [e for e in backlog if e["request_id"] == "rB"]
        rc_events = [e for e in backlog if e["request_id"] == "rC"]
        assert ra_events and all("parent_request_id" not in e for e in ra_events)
        assert rb_events and all(e["parent_request_id"] == "rA" for e in rb_events)
        # A tool_continuation published after both still attaches to the
        # *original* canonical turn (rA), never to the merged sibling (rB).
        assert rc_events and all(e["parent_request_id"] == "rA" for e in rc_events)

    def test_tool_continuation_with_no_open_turn_carries_no_parent(self) -> None:
        bus = gateway_server.PipelineEventBus()
        bus.publish(
            {"request_id": "r1", "stage": "received", "text": "", "kind": "tool_continuation"},
            session_id="s-never-opened",
        )

        _, backlog = bus.subscribe()
        assert backlog and all("parent_request_id" not in e for e in backlog)

    def test_open_turn_for_session_reflects_the_latest_conversation_cycle(self) -> None:
        bus = gateway_server.PipelineEventBus()
        assert bus.open_turn_for_session("s1") is None

        bus.publish(
            {"request_id": "r1", "stage": "received", "text": "", "kind": "conversation"},
            session_id="s1",
        )
        assert bus.open_turn_for_session("s1") == "r1"


class TestHookEventEndpoint:
    """Best-effort ingress for a hook's own live progress report."""

    def test_known_open_turn_publishes_a_hook_event(self, servers: _Servers) -> None:
        conversation_events = _run_request_and_collect_pipeline_events(
            servers, {"messages": [{"role": "user", "content": "run the query"}]}
        )
        turn_request_id = conversation_events[-1]["request_id"]

        resp = _post(
            f"{servers.gateway_url}/veil-panel/hook-event",
            json={
                "session_id": HOOK_SESSION_ID,
                "hook": "pre_tool_use",
                "phase": "start",
                "tool_name": "Bash",
            },
        )
        assert resp.status_code == 204

        chunks = _read_sse_chunks_until_idle(f"{servers.gateway_url}/veil-panel/pipeline-stream")
        events = _parse_sse_events("".join(chunks))
        hook_events = [
            e for e in events if e["request_id"] == turn_request_id and e.get("kind") == "hook"
        ]
        assert len(hook_events) == 1
        assert hook_events[0]["hook"] == "pre_tool_use"
        assert hook_events[0]["phase"] == "start"
        assert hook_events[0]["tool_name"] == "Bash"
        assert set(hook_events[0]) == {
            "request_id",
            "kind",
            "hook",
            "phase",
            "tool_name",
            "timestamp",
        }

    def test_no_open_turn_publishes_nothing(self, servers: _Servers) -> None:
        resp = _post(
            f"{servers.gateway_url}/veil-panel/hook-event",
            json={
                "session_id": "session-with-no-turn-ever-opened",
                "hook": "pre_tool_use",
                "phase": "start",
                "tool_name": "Bash",
            },
        )
        assert resp.status_code == 204

        chunks = _read_sse_chunks_until_idle(
            f"{servers.gateway_url}/veil-panel/pipeline-stream",
            idle_timeout=0.5,
            overall_timeout=2.0,
        )
        events = _parse_sse_events("".join(chunks))
        assert not any(e.get("kind") == "hook" for e in events)

    @pytest.mark.parametrize(
        "raw_body",
        [
            b"not json",
            b"[1, 2, 3]",
            json.dumps(
                {"session_id": HOOK_SESSION_ID, "hook": "pre_tool_use", "phase": "start"}
            ).encode(),
            json.dumps(
                {
                    "session_id": HOOK_SESSION_ID,
                    "hook": "not_a_real_hook",
                    "phase": "start",
                    "tool_name": "Bash",
                }
            ).encode(),
        ],
    )
    def test_malformed_body_responds_204_never_500(
        self, servers: _Servers, raw_body: bytes
    ) -> None:
        resp = httpx.post(
            f"{servers.gateway_url}/veil-panel/hook-event",
            content=raw_body,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert resp.status_code == 204

    def test_extra_field_is_ignored_not_forwarded(self, servers: _Servers) -> None:
        conversation_events = _run_request_and_collect_pipeline_events(
            servers, {"messages": [{"role": "user", "content": "run the query"}]}
        )
        turn_request_id = conversation_events[-1]["request_id"]

        _post(
            f"{servers.gateway_url}/veil-panel/hook-event",
            json={
                "session_id": HOOK_SESSION_ID,
                "hook": "pre_tool_use",
                "phase": "start",
                "tool_name": "Bash",
                "command": (
                    "sqlite3 demo/veil_demo.db \"select * from users where dni='12345678A'\""
                ),
            },
        )

        chunks = _read_sse_chunks_until_idle(f"{servers.gateway_url}/veil-panel/pipeline-stream")
        events = _parse_sse_events("".join(chunks))
        hook_events = [
            e for e in events if e["request_id"] == turn_request_id and e.get("kind") == "hook"
        ]
        assert len(hook_events) == 1
        assert "command" not in hook_events[0]
        assert "12345678A" not in json.dumps(hook_events[0])


class _StubLLMClient:
    """Stands in for `core.detector.LLMClient` in tests that never make a real call."""

    def close(self) -> None:
        pass


class TestContentBlockWalk:
    """Only text is tokenized; ids, signatures, base64 and thinking pass byte for byte."""

    _BASE64_IBAN_LOOKALIKE = "pf50hVxWO4IIUrzQljUUt37gsKD0"

    def _forward(self, servers: _Servers, messages: list[Any]) -> list[Any]:
        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": messages})
        assert resp.status_code == 200
        forwarded: list[Any] = json.loads(servers.upstream_state.requests[-1]["body"])["messages"]
        return forwarded

    def test_tool_result_text_blocks_are_tokenized(self, servers: _Servers) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": [{"type": "text", "text": "DNI 12345678Z"}],
                    }
                ],
            }
        ]

        forwarded = self._forward(servers, messages)

        block = forwarded[0]["content"][0]
        assert block["tool_use_id"] == "toolu_01"
        assert block["content"][0]["text"] == "DNI PII_DNI_0001"

    def test_tool_use_input_is_tokenized_and_its_id_kept(self, servers: _Servers) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_02",
                        "name": "Bash",
                        "input": {"command": f"grep {VALID_EMAIL} users.csv"},
                    }
                ],
            }
        ]

        forwarded = self._forward(servers, messages)

        block = forwarded[0]["content"][0]
        assert block["id"] == "toolu_02"
        assert block["name"] == "Bash"
        assert block["input"]["command"] == "grep PII_EMAIL_0001 users.csv"

    def test_image_and_thinking_blocks_are_forwarded_byte_for_byte(self, servers: _Servers) -> None:
        image = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": f"iVBORw0K{self._BASE64_IBAN_LOOKALIKE}AAAA",
            },
        }
        thinking = {
            "type": "thinking",
            "thinking": f"the user wrote {VALID_EMAIL}",
            "signature": f"Eq{self._BASE64_IBAN_LOOKALIKE}xyz",
        }
        messages = [
            {"role": "assistant", "content": [thinking, {"type": "text", "text": "ok"}]},
            {"role": "user", "content": [image, {"type": "text", "text": f"see {VALID_EMAIL}"}]},
        ]

        forwarded = self._forward(servers, messages)

        assert forwarded[0]["content"][0] == thinking
        assert forwarded[1]["content"][0] == image
        assert forwarded[1]["content"][1]["text"] == "see PII_EMAIL_0001"

    def test_text_document_source_is_tokenized(self, servers: _Servers) -> None:
        document = {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": f"to {VALID_EMAIL}"},
        }

        forwarded = self._forward(servers, [{"role": "user", "content": [document]}])

        source = forwarded[0]["content"][0]["source"]
        assert source["media_type"] == "text/plain"
        assert source["data"] == "to PII_EMAIL_0001"


class TestThinkingIsNeverDetokenized:
    """Thinking goes back to the API signed; a resolved copy would break the signature."""

    def _mint_email_token(self, servers: _Servers) -> str:
        _post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": [{"role": "user", "content": f"contact {VALID_EMAIL}"}]},
        )
        return "PII_EMAIL_0001"

    def test_streaming_thinking_delta_keeps_its_tokens(self, servers: _Servers) -> None:
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
                    "content_block": {"type": "thinking", "thinking": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": f"look up {token}"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": f"found {token}"},
                },
                {"type": "content_block_stop", "index": 1},
                {"type": "message_stop"},
            ]
        )

        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})

        assert resp.status_code == 200
        assert f"look up {token}" in resp.text
        assert _extract_text_deltas(resp.text, index=1) == f"found {VALID_EMAIL}"

    def test_buffered_thinking_and_tool_input_keep_their_tokens(self, servers: _Servers) -> None:
        token = self._mint_email_token(servers)
        servers.upstream_state.response_body = json.dumps(
            {
                "content": [
                    {"type": "thinking", "thinking": f"look up {token}", "signature": "sig"},
                    {"type": "text", "text": f"found {token}"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"q": token}},
                ]
            }
        ).encode()

        resp = _post(f"{servers.gateway_url}/v1/messages", json={"messages": []})

        content = resp.json()["content"]
        assert content[0]["thinking"] == f"look up {token}"
        assert content[1]["text"] == f"found {VALID_EMAIL}"
        assert content[2]["input"] == {"q": token}


class TestPanelTurnBoundaries:
    def test_tool_result_with_only_a_system_reminder_is_not_real_text(self) -> None:
        parsed: Any = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "42"},
                        {
                            "type": "text",
                            "text": "<system-reminder>\nupdate your todo list\n</system-reminder>",
                        },
                    ],
                }
            ]
        }

        assert gateway_server._last_user_content_has_text(parsed) is False

    def test_real_text_next_to_a_system_reminder_still_counts(self) -> None:
        parsed: Any = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "<system-reminder>x</system-reminder>\nhello"},
                    ],
                }
            ]
        }

        assert gateway_server._last_user_content_has_text(parsed) is True

    def test_bodyless_request_publishes_no_panel_stages(self, servers: _Servers) -> None:
        resp = httpx.get(f"{servers.gateway_url}/v1/models", timeout=10.0)
        assert resp.status_code == 200

        chunks = _read_sse_chunks_until_idle(f"{servers.gateway_url}/veil-panel/pipeline-stream")

        assert _parse_sse_events("".join(chunks)) == []


class TestEmptyResponseFraming:
    """An empty or bodyless response must not desync a keep-alive connection."""

    @pytest.mark.parametrize("status", [200, 204, 304])
    def test_two_requests_on_one_connection_both_succeed(
        self, servers: _Servers, status: int
    ) -> None:
        servers.upstream_state.response_status = status
        servers.upstream_state.response_body = b""

        with httpx.Client(timeout=10.0) as client:
            first = client.get(f"{servers.gateway_url}/v1/models")
            second = client.get(f"{servers.gateway_url}/v1/models")

        assert first.status_code == second.status_code == status
        assert first.content == second.content == b""
        if status != 200:
            assert "transfer-encoding" not in first.headers


class TestPanelBodyValidation:
    @staticmethod
    def _raw_post(url: str, content_length: str, body: bytes = b"") -> tuple[int, bytes]:
        """Send a request with an arbitrary Content-Length header over a raw socket."""
        parts = urlsplit(url)
        request = (
            f"POST {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {content_length}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + body
        with socket.create_connection((parts.hostname, parts.port), timeout=10) as sock:
            sock.sendall(request)
            response = b""
            while chunk := sock.recv(65536):
                response += chunk
        status = int(response.split(b" ", 2)[1])
        return status, response.split(b"\r\n\r\n", 1)[1]

    @pytest.mark.parametrize(
        ("content_length", "status"), [("abc", 400), ("-1", 400), ("100000", 413)]
    )
    def test_reveal_rejects_a_bad_content_length(
        self, servers: _Servers, content_length: str, status: int
    ) -> None:
        got, body = self._raw_post(f"{servers.gateway_url}/veil-panel/vault/reveal", content_length)

        assert got == status
        assert "error" in json.loads(body)

    def test_reveal_rejects_a_non_string_session_id(self, servers: _Servers) -> None:
        resp = httpx.post(
            f"{servers.gateway_url}/veil-panel/vault/reveal",
            json={"token": "PII_EMAIL_0001", "session_id": 7},
            timeout=10,
        )

        assert resp.status_code == 400

    def test_hook_event_with_a_bad_content_length_still_answers_204(
        self, servers: _Servers
    ) -> None:
        status, _ = self._raw_post(f"{servers.gateway_url}/veil-panel/hook-event", "abc")

        assert status == 204

    @pytest.mark.parametrize("tool_name", ["x" * 129, "Bash; rm -rf /", "a\nb"])
    def test_hook_event_with_an_invalid_tool_name_publishes_nothing(
        self, servers: _Servers, tool_name: str
    ) -> None:
        _run_request_and_collect_pipeline_events(
            servers, {"messages": [{"role": "user", "content": "run the query"}]}
        )

        resp = _post(
            f"{servers.gateway_url}/veil-panel/hook-event",
            json={
                "session_id": HOOK_SESSION_ID,
                "hook": "pre_tool_use",
                "phase": "start",
                "tool_name": tool_name,
            },
        )
        assert resp.status_code == 204

        chunks = _read_sse_chunks_until_idle(f"{servers.gateway_url}/veil-panel/pipeline-stream")
        assert not [e for e in _parse_sse_events("".join(chunks)) if e.get("kind") == "hook"]

    def test_second_comparison_while_one_runs_gets_409(self, servers: _Servers) -> None:
        assert gateway_server._comparison_lock.acquire(blocking=False)
        try:
            resp = httpx.post(f"{servers.gateway_url}/veil-panel/metrics/comparison", timeout=10)
        finally:
            gateway_server._comparison_lock.release()

        assert resp.status_code == 409
