"""Load the local LLM before a demo and check that the detector can reach it.

Ollama unloads an idle model after five minutes, and reloading it takes
longer than the detector's per-call timeout; on that failure ``detect()``
falls back to regex-only, so names would go out in clear. Run before a demo
(``scripts/run-gateway.sh`` does it):

1. Ask Ollama's native API to load the model and keep it loaded
   (``keep_alive: -1``). Other OpenAI-compatible hosts (LM Studio...) have no
   such endpoint; that step is skipped for them.
2. Send one real ``detect()`` call through the configured client and report,
   loudly, whether the LLM layer answered.

Exit codes: 0 when the LLM layer answered or none is configured (regex-only
by choice), 1 when it is configured but did not answer.
"""

from __future__ import annotations

import os
import sys
from typing import Final

import httpx

from veil_pii_middleware.core.detector import (
    LLM_BASE_URL_ENV_VAR,
    LLM_MODEL_ENV_VAR,
    detect,
    llm_client_from_env,
)

__all__ = ["main"]

#: Loading a 4-8B model from disk can take well over the per-call timeout.
_LOAD_TIMEOUT_SECONDS: Final = 180.0

_PROBE_TEXT: Final = "Ana García Ruiz from Acme Logística lives at Calle Mayor 5, Madrid."

_BANNER: Final = "!" * 72


def _keep_loaded(base_url: str, model: str) -> str:
    """Ask Ollama to load ``model`` and never unload it. Returns a status line."""
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={"model": model, "keep_alive": -1},
            timeout=_LOAD_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return f"could not ask the host to keep the model loaded ({type(exc).__name__})"
    if response.status_code == 404:
        return "host has no Ollama /api/generate endpoint; keep-alive step skipped"
    if response.is_error:
        return f"keep-alive request answered {response.status_code}"
    return "model loaded and pinned in memory (keep_alive=-1)"


def main() -> int:
    base_url = os.environ.get(LLM_BASE_URL_ENV_VAR, "").strip()
    model = os.environ.get(LLM_MODEL_ENV_VAR, "").strip()
    if not base_url or not model:
        print(
            f"veil warmup: {LLM_BASE_URL_ENV_VAR}/{LLM_MODEL_ENV_VAR} not set, "
            "running regex-only (names, organisations and addresses are not detected)"
        )
        return 0

    print(f"veil warmup: {_keep_loaded(base_url, model)}", flush=True)

    client = llm_client_from_env()
    if client is None:  # pragma: no cover - both variables were checked above
        return 1
    failures: list[str] = []
    try:
        spans = detect(
            _PROBE_TEXT,
            use_regex=False,
            llm_client=client,
            on_layer_error=lambda layer, exc: failures.append(
                f"{layer}: {type(exc).__name__}: {exc}"
            ),
        )
    finally:
        client.close()

    if failures:
        print(_BANNER, file=sys.stderr)
        print(
            "veil warmup: THE LLM LAYER DID NOT ANSWER. The gateway will fall back to\n"
            "regex-only and names, organisations and addresses will go out in clear.",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(_BANNER, file=sys.stderr)
        return 1
    print(f"veil warmup: LLM layer answered ({len(spans)} span(s) on the probe sentence)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
