"""The flow panel's prompt box: ``POST /veil-panel/ask`` and ``AskRunner``."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from veil_pii_middleware.adapters.gateway import panel_ask
from veil_pii_middleware.adapters.gateway import server as gateway_server
from veil_pii_middleware.adapters.gateway.panel_ask import ASK_ENV_VAR, AskRunner


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, list[list[str]]]]:
    """A gateway whose prompt runs are recorded instead of executed."""
    started: list[list[str]] = []

    def fake_start(self: AskRunner, prompt: str) -> str | None:
        if self._state.get("running"):
            return "a prompt is already running"
        started.append(["bash", "scripts/ask.sh", "--demo-db", prompt])
        self._state = {"running": True, "started": time.monotonic(), "prompt": prompt}
        return None

    monkeypatch.setattr(AskRunner, "start", fake_start)
    handler = gateway_server.make_handler("fallback", httpx.Client(), "http://127.0.0.1:9")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield url, started
    finally:
        server.shutdown()
        server.server_close()


def _ask(url: str, body: Any, **headers: str) -> httpx.Response:
    return httpx.post(
        f"{url}/veil-panel/ask",
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        timeout=10,
    )


def test_disabled_by_default(
    gateway: tuple[str, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, started = gateway
    monkeypatch.delenv(ASK_ENV_VAR, raising=False)
    assert _ask(url, {"prompt": "hi"}).status_code == 403
    assert started == []
    assert httpx.get(f"{url}/veil-panel/ask", timeout=10).json()["enabled"] is False


def test_runs_a_prompt_from_the_panel_origin(
    gateway: tuple[str, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, started = gateway
    monkeypatch.setenv(ASK_ENV_VAR, "1")
    resp = _ask(url, {"prompt": "The user a@b.es has a problem"}, Origin=url)
    assert resp.status_code == 202
    assert started == [["bash", "scripts/ask.sh", "--demo-db", "The user a@b.es has a problem"]]
    status = httpx.get(f"{url}/veil-panel/ask", timeout=10).json()
    assert status["running"] is True and status["enabled"] is True


def test_second_prompt_while_running_is_409(
    gateway: tuple[str, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _ = gateway
    monkeypatch.setenv(ASK_ENV_VAR, "1")
    assert _ask(url, {"prompt": "one"}).status_code == 202
    assert _ask(url, {"prompt": "two"}).status_code == 409


def test_other_origin_is_refused(
    gateway: tuple[str, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, started = gateway
    monkeypatch.setenv(ASK_ENV_VAR, "1")
    assert _ask(url, {"prompt": "hi"}, Origin="https://evil.example").status_code == 403
    assert started == []


def test_non_json_body_is_refused(
    gateway: tuple[str, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain form post needs no CORS preflight, so it must never run anything."""
    url, started = gateway
    monkeypatch.setenv(ASK_ENV_VAR, "1")
    resp = httpx.post(
        f"{url}/veil-panel/ask",
        content=b"prompt=hi",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    assert resp.status_code == 415
    assert started == []


@pytest.mark.parametrize("body", [{}, {"prompt": ""}, {"prompt": "x" * 4001}, ["prompt"]])
def test_bad_body_is_400(
    gateway: tuple[str, list[list[str]]], monkeypatch: pytest.MonkeyPatch, body: Any
) -> None:
    url, started = gateway
    monkeypatch.setenv(ASK_ENV_VAR, "1")
    assert _ask(url, body).status_code == 400
    assert started == []


def test_runner_passes_the_prompt_as_one_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """No shell parses the prompt: quotes and `$()` stay literal."""
    monkeypatch.setattr(panel_ask, "find_bash", lambda: "/bin/bash")
    calls: list[tuple[list[str], dict[str, str]]] = []
    done = threading.Event()

    def runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        calls.append((argv, env))
        done.set()
        return subprocess.CompletedProcess(argv, 0, stdout="answer", stderr="")

    ask = AskRunner("http://127.0.0.1:8787", runner=runner)
    prompt = 'it\'s $(rm -rf ~) "quoted"'
    assert ask.start(prompt) is None
    assert done.wait(5)
    for _ in range(50):
        if not ask.status()["running"]:
            break
        time.sleep(0.02)
    argv, env = calls[0]
    assert argv[0] == "/bin/bash"
    assert argv[2:] == ["--demo-db", prompt]
    assert env["VEIL_GATEWAY_URL"] == "http://127.0.0.1:8787"
    status = ask.status()
    assert status["running"] is False
    assert status["exit_code"] == 0
    assert status["output"] == "answer"
