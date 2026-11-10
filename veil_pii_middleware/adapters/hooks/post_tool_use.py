"""``PostToolUse`` hook: tokenizes a demo-db Bash command's output.

Reads the hook's JSON payload from stdin only. When the tool call is a
``Bash`` invocation referencing the demo database (see
``adapters/hooks/_scope.py``), tokenizes the whole ``tool_response`` value
via ``core/tokenizer.py::tokenize()`` against a per-session
``core/vault.py::Vault`` and returns it as ``updatedToolOutput``. Every other
tool call passes through untouched.

Deliberately generic: Claude Code's official hooks reference does not fully
document ``tool_response``'s shape for ``Bash`` (only one unconfirmed
example was seen: ``{stdout, stderr, exit_code}``), so this hook never
assumes a field name -- it hands the whole value to ``tokenize()``, which
already recurses through dicts/lists and only touches string leaves. Whatever
the real shape turns out to be, every string in it gets scanned.

``PostToolUse`` cannot block (the tool has already run, per the official
hooks reference). On any failure this hook prints a stderr diagnostic and
exits non-zero, but the tool's original, untokenized response still reaches
the model on that path -- an accepted, documented gap,
not something fixable at this layer.
"""

from __future__ import annotations

import json
import sys

from veil_pii_middleware.adapters.hooks._panel_report import report
from veil_pii_middleware.adapters.hooks._paths import AUDIT_LOG_PATH, VAULT_DB_PATH
from veil_pii_middleware.adapters.hooks._scope import is_demo_db_command
from veil_pii_middleware.core.audit import AuditLog
from veil_pii_middleware.core.tokenizer import JSONValue, tokenize
from veil_pii_middleware.core.vault import Vault

__all__ = ["main"]

#: Recorded on every audit entry this hook produces so the observability
#: frontend can attribute a token to the leg that minted it.
_ADAPTER_NAME = "hooks:post_tool_use"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
    except Exception:
        print("post_tool_use: malformed hook payload", file=sys.stderr)
        return 1

    tool_input = payload.get("tool_input")
    if payload.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str) or not is_demo_db_command(command):
        return 0

    if "tool_response" not in payload:
        print("post_tool_use: payload has no tool_response, nothing to tokenize", file=sys.stderr)
        return 1
    tool_response = payload["tool_response"]

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        print("post_tool_use: missing session_id", file=sys.stderr)
        return 1

    # Best-effort only -- see adapters/hooks/_panel_report.py's own
    # docstring. Never raises, never affects anything below.
    report(session_id, "post_tool_use", "start", "Bash")

    try:
        VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with Vault(VAULT_DB_PATH, session_id) as vault:
            tokenized = tokenize(
                tool_response,
                vault,
                audit_log=AuditLog(AUDIT_LOG_PATH),
                adapter=_ADAPTER_NAME,
            )
    except Exception:
        print("post_tool_use: tokenization failed", file=sys.stderr)
        report(session_id, "post_tool_use", "error", "Bash")
        return 1

    output: JSONValue = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": tokenized,
        }
    }
    sys.stdout.write(json.dumps(output))
    report(session_id, "post_tool_use", "done", "Bash")
    return 0


if __name__ == "__main__":
    sys.exit(main())
