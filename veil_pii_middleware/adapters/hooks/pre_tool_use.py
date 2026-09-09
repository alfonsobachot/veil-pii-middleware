"""``PreToolUse`` hook: de-tokenizes a demo-db Bash command before it runs.

Reads the hook's JSON payload from stdin only. When the tool call is a
``Bash`` invocation referencing the demo database (see
``adapters/hooks/_scope.py``), de-tokenizes ``tool_input["command"]`` via
``core/tokenizer.py::detokenize()`` against a per-session
``core/vault.py::Vault`` and returns the result as ``updatedInput`` so the
real value -- not the literal ``PII_<TYPE>_<NNNN>`` placeholder -- is what
actually executes. Every other tool call passes through untouched.

Fails closed: any failure to de-tokenize a demo-db command (vault won't
open, unknown/foreign/malformed token) blocks the call (exit 2) rather than
letting a literal token string execute against the real database. This is
not a PII-leak risk -- the risk on this side is a broken command -- but
CLAUDE.md's fail-closed doctrine applies uniformly.

Deliberately never sets ``permissionDecision`` on the success path either --
only ``updatedInput``. ``is_demo_db_command`` is a bare substring check, not
a validator that the command is *only* an `sqlite3` invocation against the
demo db; a compound/chained command (e.g. `` rm -rf ~; sqlite3
demo/veil_demo.db "select 1"` ``) would also match it. Pairing that with
``permissionDecision: "allow"`` would auto-approve the *entire* command and
skip Claude Code's normal ask-permission prompt -- the same failure class
the (now-closed) `/veil` mission already hit once and fixed by removing its
`allowed-tools` wildcard grant (see `docs/missions/mission-5/handoff.md`).
Leaving `permissionDecision` unset here means the rewritten command still
goes through that same normal ask-permission flow before it runs.
"""

from __future__ import annotations

import json
import sys

from veil_pii_middleware.adapters.hooks._panel_report import report
from veil_pii_middleware.adapters.hooks._paths import VAULT_DB_PATH
from veil_pii_middleware.adapters.hooks._scope import is_demo_db_command
from veil_pii_middleware.core.tokenizer import JSONValue, detokenize
from veil_pii_middleware.core.vault import Vault

__all__ = ["main"]


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
    except Exception:
        print("pre_tool_use: malformed hook payload, blocking", file=sys.stderr)
        return 2

    tool_input = payload.get("tool_input")
    if payload.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str) or not is_demo_db_command(command):
        return 0

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        print("pre_tool_use: missing session_id, blocking", file=sys.stderr)
        return 2

    # Best-effort only -- see adapters/hooks/_panel_report.py's own
    # docstring. Never raises, never affects anything below.
    report(session_id, "pre_tool_use", "start", "Bash")

    try:
        VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with Vault(VAULT_DB_PATH, session_id) as vault:
            detokenized = detokenize(command, vault)
    except Exception:
        print("pre_tool_use: detokenization failed, blocking", file=sys.stderr)
        report(session_id, "pre_tool_use", "error", "Bash")
        return 2

    if not isinstance(detokenized, str):
        # detokenize() always returns a str for a str input; this only
        # guards mypy's narrowing and would indicate a core/ contract break.
        print("pre_tool_use: detokenization failed, blocking", file=sys.stderr)
        report(session_id, "pre_tool_use", "error", "Bash")
        return 2

    updated_input: JSONValue = {**tool_input, "command": detokenized}
    output: JSONValue = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated_input,
        }
    }
    sys.stdout.write(json.dumps(output))
    report(session_id, "pre_tool_use", "done", "Bash")
    return 0


if __name__ == "__main__":
    sys.exit(main())
