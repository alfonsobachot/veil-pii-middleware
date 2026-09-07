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

## Data disclaimer

The demo database is fully synthetic, generated with Faker under a fixed seed.
It contains no real personal data. The vault, generated databases and audit
logs are git-ignored and must never be committed.
