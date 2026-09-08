"""Shared pytest fixtures for the whole suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from veil_pii_middleware.core.detector import LLM_BASE_URL_ENV_VAR, LLM_MODEL_ENV_VAR

_LLM_ENV_VARS = (LLM_BASE_URL_ENV_VAR, LLM_MODEL_ENV_VAR)


@pytest.fixture(autouse=True)
def _no_ambient_llm_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip VEIL_LLM_BASE_URL/VEIL_LLM_MODEL from the environment for every test.

    A developer may have these set persistently (e.g. a personal
    `.claude/settings.local.json` pointing at their own LM Studio instance,
    the way this project's own contributor does for manual demo testing).
    Without this fixture, any test that omits an explicit `llm_client` --
    which is most of the suite, since `tokenize()`/`detect()` default to
    building one from the environment -- would silently pick up that
    ambient config and produce environment-dependent results (an
    unexpected `PERSON` token, an extra audit entry, a subprocess adapter
    test's exact-output assertion failing) purely because of who happens to
    run the suite, not because of a real regression. Every test that
    deliberately wants the LLM layer already opts in explicitly via
    `monkeypatch.setenv(...)`, which layers correctly on top of this.

    Also isolates subprocess-based adapter tests (`test_hooks.py`,
    `test_sanitize.py`), whose `_run()` helpers copy `os.environ` for the
    child process: `monkeypatch.delenv` mutates the real `os.environ`, so
    that copy is made without these keys too.
    """
    for name in _LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
