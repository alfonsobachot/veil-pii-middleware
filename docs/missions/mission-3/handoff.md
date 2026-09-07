# Handoff

## Summary

`core/vault.py` is implemented: a per-session, AES-GCM-encrypted SQLite store
of `token <-> value` mappings per BRIEF §3.2. `Vault(db_path, session_id,
master_key=...)` binds one session at construction. Values are never stored
in plaintext — only ciphertext (AES-256-GCM) and an HMAC-SHA256 lookup digest
of the normalized value are written. The master key comes only from the
`VEIL_VAULT_KEY` environment variable (base64, 32 bytes); missing or
malformed keys raise `InvalidMasterKeyError` before any database write —
fail-closed, no partial state. Getting a token for the same normalized value
twice in a session always returns the same token; a raw value used as two
different `EntityType`s never collides. `purge_expired(ttl_seconds)` deletes
aged mappings without ever reassigning a purged sequence number. This is the
project's first runtime dependency (`cryptography`) and its first real
cryptography/secret-handling code.

`.harness/bin/verify` is green (contract, lint, typecheck, test, eval,
build). `tests/test_vault.py` has 24 tests covering round-trip crypto, HMAC
normalization consistency, cross-type non-collision, nonce uniqueness,
AAD row-swap integrity, fail-closed key handling, no-plaintext-at-rest, TTL
purge and monotonic non-reuse, and cross-session isolation. `goal-verifier`
independently confirmed all 8 numbered acceptance criteria pass.
`code-reviewer` ran a security-focused pass, found one blocking gap (missing
AEAD associated data) plus several should-fix items; all are addressed
below and the gate is re-verified green after the fixes.

### Security review findings and resolution

`code-reviewer` (fresh context, security-focused) reviewed `vault.py`,
`test_vault.py` and the `pyproject.toml` dependency addition. Findings and
what was done about each:

- **Blocking — missing AEAD associated data (fixed).** `AESGCM.encrypt`/
  `decrypt` were called with `associated_data=None`, so ciphertext was
  authenticated but not bound to the row it belongs to. Anyone with
  filesystem-level write access to the SQLite file (not the master key)
  could swap `(ciphertext, nonce)` between two `mappings` rows — same
  session or cross-session — and `resolve()` would decrypt "successfully"
  and silently return the wrong value under a given token. Fixed by binding
  AAD to `f"{session_id}:{entity_type}:{token}"` on both encrypt and
  decrypt (`vault.py`, `_aad()`), which required reordering
  `get_or_create_token` to mint the token before encrypting rather than
  after. A row-swap now raises `cryptography.exceptions.InvalidTag`,
  wrapped as the new `VaultIntegrityError` — never surfaced with any
  plaintext. Covered by the new
  `TestAadIntegrity.test_swapped_ciphertext_between_rows_fails_authentication`.
- **Should-fix — implicit `Any` from `sqlite3` row access in `resolve()`
  (fixed).** Unlike the rest of the file, `resolve()` unpacked
  `ciphertext, nonce = row` with no cast. Now explicitly cast to
  `bytes(row[0])` / `bytes(row[1])`, matching the `str(row[0])` /
  `int(row[0])` pattern used elsewhere, so `mypy --strict` actually has
  something typed to check rather than silently accepting `Any`.
- **Should-fix — single key reused for AES-GCM and HMAC (fixed).** The
  32-byte master key was used directly both as the AEAD key and the HMAC
  key — no known break, but no domain separation either. Now two
  independent subkeys are derived via HKDF-SHA256 with distinct `info`
  labels (`_derive_subkey`), so a future weakness or misuse in one
  primitive can't be leveraged against the other.
- **Should-fix — no transaction guard on concurrent writers (deferred, see
  Residual Risks).** `get_or_create_token`'s SELECT-then-INSERT and
  `_next_seq`'s SELECT-then-upsert aren't wrapped in an explicit
  transaction. Not a plaintext-leak risk, and no adapter with concurrent
  writers to the same session exists yet (out of this mission's scope per
  Non-Goals) — documented as a residual risk for whichever future adapter
  mission introduces concurrent access instead of speculatively guarding
  against it now.
- **Should-fix — `cryptography~=43.0` pin was ~7 majors behind latest
  (fixed).** Bumped to `~=50.0`, the latest major as of writing, since this
  dependency is being added fresh with no prior-version compatibility
  constraint to preserve.
- **Nit — ineffective `PRAGMA foreign_keys = ON` (fixed).** No column
  declares a `FOREIGN KEY`, so the pragma had no effect. Removed rather than
  implying enforcement that wasn't happening.
- **Nit — phone international-prefix normalization (`0034` vs `+34`) not
  collapsed (deferred, see Residual Risks).** Out of BRIEF's literal
  normalization scope (email/IBAN/DNI only); noted for follow-up if it
  surfaces as a real correlation gap in `demo/eval.py`.

## Implemented

- `veil_pii_middleware/core/vault.py` (new): `Vault` class —
  `get_or_create_token(value, entity_type)`, `resolve(token)`,
  `purge_expired(ttl_seconds, now=None)`, context-manager support
  (`__enter__`/`__exit__`/`close()`); `MASTER_KEY_ENV_VAR`,
  `InvalidMasterKeyError`, `TokenNotFoundError`; per-`EntityType`
  normalization (`_normalize`) covering all seven current entity types
  (`EMAIL, DNI, NIE, IBAN, PHONE, CARD, IP`); `mappings` and `counters`
  tables created on open.
- `pyproject.toml`: added `[project.dependencies]` with
  `cryptography~=43.0` — the project's first runtime dependency, pinned to
  one major line since it holds the master key's cipher primitive.
