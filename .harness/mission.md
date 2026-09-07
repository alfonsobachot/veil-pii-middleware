# Mission

## Objective

Implement `core/vault.py`: a per-session, encrypted SQLite store of
`token <-> value` mappings per BRIEF §3.2. The vault answers two questions
without ever touching plaintext at rest: "do I already have a token for this
normalized value?" (via an HMAC-SHA256 lookup) and "what was the original
value behind this token?" (via AES-GCM decryption, used only by local tools
that need the real value back). The master key never comes from the
repository. The same normalized value always yields the same token within a
session — the hard correlation requirement the whole tokenizer/gateway
pipeline depends on.

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

## Intake Grill

| Question | Answer / Evidence |
|---|---|
| Which AES-GCM library, given the project has zero runtime dependencies today? | `cryptography` (pyca). Industry-standard AEAD API (`AESGCM`), maintained, prebuilt wheels for Windows/py3.12 — this becomes the project's first runtime dependency, added to `pyproject.toml` `[project.dependencies]`. |
| BRIEF says the master key comes from "environment variable or keyring" — implement both now? | No — env var only this mission (`VEIL_VAULT_KEY`, base64-encoded 32-byte / 256-bit key for AES-256-GCM). Missing or malformed key raises immediately (fail-closed, per `CLAUDE.md`). Keyring support is deferred to a future adapter mission when there's a real cross-session persistence need — it would add an optional dependency this already-high-risk mission doesn't need. |
| How does `Vault` relate to `session_id`, given it's "one store per session"? | `Vault(db_path, master_key, session_id)` binds the session at construction. All methods (`get_or_create_token`, `resolve`, `purge_expired`) operate implicitly on that session. A process handling multiple sessions constructs multiple `Vault` instances over the same `db_path`. |
| After a TTL purge deletes a mapping, can its `NNNN` be reassigned to a new value of the same `entity_type`? | No — monotonic per `(session_id, entity_type)`, never reused within a session. Reassigning a purged number would let `PII_EMAIL_0003` mean two different values at two different points in the same session, which would corrupt correlation for anything already tokenized against it (a transcript, a cached prompt, an audit log entry). This requires a small `counters` table beyond the `mappings` table BRIEF names, since the next-sequence value must survive a purge of its own row — `COUNT`/`MAX` over `mappings` alone cannot, since purge deletes rows. |

Evidence gathered from the repo before asking: `veil_pii_middleware/core/vault.py`
is a docstring-only stub; `veil_pii_middleware/core/detector.py`'s `EntityType`
enum currently has seven members (`EMAIL, DNI, NIE, IBAN, PHONE, CARD, IP`), so
normalization must cover all seven, not only the two BRIEF spells out
(email, IBAN/DNI); `pyproject.toml` has no `[project.dependencies]` yet;
`.gitignore` already ignores `vault/`, `*.vault`, `*.vault.db`, so no new
ignore rule is needed as long as test fixtures use a temp path.

## Scope

- `veil_pii_middleware/core/vault.py`: `Vault` class — schema creation
  (`mappings` + `counters` tables, the unique `(session_id, lookup_hmac)`
  index), `MasterKey` resolution from `VEIL_VAULT_KEY`, per-entity-type value
  normalization, AES-GCM encrypt/decrypt, HMAC-SHA256 lookup,
  `get_or_create_token()`, `resolve(token)`, `purge_expired(ttl_seconds)`,
  context-manager support (`__enter__`/`__exit__`/`close()`).
- `pyproject.toml`: add `cryptography` under a new
  `[project.dependencies]` (or equivalent) with a pinned minor range,
  matching the existing pinning convention used for `faker`.
- `tests/test_vault.py` (new): round-trip encrypt/decrypt: HMAC lookup
  consistency across equivalent normalized values (case, whitespace); no
  collision between two different entity types sharing a raw value; TTL
  purge behavior with an injectable clock; monotonic non-reuse of `NNNN`
  after purge; fail-closed behavior on a missing/malformed master key.
- `.harness/validation.md`, `.harness/handoff.md`.

## Non-Goals

- `core/tokenizer.py` — substitution/reversal over text and structures — is
  untouched. This mission only makes the vault's `get_or_create_token()` /
  `resolve()` API available to it.
- `core/audit.py` is untouched.
- No adapter work (`adapters/command`, `adapters/hooks`, `adapters/gateway`)
  and no frontend work.
- No keyring integration in this mission (see Intake Grill above).
- No automatic purge scheduling / `SessionEnd` hook wiring — this mission
  only provides the `purge_expired()` method a future adapter will call.
- No change to `core/detector.py`'s `EntityType` enum or detection logic.

## Acceptance Criteria

1. `Vault` creates `mappings(token, entity_type, lookup_hmac, ciphertext,
   nonce, session_id, created_at)` with a unique index on
   `(session_id, lookup_hmac)`, plus a `counters` table for monotonic
   per-`(session_id, entity_type)` sequence numbers.
