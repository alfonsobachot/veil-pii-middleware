"""Shared demo-scope check used by both hook scripts.

Not a dispatch mechanism -- just a single, safety-relevant check that both
``pre_tool_use.py`` and ``post_tool_use.py`` must agree on, kept in one place
so the two scripts cannot silently drift apart on what counts as "in scope".

This is a bare substring check, not proof that a command *only* invokes
`sqlite3` against the demo db -- a compound/chained command containing the
substring also matches. It is only ever used to decide whether to *rewrite*
`tool_input`/`tool_response`, never to auto-approve a command: neither
script emits `permissionDecision: "allow"`, so a match here still leaves
Claude Code's normal ask-permission flow in charge of whether the (possibly
rewritten) command actually runs.
"""

from __future__ import annotations

__all__ = ["is_demo_db_command"]

#: The demo database's literal filename (see ``demo/veil_demo.db``). A bare
#: substring check is deliberately the whole strategy here: this project's
#: demo has exactly one database, invoked exactly one way (``sqlite3`` over
#: Bash, per BRIEF SS4), so a broader classifier would be solving a problem
#: this demo does not have.
_DEMO_DB_FILENAME = "veil_demo.db"


def is_demo_db_command(command: str) -> bool:
    """Return whether ``command`` looks like a query against the demo database."""
    return _DEMO_DB_FILENAME in command