- `tests/test_vault.py` (new): 23 tests across round-trip, HMAC
  normalization, cross-type non-collision, nonce uniqueness, fail-closed key
  handling, no-plaintext-at-rest, TTL purge, monotonic non-reuse, and
  session isolation.

## Deviations From BRIEF (justified, called out explicitly)

1. **`counters` table**, beyond BRIEF's literal `mappings`-only schema.
   Needed because the monotonic-non-reuse-after-purge requirement (an
   explicit hard requirement from this mission's intake grill) cannot be met
   by deriving the next sequence number from `COUNT`/`MAX` over `mappings`
   alone — `purge_expired` deletes rows, which would let a freed row's
   number be handed to a new, unrelated value. The counter lives in its own
   table so it survives a purge of its own row.
2. **Composite primary key `(session_id, token)`** instead of BRIEF's literal
   `token TEXT PRIMARY KEY`. Found by a real test failure during
   implementation: the intake grill confirmed multiple sessions can share
   one `db_path` (`Vault` is bound to a session, but a process handling
   several sessions opens several `Vault` instances over the same file).
   Two different sessions each start their per-entity-type counter at 1, so
   `PII_EMAIL_0001` is only unique *within* a session — a bare `token`
   primary key caused an `sqlite3.IntegrityError` the first time two
   sessions independently minted the same token text. Covered by
   `TestSessionIsolation.test_equal_values_in_different_sessions_can_get_same_token_text`.

## Human Review Guide

1. Read `veil_pii_middleware/core/vault.py` top to bottom — it's ~250 lines,
   the whole diff for the mission's core surface.
2. Confirm the master key never appears in an exception message: every
   `InvalidMasterKeyError` in `_load_master_key` and `Vault.__init__` reports
   shape/length only, never the raw or decoded value.
3. Confirm nonce generation (`os.urandom(_NONCE_LENGTH_BYTES)` in
   `get_or_create_token`) is called fresh per call, never reused or
   derived — `TestNonceUniqueness` checks two different values get two
   different nonces, but read the code to confirm this isn't incidental.
3b. Confirm the AAD binding (`_aad()`) is actually passed to both `encrypt`
   and `decrypt` with matching inputs, and that `TestAadIntegrity` genuinely
   exercises a row-swap rather than a trivial case.
4. Run `.venv/Scripts/pytest tests/test_vault.py -v` and read the output,
   not just the exit code, per the User-Testing Validator section of
   `.harness/validation.md`.
5. Optionally inspect a generated vault file directly:
   `.venv/Scripts/python -c "from pathlib import Path; ..."` or the SQLite
   CLI, to visually confirm no plaintext is readable in `ciphertext` or
   `lookup_hmac`.
6. Review the two BRIEF deviations above (`counters` table, composite
   primary key) — both are schema changes on the project's highest-risk
   surface and warrant explicit sign-off, not just an implicit pass.

## Validation Performed

- `.venv/Scripts/pytest tests/test_vault.py -q` — 24 passed (post-fix; 23
  before the AAD integrity test was added).
- `.venv/Scripts/pytest -q` (full suite) — all passed.
- `.venv/Scripts/mypy` — Success: no issues found in 19 source files.
- `.venv/Scripts/ruff check .` — All checks passed.
- `.venv/Scripts/ruff format --check .` — 40 files already formatted.
- `.venv/Scripts/python -m build --no-isolation` — built sdist and wheel
  successfully, confirming `cryptography` resolves as a real, installable
  dependency (re-verified after bumping the pin to `~=50.0`).
- `.harness/bin/verify` (from Git Bash) — `RESULT: PASS — all 6 gate(s)
  green` (contract, lint, typecheck, test, eval, build), exit 0. Re-run
  after all security-review fixes; still green.
- Independent `goal-verifier` pass (fresh context, pre-fix code): all 8
  numbered acceptance criteria PASS with file:line evidence; also flagged
  this handoff was stale, fixed by this write.
- `code-reviewer` (security-focused pass, fresh context, pre-fix code): one
  blocking finding (missing AEAD associated data) and several should-fix
  items; all addressed above, gate re-verified green. Not re-run against
  the fixed code in this session — a human reviewer should confirm the
  fixes match what was asked for, per the Human Review Guide above.

## Residual Risks

- No keyring integration yet (deferred to a future adapter mission per the
  intake grill) — today a lost/rotated `VEIL_VAULT_KEY` makes every existing
  mapping unrecoverable, by design (there is no key-recovery path in this
  mission's scope).
- `purge_expired` is a method only; no adapter calls it yet, so a long-running
  process will accumulate mappings until something wires it to `SessionEnd`
  or a scheduler — explicitly out of scope for this mission.
- Normalization for `PHONE`/`CARD`/`IP` was extended beyond what BRIEF
  spells out (which only details email/IBAN/DNI) using the same
  strip-and-canonicalize approach; phone normalization does not collapse
  `0034`/`+34` international-prefix variants to one canonical form, so the
  same real number logged both ways could still mint two tokens. Low-impact
  today (BRIEF only mandates normalization for email/IBAN/DNI); revisit if
  it surfaces as a real gap in `demo/eval.py`.
- `get_or_create_token`'s SELECT-then-INSERT and `_next_seq`'s
  SELECT-then-upsert are not wrapped in an explicit transaction. Safe today
  since nothing writes concurrently to the same session, but the first
  adapter that allows concurrent writers to one session (e.g. two hook
  invocations racing) must add a transaction/retry guard, or two racing
  calls for the same new value could both miss the lookup and raise an
  uncaught `sqlite3.IntegrityError` on the unique `(session_id,
  lookup_hmac)` index instead of failing closed cleanly.

## Status

Implementation complete, all validation checks green, `goal-verifier` PASS,
`code-reviewer` security findings addressed (see above). Ready for human
review and merge decision.
