"""The pre-demo warm-up: pins the Ollama model and checks the LLM layer answers."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import veil_pii_middleware.demo.warmup as warmup
from veil_pii_middleware.core.detector import LLM_BASE_URL_ENV_VAR, LLM_MODEL_ENV_VAR, Span


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """LLM env set; the keep-alive POST is recorded instead of sent."""
    monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, "http://127.0.0.1:11434")
    monkeypatch.setenv(LLM_MODEL_ENV_VAR, "qwen-test")
    posts: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        posts.append({"url": url, **kwargs})
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    return posts


def test_without_llm_env_it_reports_regex_only_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(LLM_BASE_URL_ENV_VAR, raising=False)
    monkeypatch.delenv(LLM_MODEL_ENV_VAR, raising=False)
    assert warmup.main() == 0
    assert "regex-only" in capsys.readouterr().out


def test_pins_the_model_and_reports_success(
    configured: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(warmup, "detect", lambda *a, **k: [])
    assert warmup.main() == 0
    assert configured[0]["url"] == "http://127.0.0.1:11434/api/generate"
    assert configured[0]["json"] == {"model": "qwen-test", "keep_alive": -1}
    out = capsys.readouterr().out
    assert "keep_alive=-1" in out
    assert "LLM layer answered" in out


def test_a_failed_probe_warns_loudly_and_exits_1(
    configured: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def detect(text: str, **kwargs: Any) -> list[Span]:
        kwargs["on_layer_error"]("llm", httpx.ReadTimeout("timed out"))
        return []

    monkeypatch.setattr(warmup, "detect", detect)
    assert warmup.main() == 1
    err = capsys.readouterr().err
    assert "THE LLM LAYER DID NOT ANSWER" in err
    assert "ReadTimeout" in err


def test_a_host_without_the_ollama_api_skips_keep_alive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, "http://127.0.0.1:1234")
    monkeypatch.setenv(LLM_MODEL_ENV_VAR, "lm-studio-model")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **k: httpx.Response(404, request=httpx.Request("POST", url)),
    )
    monkeypatch.setattr(warmup, "detect", lambda *a, **k: [])
    assert warmup.main() == 0
    assert "keep-alive step skipped" in capsys.readouterr().out
