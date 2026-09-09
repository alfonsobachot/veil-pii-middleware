"""Best-effort progress report from a hook script to the gateway's panel (mission-13).

Shared by ``pre_tool_use.py``/``post_tool_use.py`` so both scripts call the
exact same, tightly-scoped reporting primitive rather than two independently
drifting HTTP calls. This is purely additive observability, never load-
bearing: ``report()`` never raises and its return value carries no
information a caller could branch on -- neither hook script checks anything
after calling it, and neither script's exit code, ``updatedInput``/
``updatedToolOutput`` payload, or fail-closed blocking behavior changes
whether the gateway is reachable, running at all, or configured differently
than the hook expects. A short, fixed timeout keeps a slow/unreachable
gateway from meaningfully delaying the hook itself.

Deliberately excludes anything that could carry a command string, a tool
response, or a tokenized/detokenized value: the only fields this module ever
sends are ``session_id``, ``hook``, ``phase``, and a static ``tool_name`` --
see ``adapters/gateway/server.py::_serve_hook_event``'s matching schema and
the mission's Non-goals for why this must be true by construction, not by
convention (``pre_tool_use.py`` in particular exists specifically to produce
a command containing the *real, detokenized* PII value the local DB needs to
see, so nothing derived from that command may ever reach this module).
"""

from __future__ import annotations

import contextlib
import os

import httpx

__all__ = ["report"]

#: Same ``127.0.0.1``-only, single-gateway-process demo trust model every
#: other panel route already assumes; matches ``run-gateway.sh``'s own
#: default port. Overridable so a hook still finds the gateway if it was
#: started on a non-default port, mirroring ``VEIL_VAULT_KEY``'s env var
#: convention.
_GATEWAY_URL_ENV_VAR = "VEIL_GATEWAY_URL"
_DEFAULT_GATEWAY_URL = "http://127.0.0.1:8787"

#: Combined connect+read budget. Short enough that an unreachable gateway
#: costs a hook invocation a bounded, sub-second delay rather than hanging
#: it -- this is a live progress signal, not a delivery-guaranteed call.
_TIMEOUT_SECONDS = 0.2


def report(session_id: str, hook: str, phase: str, tool_name: str) -> None:
    """POST one best-effort progress event; never raises, never blocks meaningfully.

    Any failure -- gateway not running, wrong port, network hiccup, non-2xx
    response -- is swallowed identically and silently. Callers must not
    (and do not) treat the absence of an exception as confirmation the
    gateway actually received or displayed anything.
    """
    base_url = os.environ.get(_GATEWAY_URL_ENV_VAR, "").strip() or _DEFAULT_GATEWAY_URL
    with contextlib.suppress(Exception):
        httpx.post(
            f"{base_url.rstrip('/')}/veil-panel/hook-event",
            json={
                "session_id": session_id,
                "hook": hook,
                "phase": phase,
                "tool_name": tool_name,
            },
            timeout=_TIMEOUT_SECONDS,
        )
