# veil-pii-middleware

PII sanitization middleware for Claude Code, built around a tokenization /
de-tokenization cycle. No personal data in clear text ever leaves the machine
towards an external API, while local tools — database queries, file reads —
keep operating on the real values.

## Status

Skeleton only. No logic is implemented yet. This README will grow into the
full explanation of the design as the implementation lands.

## Layout

```
veil_pii_middleware/
  core/       detector, vault, tokenizer, audit
  adapters/   command (/ask), hooks, gateway
  frontend/   live observability panel
  demo/       seed (database + manifest), eval (recall / precision)
```

## Requirements

Python 3.11+.

## Development

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on POSIX
pip install -e ".[dev]"
```

Lint and tests:

```bash
ruff check .
ruff format --check .
pytest
```

## Running the demo (hooks)

`PreToolUse`/`PostToolUse` hooks de-tokenize and tokenize `Bash` calls
against the demo database (`demo/veil_demo.db`) so a Claude Code session can
query it without any raw PII reaching the model. They are **not** active by
default — activate them for a demo run with:

```bash
demo/run_demo.sh
```

(from Git Bash; this wraps `claude --settings demo/settings.hooks.json` and
also locates the `claude` binary for you if it's only installed as the
VSCode extension's bundled copy, not on `PATH`.) `VEIL_VAULT_KEY` must be
set first (see `core/vault.py`). The `--settings` flag merges
additively per top-level key into the session's settings, but the `hooks`
key itself is replaced wholesale for that session, so this repo's own
meta-development hooks (`.claude/settings.json`'s `protect-paths.sh`,
`mission-state-guard.sh`, etc.) do not run while it's active — an accepted
trade-off, since a demo run doesn't edit repo files. Do not merge
`demo/settings.hooks.json`'s contents into `.claude/settings.json` or
`.claude/settings.local.json.example`: doing so would apply the `Bash`
matcher to every Bash call in every future session on this repo, not just
demo-db queries.

`--settings` is a CLI startup flag: it must be run from a real terminal.
The VSCode extension's own panel/native UI does not expose a way to pass it
(checked its `contributes.configuration` — no such setting exists), so a
session already running inside the extension's panel can't switch this on;
start a separate terminal session instead.

## Running the demo (gateway)

The gateway is the mechanism that satisfies the project's core requirement
(zero raw PII to an external API) for the *whole* conversation, not just
`Bash` calls against the demo database. Two scripts drive it non-interactively
for testing, without needing the VSCode extension or a manual `claude`
session:

```bash
scripts/run-gateway.sh [port]        # default 8787; generates/reuses .veil/vault.key
scripts/ask.sh "your message"        # plain gateway test, no demo-db hooks
scripts/ask.sh --demo-db "your message"   # full pipeline: gateway + PreToolUse/PostToolUse
```

`--demo-db` is the one that exercises the target scenario end-to-end: it
tokenizes your message before it reaches the model, lets the model query
`demo/veil_demo.db` with only the token available to it, de-tokenizes the
`Bash` call so the real query runs locally, and re-tokenizes the result
before it re-enters the model's context. It runs from an isolated scratch
directory with no `CLAUDE.md` of its own (see below for why), and prepends a
system-prompt note telling the model that `PII_<TYPE>_<NNNN>` tokens are
expected and correct so it doesn't hedge or add disclaimers about them.

Override the gateway URL with `VEIL_GATEWAY_URL` (matching whatever port
`run-gateway.sh` used); add extra instructions with `VEIL_APPEND_SYSTEM_PROMPT`.

**Known gotchas, found and fixed while building this test path:**

- **`VEIL_VAULT_KEY` must always come from `.veil/vault.key`.** A stale value
  already exported in your shell (from an earlier session) silently wins over
  the current key file if a script only loads it "when unset" — the fix
  (`scripts/ask.sh`) always reloads it fresh. If you see
  `VaultIntegrityError`/"detokenization failed" on every single command
  regardless of which token is involved, this is the first thing to check.
- **`claude`'s CLAUDE.md auto-discovery walks up parent directories**, not
  just the exact working directory — even starting a session from `demo/`
  still loads this repo's own `CLAUDE.md`. For `--demo-db`'s test subprocess,
  that is a real problem: the subprocess reads a document describing a
  PII-blocking system watching *it*, and can start reasoning about itself as
  "the external LLM being guarded against" instead of just running the tool
  call. `demo/settings.hooks.isolated.json` plus `$VEIL_REPO_ROOT` (hook
  commands `cd` into it themselves) solves this by running from a directory
  with no repository ancestry at all, while still sharing the same vault. In
  a real deployment this never comes up — the `CLAUDE.md` the model reads
  would belong to whatever project is actually being debugged, not to this
  middleware.
- **A message containing an absolute filesystem path gets scanned for PII
  like everything else.** This machine's username inside a path can read as
  a person's name to the LLM detection layer, corrupting the very path the
  model needs. `--demo-db` avoids this by hard-linking `demo/veil_demo.db`
  into the scratch directory so a plain relative path always works, rather
  than telling the model an absolute one.
- **`sqlite3` may not be installed as a CLI binary** on a given machine, even
  though Python's `sqlite3` module always is. If a query fails with `command
  not found`, ask for the query via Python's `sqlite3` module instead of the
  CLI.
- **The model's own narration of what it just did is not reliable evidence.**
  Across many test runs, its prose sometimes claimed hooks were inactive, or
  that the WHERE-clause token didn't match the result, when the vault's own
  data showed otherwise every time. Trust the audit log and vault contents
  (`.veil/audit.jsonl`, `.veil/vault.db`) over what the model says happened.

## Data disclaimer

The demo database is fully synthetic, generated with Faker under a fixed seed.
It contains no real personal data. The vault, generated databases and audit
logs are git-ignored and must never be committed.
