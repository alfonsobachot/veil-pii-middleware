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

## Data disclaimer

The demo database is fully synthetic, generated with Faker under a fixed seed.
It contains no real personal data. The vault, generated databases and audit
logs are git-ignored and must never be committed.
