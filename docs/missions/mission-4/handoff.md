# Handoff

## Summary

`core/tokenizer.py` is implemented: `tokenize(value, vault)` and
`detokenize(value, vault)`, the substitution/reversal layer between
`core/detector.py` and `core/vault.py` (both consumed unmodified). `tokenize`
runs `detect_regex()` + `merge_spans()` over string leaves and replaces each
surviving span with its vault token via right-to-left offset substitution;
`detokenize` finds `PII_<TYPE>_<NNNN>`-shaped substrings and replaces them
with `vault.resolve(token)`. Both extend recursively to `dict`/`list`
structures via a `JSONValue` type alias, tokenizing/detokenizing only string
leaves and leaving keys, container types, and non-string scalars untouched
without mutating the caller's input.

Per the Intake Grill: an unresolvable token in `detokenize` propagates the
vault's own `TokenNotFoundError`/`VaultIntegrityError` uncaught (no new
wrapper exception), and a nested structure aborts entirely on the first
unresolvable leaf rather than returning anything partial.

`.harness/bin/verify` is green (contract, lint, typecheck, test, eval,
build; 129 tests, no regressions in `detector`/`vault` suites).
`tests/test_tokenizer.py` has 21 tests covering single- and multi-entity
substitution, exact round-trip, nested structures (including no in-place
mutation), same-value-same-token consistency end-to-end, unknown/foreign-
session token failure, offset correctness under multiple spans, and the
token-sequence-overflow regression below. `goal-verifier` independently
confirmed all 8 numbered acceptance criteria pass with file:line evidence.
A security-focused `code-reviewer` pass found one blocking issue; it is
fixed and re-verified green below.

### Security review finding and resolution

**Finding (blocking):** `Vault.get_or_create_token` (`core/vault.py:218`,
unmodified) formats its per-`(session_id, entity_type)` sequence number with
`f"{seq:04d}"` — a minimum-width zero-pad, not a cap. Past 9999 tokens of one
entity type in one session, this silently produces a five-digit token (e.g.
`PII_EMAIL_10000`). `detokenize`'s original token-matching regex
(`PII_[A-Z]+_\d{4}`) then matched only the first four digits of such a token
and successfully resolved that truncated prefix against a different,
earlier-issued token in the same session — silently returning the *wrong*
customer's plaintext value instead of failing closed. The reviewer
reproduced this live.

**Resolution (user-approved, confined to `tokenizer.py`):** `tokenize()` now
verifies, immediately after minting each token via `vault.get_or_create_token`,
that the token still matches the fixed four-digit shape
(`_TOKEN_PATTERN.fullmatch(token)`), and raises a new `TokenFormatOverflowError`
(a `ValueError` subclass carrying only the token string, never a PII value)
before ever writing an ambiguous token into output text. This keeps
`detokenize`'s four-digit assumption a true invariant of everything this
module emits, without touching `vault.py` (declined for this mission — the
alternative root-cause fix, capping the counter in `vault.py` itself, is
left as a possible follow-up mission). A regression test
(`TestTokenFormatOverflow` in `tests/test_tokenizer.py`) forces a session's
counter to 10000 and asserts the overflow is caught at `tokenize()` time.

Two should-fix/nit items from the same review were judged non-blocking and
left as-is, noted here for the record:

- `_tokenize_text` routes through `merge_spans(detect_regex(text))` even
  though `detect_regex` already returns merged spans internally — this is
  intentional (documented in a code comment) as the future LLM-span union
  point, not dead code, but a future reader could mistake it for a no-op.
- Non-`dict`/`list` container types a caller might pass (e.g. a `tuple`)
  fall through unrecursed, matching the documented `JSONValue` contract
  exactly (out of scope by design per the Intake Grill), but worth
  remembering if a future adapter hands looser structures to these
  functions.

## Files changed

- `veil_pii_middleware/core/tokenizer.py` — new: `tokenize`, `detokenize`,
  `JSONValue`, `TokenFormatOverflowError`.
- `tests/test_tokenizer.py` — new: 21 tests.
- `.harness/mission.md`, `.harness/validation.md` — rewritten for this
  mission (previously held the prior vault mission's content, not reset
  after that mission was archived to `docs/missions/mission-3/`).

## Human verification steps

1. Run `bash .harness/bin/verify` from Git Bash at the repo root — expect
   `RESULT: PASS — all 6 gate(s) green.`
2. Read `veil_pii_middleware/core/tokenizer.py` end to end (77 lines) —
   small enough to review in full given its high-risk classification.
3. Read `tests/test_tokenizer.py::TestTokenFormatOverflow` and confirm the
   overflow scenario and fix make sense; decide whether the declined
   `vault.py` root-cause fix (capping the counter) should be a follow-up
   mission.
4. Confirm you're comfortable with the scope-boundary call: this mission
   touches only `tokenizer.py` and its tests, not `vault.py`, per your
   answer to the fix-location question.
5. Approve or request changes before merge (required per `CLAUDE.md`'s
   high-risk review policy for detection/vault/tokenization changes). No
   commit, push, or merge has been performed.

## Follow-ups (not done, for a future mission if wanted)

- Consider capping `Vault`'s per-`(session_id, entity_type)` counter at 9999
  and raising in `vault.py` itself, so the four-digit format is an
  invariant enforced at the source, not only guarded downstream in
  `tokenizer.py`.
- Wire `tokenize`/`detokenize` into an adapter (`/ask`, hooks, or gateway) —
  explicitly out of scope for every mission so far.
- `core/audit.py` — still an unimplemented stub.
