"""Runs a demo prompt typed in the flow panel, through ``scripts/ask.sh --demo-db``.

The panel's prompt box posts here instead of the presenter switching to a
terminal. It runs the same script a terminal would, as an argv list (the
prompt is one argument, never parsed by a shell), so the turn goes through
the gateway and the hooks exactly like a typed one and shows up on the
pipeline stream by itself.

It starts a local process, so it is guarded three ways:

- off unless ``VEIL_PANEL_ASK=1`` is set (``scripts/run-gateway.sh`` sets it
  for the demo);
- the server only accepts a JSON body from the panel's own origin, so a web
  page in the same browser cannot post a prompt (a cross-origin JSON post
  needs a CORS preflight this server never grants, and the ``Origin`` header
  is checked as well);
- one run at a time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

__all__ = ["ASK_ENV_VAR", "MAX_PROMPT_CHARS", "AskRunner", "find_bash"]

ASK_ENV_VAR: Final = "VEIL_PANEL_ASK"
MAX_PROMPT_CHARS: Final = 4000
_REPO_ROOT: Final = Path(__file__).resolve().parents[3]
_ASK_SCRIPT: Final = _REPO_ROOT / "scripts" / "ask.sh"
#: Generous: a demo turn takes 1-3 minutes.
_TIMEOUT_SECONDS: Final = 600
_OUTPUT_TAIL_CHARS: Final = 4000


def find_bash() -> str | None:
    """Git Bash on Windows (never WSL's ``System32\\bash.exe``), else ``bash``."""
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    candidates = []
    if git:
        candidates.append(Path(git).resolve().parents[1] / "bin" / "bash.exe")
    candidates.append(Path(r"C:\Program Files\Git\bin\bash.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


Runner = Callable[[list[str], dict[str, str]], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, no shell; the prompt is one argument
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=_REPO_ROOT,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


class AskRunner:
    """Runs one prompt at a time in a background thread and reports its state."""

    def __init__(self, gateway_url: str, runner: Runner = _run) -> None:
        self._gateway_url = gateway_url
        self._runner = runner
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"running": False}

    @staticmethod
    def enabled() -> bool:
        return os.environ.get(ASK_ENV_VAR, "").strip() == "1"

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        state["enabled"] = self.enabled()
        if state.get("running") and "started" in state:
            state["elapsed"] = round(time.monotonic() - state["started"], 1)
        state.pop("started", None)
        return state

    def start(self, prompt: str) -> str | None:
        """Start a run; return an error message instead when it cannot start."""
        bash = find_bash()
        if bash is None:
            return "bash not found (install Git for Windows)"
        with self._lock:
            if self._state.get("running"):
                return "a prompt is already running"
            self._state = {"running": True, "started": time.monotonic(), "prompt": prompt}
        env = dict(os.environ)
        env["VEIL_GATEWAY_URL"] = self._gateway_url
        argv = [bash, str(_ASK_SCRIPT), "--demo-db", prompt]
        threading.Thread(target=self._work, args=(argv, env), daemon=True).start()
        return None

    def _work(self, argv: list[str], env: dict[str, str]) -> None:
        started = time.monotonic()
        try:
            result = self._runner(argv, env)
            outcome: dict[str, Any] = {
                "exit_code": result.returncode,
                "output": (result.stdout + result.stderr)[-_OUTPUT_TAIL_CHARS:],
            }
        except subprocess.TimeoutExpired:
            outcome = {"exit_code": None, "output": f"timed out after {_TIMEOUT_SECONDS} s"}
        except Exception as exc:  # pragma: no cover - reported to the panel, never raised
            outcome = {"exit_code": None, "output": f"could not run: {exc}"}
        with self._lock:
            self._state = {
                "running": False,
                "prompt": self._state.get("prompt", ""),
                "seconds": round(time.monotonic() - started, 1),
                **outcome,
            }
