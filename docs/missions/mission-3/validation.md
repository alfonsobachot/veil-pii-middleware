# Validation Contract

Write this before implementation starts.

## Gate

The required gate is:

```bash
.harness/bin/verify
```

Exit codes:

- `0` = pass
- `1` = fail
- `2` = unconfigured

## Executable Checks

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore`, no implicit `Any` on rows/bytes/ciphertext |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Build | `.venv/Scripts/python -m build --no-isolation` | exit `0` (confirms `cryptography` resolves as a real, installable dependency) |
| Detector recall/decoys (unchanged) | `eval` GATES row: seed, then `python -m veil_pii_middleware.demo.eval --min-recall 1.0 --max-false-positives 0` | exit `0`, unmodified from mission-2 |
| Round-trip crypto | pytest: encrypt a value via `get_or_create_token`, `resolve()` returns the exact original value | exit `0` |
| HMAC normalization consistency | pytest, parametrized per `EntityType`: two differently-formatted-but-equivalent values (case/whitespace) produce the same `lookup_hmac` and the same token | exit `0` |
| Cross-type non-collision | pytest: the same raw string used as two different `EntityType`s never shares a token or a `lookup_hmac` row | exit `0` |
| Nonce uniqueness | pytest: two `get_or_create_token` calls for two different values produce two different nonces and two different ciphertexts, even under a fixed key | exit `0` |
| Fail-closed on bad key | pytest, parametrized: `VEIL_VAULT_KEY` unset, empty, not valid base64, and valid base64 but not 32 bytes — each raises before any DB write, no partial file created | exit `0` |
| No plaintext at rest | pytest: after inserting a fixture value, read the raw SQLite file bytes and assert the plaintext fixture value does not appear | exit `0` |
| TTL purge | pytest, injectable clock: a mapping older than `ttl_seconds` is deleted by `purge_expired()`, one that is newer is kept, return value equals the count deleted | exit `0` |
| Monotonic non-reuse after purge | pytest: purge a mapping, insert a new value of the same `entity_type` in the same session, assert its `NNNN` is strictly greater than the purged one's, never reused | exit `0` |
| Session isolation | pytest: `resolve()` on a token that exists only in a different `session_id` raises rather than returning cross-session data | exit `0` |

## Scrutiny Validator

- Confirm the diff touches only `veil_pii_middleware/core/vault.py`,
  `pyproject.toml`, `tests/test_vault.py`, and the harness mission files —
  `detector.py`, `tokenizer.py`, `audit.py`, adapters and frontend stay
  untouched.
- Confirm no plaintext value is ever passed to a `str`/log/exception message
  that could reach stdout, a log file, or `.harness` memory — check every
  `raise` and any debug-adjacent code path.
- Confirm the master key is read only from `VEIL_VAULT_KEY` and never
  hardcoded, defaulted to a constant, or written to disk anywhere (including
  test fixtures — tests should set the env var to a locally generated key,
  never commit a literal key).
- Confirm nonces are generated with `os.urandom` (or an equivalent CSPRNG)
  per call, never derived, reused, or zero-filled.
- Confirm the unique index is genuinely `(session_id, lookup_hmac)` — a
  missing `session_id` component would let two sessions' identical values
  collide on the same row.
- Confirm `mypy --strict` passes with no new `# type: ignore` introduced to
  paper over `sqlite3`'s loosely-typed row access.
- Confirm the `counters` table addition is documented in the mission/handoff
  as an explicit, justified deviation from BRIEF's literal `mappings`-only
  schema, not silent scope creep.

## User-Testing Validator

- Run `tests/test_vault.py` directly and read the output — not just the exit
  code — to confirm the assertions actually exercise ciphertext bytes and
  HMAC values rather than only checking token strings.
- Manually inspect a test-generated vault DB file (e.g. via `sqlite3` CLI or
  a short throwaway script) to visually confirm no fixture plaintext value
  is readable in the `ciphertext` or `lookup_hmac` columns.
- Confirm `pip install -e ".[dev]"` succeeds cleanly with the new
  `cryptography` dependency in this repo's `.venv` on Windows.
