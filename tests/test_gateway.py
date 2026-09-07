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
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from veil_pii_middleware.adapters.gateway import server as gateway_server

_TEST_KEY_B64 = base64.b64encode(os.urandom(32)).decode()


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

    upstream_state = _RecordingUpstream()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _make_upstream_handler(upstream_state))
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    upstream_base = f"http://127.0.0.1:{upstream.server_address[1]}"

    client = httpx.Client(timeout=httpx.Timeout(10.0))
    session_id = gateway_server._new_session_id()
    handler_cls = gateway_server.make_handler(session_id, client, upstream_base)
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
        resp = httpx.post(f"{servers.gateway_url}/v1/messages?beta=true", json=body, timeout=10)
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
        httpx.post(f"{servers.gateway_url}/v1/messages", json=body, timeout=10)
        httpx.post(f"{servers.gateway_url}/v1/messages", json=body, timeout=10)

        assert len(servers.upstream_state.requests) == 2
        first = json.loads(servers.upstream_state.requests[0]["body"])
        second = json.loads(servers.upstream_state.requests[1]["body"])
        assert first == second
        assert "PII_EMAIL_0001" in first["messages"][0]["content"]

    def test_auth_header_forwarded_unchanged(self, servers: _Servers) -> None:
        resp = httpx.post(
            f"{servers.gateway_url}/v1/messages",
            json={"messages": []},
            headers={"Authorization": "Bearer test-secret-token", "x-api-key": "test-key"},
            timeout=10,
        )
        assert resp.status_code == 200
        upstream_req = servers.upstream_state.requests[0]
        assert upstream_req["headers"]["authorization"] == "Bearer test-secret-token"
        assert upstream_req["headers"]["x-api-key"] == "test-key"

    def test_get_with_no_body_passes_through_without_touching_the_vault(
        self, servers: _Servers, tmp_path: Path
    ) -> None:
        resp = httpx.get(f"{servers.gateway_url}/v1/models", timeout=10)
        assert resp.status_code == 200
        assert servers.upstream_state.requests[0]["body"] == b""
        assert not (tmp_path / ".veil" / "vault.db").exists()


class TestResponseIsRelayedUntouched:
    def test_response_body_is_not_rewritten_even_if_it_looks_like_a_token(
        self, servers: _Servers
    ) -> None:
        servers.upstream_state.response_body = json.dumps(
            {"content": [{"type": "text", "text": "your token is PII_EMAIL_0001"}]}
        ).encode()
        resp = httpx.post(f"{servers.gateway_url}/v1/messages", json={"messages": []}, timeout=10)
        assert resp.status_code == 200
        assert resp.json()["content"][0]["text"] == "your token is PII_EMAIL_0001"

    def test_large_response_relayed_byte_for_byte(self, servers: _Servers) -> None:
        big = ("x" * 50_000).encode()
        servers.upstream_state.response_body = big
        servers.upstream_state.response_headers = {"Content-Type": "text/plain"}
        resp = httpx.post(f"{servers.gateway_url}/v1/messages", json={"messages": []}, timeout=10)
        assert resp.status_code == 200
        assert resp.content == big

    def test_upstream_status_code_is_preserved(self, servers: _Servers) -> None:
        servers.upstream_state.response_status = 429
        servers.upstream_state.response_body = b'{"type":"error"}'
        resp = httpx.post(f"{servers.gateway_url}/v1/messages", json={"messages": []}, timeout=10)
        assert resp.status_code == 429


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
            b"POST /v1/messages HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"{}"
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
            httpx.post(f"{servers.gateway_url}/v1/messages", json={"messages": []}, timeout=10)

        servers.upstream_state.truncate_response = False
        servers.upstream_state.response_body = b'{"ok": true}'
        resp = httpx.post(f"{servers.gateway_url}/v1/messages", json={"messages": []}, timeout=10)
        assert resp.status_code == 200


class TestFailsClosed:
    def test_malformed_json_body_is_blocked_not_forwarded(self, servers: _Servers) -> None:
        resp = httpx.post(
            f"{servers.gateway_url}/v1/messages",
            content=b"{not valid json",
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert resp.status_code == 400
        assert servers.upstream_state.requests == []

    def test_vault_failure_blocks_the_request_entirely(
        self, servers: _Servers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VEIL_VAULT_KEY", raising=False)
        body = {"messages": [{"role": "user", "content": "carol@example.com"}]}
        resp = httpx.post(f"{servers.gateway_url}/v1/messages", json=body, timeout=10)
        assert resp.status_code == 500
        assert servers.upstream_state.requests == []

    def test_no_secret_or_pii_appears_in_gateway_stderr(
        self, servers: _Servers, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("VEIL_VAULT_KEY", raising=False)
        body = {"messages": [{"role": "user", "content": "dave@example.com"}]}
        httpx.post(f"{servers.gateway_url}/v1/messages", json=body, timeout=10)
        out, err = capfd.readouterr()
        assert "dave@example.com" not in out
        assert "dave@example.com" not in err
        assert _TEST_KEY_B64 not in out
        assert _TEST_KEY_B64 not in err