2. `get_or_create_token(value, entity_type)` returns a token formatted
   `PII_<TYPE>_<NNNN>`; calling it twice with equivalent normalized values
   (e.g. differently-cased emails, an IBAN with/without spaces) returns the
   same token within a session, and never writes plaintext to any column.
3. `resolve(token)` returns the original plaintext value via AES-GCM
   decryption; a token from a different session or an unknown token raises.
4. Two values that are byte-identical but belong to different `EntityType`s
   never collide on the same token or the same `lookup_hmac` row.
5. Missing or malformed `VEIL_VAULT_KEY` (wrong length after base64 decode,
   not set at all) raises before any database write — fail-closed, no
   partial state.
6. `purge_expired(ttl_seconds)` deletes mappings older than the TTL and
   returns the count deleted; a value purged and re-submitted gets a new,
   higher `NNNN` in the same session, never a reused number.
7. `tests/test_vault.py` passes and covers: round-trip crypto, HMAC
   normalization consistency, cross-type non-collision, TTL purge, and
   fail-closed key handling.
8. `.harness/bin/verify` stays green — lint, typecheck (`strict`, no new
   `Any`), tests, build, and the unchanged `eval` gate.

## Tooling And Approach Assessment

1. **What project type is this?** Core cryptographic storage component in a
   pure-Python package; first module in the project to touch a real secret
   (the master key) and to hold ciphertext at rest.

2. **What methods, tooling, or approaches does that imply?**
   `cryptography.hazmat.primitives.ciphers.aead.AESGCM` for encrypt/decrypt;
   stdlib `hmac` + `hashlib.sha256` for the lookup digest; stdlib `sqlite3`
   for storage; `os.urandom(12)` for GCM nonces (96-bit, the standard AEAD
   nonce size — never reused per key, which is safe here since a fresh
   random nonce accompanies every encryption call); `base64` for decoding
   `VEIL_VAULT_KEY`; per-`EntityType` normalization functions mirroring the
   ones BRIEF names for email/IBAN/DNI, extended to the other four
   (`NIE, PHONE, CARD, IP`) using the same strip-whitespace-and-canonicalize
   logic.

3. **What risks or missing decisions does it reveal?**
   - Nonce uniqueness is the load-bearing GCM safety property: reusing a
     nonce under the same key breaks confidentiality outright. Using
     `os.urandom(12)` per call and never persisting/reusing a nonce across
     rows is the mitigation; a test asserts two encryptions of the same
     value produce different nonces and different ciphertexts.
   - The `counters` table is an addition beyond BRIEF's literal `mappings`
     schema, needed to satisfy the monotonic-non-reuse requirement the user
     set as a hard requirement — called out explicitly above and in the
     handoff so a reviewer doesn't read it as scope creep.
   - `mypy --strict`: `sqlite3` row access returns loosely-typed tuples;
     row-to-dataclass conversion needs explicit casts/typed unpacking to
     avoid an implicit `Any`, consistent with why `CLAUDE.md` runs strict
     mode over exactly this kind of code.
   - Key material must never land in a traceback, log line, or the audit
     log; error messages on a bad key report shape/length problems only,
     never the value.
   - Windows path: `cryptography` ships manylinux/win wheels for py3.12, so
     no build toolchain surprise is expected in this repo's `.venv`, but
     `pip install -e ".[dev]"` should be re-run to confirm before relying on
     it.

4. **What should be added to validation?** A round-trip crypto test; an HMAC
   normalization-consistency test per entity type; a cross-entity-type
   non-collision test; a nonce-uniqueness assertion; a fail-closed test for
   missing/malformed `VEIL_VAULT_KEY`; a TTL purge test with an injectable
   clock; a monotonic-non-reuse-after-purge test; confirmation that no test
   or the module itself ever writes a plaintext value to disk (grep the test
   DB file bytes for the raw fixture values and assert absence).

5. **What is explicitly not needed now?** Keyring integration, tokenizer
   wiring, audit log wiring, any adapter, the frontend, automatic purge
   scheduling, and any change to `core/detector.py`.

## Risk

Risk class: high

Matches `CLAUDE.md`'s explicit high-risk surface: "Changes to detection,
vault or tokenization... require human approval before merge." This mission
handles the master key and real AES-GCM encryption for the first time in the
project. A mistake here (a reused nonce, a key logged on error, a
normalization bug that silently splits one customer's value across two
tokens) is a direct PII-leak or correlation-breaking defect, not a cosmetic
one. Protected areas: `core/detector.py`, `core/tokenizer.py`, `core/audit.py`,
all adapters, the frontend.

## Merge Policy

High-risk human verification required. Independent `goal-verifier` and
`code-reviewer` passes before merge. A security-focused review pass is
warranted given this is the project's first real cryptography and secret-key
handling code.

## Status

approved — implementing

<!-- Lifecycle values read by .claude/hooks/mission-state-guard.sh:
     not started -> awaiting approval -> approved — implementing
     (or, when ALL agent-owned skip conditions hold:
      agent-owned — implementing / agent-owned — proceeding without
      pre-code approval). Implementation edits are blocked until the
      status starts with `approved` or `agent-owned`. -->
