"""Sanitizer script invoked by the ``/veil`` slash command.

Reads the raw user prompt from stdin only -- never argv, never an
environment variable -- so no caller can smuggle prompt content into a shell
command line through this script. Tokenizes it via
``core/tokenizer.py::tokenize()`` against a per-session ``core/vault.py::Vault``
and writes the result to stdout. Detection and substitution are not
reimplemented here; this module only wires the existing `core/` layer into a
stdin/stdout transport, per BRIEF SS2.1.

Fails closed: any exception (vault open failure, tokenization failure) is
caught here, produces no stdout, and exits non-zero rather than letting the
raw, untokenized prompt through.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from veil_pii_middleware.core.audit import AuditLog
from veil_pii_middleware.core.tokenizer import tokenize
from veil_pii_middleware.core.vault import Vault

__all__ = ["main"]

#: Recorded on every audit entry this adapter produces so the observability
#: frontend (BRIEF SS5) can attribute a token to the leg that minted it.
_ADAPTER_NAME = "command"

#: Slash commands receive no session JSON on stdin (unlike PreToolUse /
#: PostToolUse hooks), so the session id comes from this env var when set --
#: falling back to a fixed id for the demo when it is not.
_SESSION_ID_ENV_VAR = "CLAUDE_SESSION_ID"
_FALLBACK_SESSION_ID = "veil-demo"

#: Fixed, git-ignored vault location for this adapter. Resolved relative to
#: the current working directory, which is the project root when Claude Code
#: invokes the slash command.
_VAULT_DB_PATH = Path(".veil") / "vault.db"

#: Same fixed, git-ignored audit log every adapter shares (see
#: adapters/hooks/_paths.py::AUDIT_LOG_PATH) -- not imported from there to
#: avoid a hooks-package dependency from this unrelated adapter; the path
#: itself must still match so the gateway's SSE tail sees this adapter's
#: entries too.
_AUDIT_LOG_PATH = Path(".veil") / "audit.jsonl"


def main() -> int:
    try:
        prompt = sys.stdin.read()
        session_id = os.environ.get(_SESSION_ID_ENV_VAR)
        if not session_id:
            # Not a fatal condition -- this is the accepted demo fallback --
            # but it silently collapses every concurrent /veil invocation
            # onto one shared vault namespace, breaking the "one mapping set
            # per session" guarantee core/vault.py documents. Surfacing it
            # here means a human running the command sees the trade-off
            # instead of it being invisible.
            print(
                f"veil: {_SESSION_ID_ENV_VAR} not set; using shared fallback "
                f"session {_FALLBACK_SESSION_ID!r} (vault mappings are not "
                "isolated per conversation)",
                file=sys.stderr,
            )
            session_id = _FALLBACK_SESSION_ID
        _VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with Vault(_VAULT_DB_PATH, session_id) as vault:
            sanitized = tokenize(
                prompt, vault, audit_log=AuditLog(_AUDIT_LOG_PATH), adapter=_ADAPTER_NAME
            )
        if not isinstance(sanitized, str):
            # tokenize() always returns a str for a str input; this only
            # guards mypy's narrowing and would indicate a core/ contract
            # break, not a normal runtime path.
            raise TypeError("tokenize() did not return a string for a string input")
    except Exception:
        print("veil: sanitization failed, blocking prompt", file=sys.stderr)
        return 1
    sys.stdout.write(sanitized)
    return 0


if __name__ == "__main__":
    sys.exit(main())
