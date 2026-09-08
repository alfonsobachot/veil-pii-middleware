"""Shared fixed path used by both hook scripts."""

from __future__ import annotations

from pathlib import Path

__all__ = ["AUDIT_LOG_PATH", "VAULT_DB_PATH"]

#: Same fixed, git-ignored vault location the (now-closed) `/veil` command
#: adapter uses -- resolved relative to the cwd, which is the project root
#: when Claude Code invokes a hook. Kept in one place so `pre_tool_use.py`
#: and `post_tool_use.py` cannot silently diverge onto two different vaults.
VAULT_DB_PATH = Path(".veil") / "vault.db"

#: Same fixed, git-ignored audit log every adapter (gateway, hooks, `/veil`)
#: appends to -- one shared file is what lets the gateway's SSE tail show
#: every adapter's activity, not just its own.
AUDIT_LOG_PATH = Path(".veil") / "audit.jsonl"
