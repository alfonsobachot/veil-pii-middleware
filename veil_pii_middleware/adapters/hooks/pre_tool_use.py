"""``PreToolUse`` hook: de-tokenizes a demo-db shell command before it runs.

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
the `/veil` command already hit once and fixed by removing its
`allowed-tools` wildcard grant.
Leaving `permissionDecision` unset here means the rewritten command still
goes through that same normal ask-permission flow before it runs.
"""

from __future__ import annotations

import json
import re
import sys

from veil_pii_middleware.adapters.hooks._panel_report import report
from veil_pii_middleware.adapters.hooks._paths import VAULT_DB_PATH
from veil_pii_middleware.adapters.hooks._scope import SHELL_TOOLS, is_demo_db_command
from veil_pii_middleware.core.tokenizer import JSONValue, detokenize
from veil_pii_middleware.core.vault import Vault

__all__ = ["force_python_utf8", "main"]

#: A Python interpreter in command position: at the start, or right after a
#: shell separator (``;``, ``&&``, ``||``, ``|``, ``(``, ``$(``, newline) -- not
#: an argument, as in ``which python``.
_PYTHON_CALL = re.compile(r"(?:^|(?<=[;&|(\n]))\s*(?:python3?|py)(?:\.exe)?(?=\s)")


def force_python_utf8(command: str) -> str:
    """Add ``-X utf8`` to every Python interpreter call in ``command``.

    On Windows a piped Python prints in the ANSI code page, so ``Lucía``
    reached Claude Code as ``Luc\\ufffda``: unreadable, and the detector then
    tokenized only ``Ferrer Montoya`` and let the broken first name through
    (live runs, 2026-11-11). ``ask.sh`` sets ``PYTHONUTF8=1``, but the model
    often calls ``python -I``, which ignores every ``PYTHON*`` variable; the
    ``-X utf8`` flag still applies under ``-I``.
    """
    if "-X utf8" in command:
        return command
    return _PYTHON_CALL.sub(lambda m: f"{m.group(0)} -X utf8", command)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
    except Exception:
        print("pre_tool_use: malformed hook payload, blocking", file=sys.stderr)
        return 2

    tool_input = payload.get("tool_input")
    tool_name = payload.get("tool_name")
    if tool_name not in SHELL_TOOLS or not isinstance(tool_input, dict):
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str) or not is_demo_db_command(command):
        return 0

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        print("pre_tool_use: missing session_id, blocking", file=sys.stderr)
        return 2

    # report() below is best-effort only -- see adapters/hooks/_panel_report.py.
    # It never raises and never affects the hook's result.
    try:
        VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with Vault(VAULT_DB_PATH, session_id) as vault:
            detokenized = detokenize(command, vault)
    except Exception:
        print("pre_tool_use: detokenization failed, blocking", file=sys.stderr)
        report(session_id, "pre_tool_use", "error", str(tool_name), before=command)
        return 2

    if not isinstance(detokenized, str):
        # detokenize() always returns a str for a str input; this only
        # guards mypy's narrowing and would indicate a core/ contract break.
        print("pre_tool_use: detokenization failed, blocking", file=sys.stderr)
        report(session_id, "pre_tool_use", "error", str(tool_name), before=command)
        return 2

    detokenized = force_python_utf8(detokenized)
    updated_input: JSONValue = {**tool_input, "command": detokenized}
    output: JSONValue = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated_input,
        }
    }
    sys.stdout.write(json.dumps(output))
    # The panel aligns before and after; both carry the same -X utf8 so only tokens differ.
    shown_before = force_python_utf8(command)
    report(
        session_id, "pre_tool_use", "done", str(tool_name), before=shown_before, after=detokenized
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
