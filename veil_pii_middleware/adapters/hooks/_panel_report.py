"""Best-effort report from a hook script to the gateway's panel.

Shared by ``pre_tool_use.py``/``post_tool_use.py`` so both scripts call the
exact same reporting primitive rather than two independently drifting HTTP
calls. Purely additive observability, never load-bearing: ``report()`` never
raises and returns nothing a caller could branch on, so neither hook's exit
code, ``updatedInput``/``updatedToolOutput`` payload or fail-closed blocking
depends on whether the gateway is reachable.

Carries the hook's before/after text (option B, H8 in
``docs/review-findings.md``): ``PreToolUse`` sends the tokenized command and
the detokenized one it hands to the shell; ``PostToolUse`` sends the raw tool
output and its tokenized form. The detokenized command and the raw output hold
real values, so this is only acceptable because the panel is served on
``127.0.0.1`` alone and the gateway puts these fields on its in-memory panel
bus only -- never in ``audit.jsonl``, never upstream. Each text is truncated to
``TEXT_LIMIT`` characters.

One call per hook invocation, after the hook's work is done (H7): the gateway
is on the same machine, so ``trust_env=False`` keeps any ``HTTP(S)_PROXY`` out
of the way and a short timeout bounds the delay when it is not running.
"""

from __future__ import annotations

import contextlib
import json
import os

import httpx

__all__ = ["TEXT_LIMIT", "display_text", "report"]

#: Same ``127.0.0.1``-only demo trust model every other panel route assumes;
#: matches ``run-gateway.sh``'s default port. Overridable for a gateway started
#: on another port.
_GATEWAY_URL_ENV_VAR = "VEIL_GATEWAY_URL"
_DEFAULT_GATEWAY_URL = "http://127.0.0.1:8787"

#: Connect+read budget. A refused connection on loopback returns at once; this
#: only bounds a gateway that is up but stalled.
_TIMEOUT_SECONDS = 0.3

#: Characters kept of each before/after text. Two of them, UTF-8 encoded, stay
#: under the gateway's 64 KB panel body limit.
TEXT_LIMIT = 6000


def display_text(value: object) -> str:
    """Render a hook value (a command string or a tool response) as panel text.

    A Bash ``tool_response`` is a dict with ``stdout``/``stderr``; its non-empty
    string fields are joined. Anything else falls back to compact JSON.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        streams = [value.get(key) for key in ("stdout", "stderr")]
        joined = "\n".join(s for s in streams if isinstance(s, str) and s)
        if joined:
            return joined
    with contextlib.suppress(TypeError, ValueError):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def report(
    session_id: str,
    hook: str,
    phase: str,
    tool_name: str,
    *,
    before: str | None = None,
    after: str | None = None,
) -> None:
    """POST one best-effort event; never raises, never blocks meaningfully.

    Any failure -- gateway not running, wrong port, non-2xx -- is swallowed.
    Callers must not treat a normal return as proof the panel received it.
    """
    base_url = os.environ.get(_GATEWAY_URL_ENV_VAR, "").strip() or _DEFAULT_GATEWAY_URL
    payload: dict[str, str] = {
        "session_id": session_id,
        "hook": hook,
        "phase": phase,
        "tool_name": tool_name,
    }
    if before is not None:
        payload["before"] = before[:TEXT_LIMIT]
    if after is not None:
        payload["after"] = after[:TEXT_LIMIT]
    with contextlib.suppress(Exception), httpx.Client(trust_env=False) as client:
        client.post(
            f"{base_url.rstrip('/')}/veil-panel/hook-event",
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=_TIMEOUT_SECONDS,
        )
