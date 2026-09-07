# Mission

## Objective

Implement `veil_pii_middleware/core/tokenizer.py`: `tokenize()` and
`detokenize()`, the substitution/reversal layer between the detector and the
vault, per BRIEF §3.1 and §3.3. `tokenize(value, vault)` runs `detect_regex()`
over string leaves, unions the result through `merge_spans()` (already
designed to merge multiple detector layers, so `tokenize` must not assume
regex is the only source of spans — this is where the future LLM layer's
spans will join), and replaces each surviving span with
`vault.get_or_create_token(span.text, span.entity_type)`, applying offset
replacements right-to-left so earlier offsets stay valid.
`detokenize(value, vault)` finds `PII_<TYPE>_<NNNN>`-shaped substrings and
replaces each with `vault.resolve(token)`. Both functions extend to nested
JSON-shaped structures (`dict`, `list`, and JSON scalar leaves), recursing
into dicts and lists, tokenizing/detokenizing only string leaf values, and
leaving keys, structure, and non-string scalars untouched.

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

## Intake Grill

| Question | Answer / Evidence |
|---|---|
| When `detokenize()` meets a token that doesn't resolve in this vault's session (unknown, malformed, or from another session), what happens? | Let the vault's own typed errors propagate as-is — `TokenNotFoundError` / `VaultIntegrityError` (`core/vault.py`) already carry the token as context and are already fail-closed. No new wrapper exception in `tokenizer.py`; that would be an abstraction the task doesn't need. |
| Inside a nested structure, if one leaf token fails to resolve, does the rest still get processed? | No — abort on first failure. Nothing partially resolved is returned. Matches the `CLAUDE.md` fail-closed doctrine ("when detection cannot run, block rather than forward"): a structure with one real value and one un-resolvable token left as literal text is a worse outcome than failing the whole call. |
| How should non-JSON-shaped leaves (tuple, bytes, a dataclass instance) be handled during nested traversal? | Pass through unchanged, no recursion attempted. Matches the task's stated scope ("estructuras anidadas ... valores JSON-serializables") — `dict`, `list`, `str`, and JSON scalars (`int`, `float`, `bool`, `None`) are the only shapes in scope. |

Evidence gathered from the repo before asking: `core/detector.py` exposes
`detect_regex(text) -> list[Span]` (already internally merged) and
`merge_spans(spans: Iterable[Span]) -> list[Span]` (longest-span-wins, ties to
regex, ordered by start offset) — both already implemented and unmodified by
this mission. `core/vault.py`'s `Vault.get_or_create_token(value, entity_type)`
and `Vault.resolve(token)` are implemented and unmodified; `resolve()` already
raises `TokenNotFoundError` for any token absent from this session (including
a token that belongs to a different session) and `VaultIntegrityError` on
AEAD authentication failure — both already carry the token in the exception,
so propagating them satisfies "fail closed with context" without new code.
`tokenizer.py` today is a docstring-only stub. No existing tests reference
`tokenizer`.

## Scope

- `veil_pii_middleware/core/tokenizer.py`:
  - `tokenize(value: JSONValue, vault: Vault) -> JSONValue` — string leaves
    run through `detect_regex()` + `merge_spans()`, each surviving span
    replaced by its vault token via right-to-left offset substitution; dicts
    and lists recurse (keys untouched); other scalar/non-JSON types pass
    through unchanged.
  - `detokenize(value: JSONValue, vault: Vault) -> JSONValue` — same
    recursion shape; string leaves have every `PII_<TYPE>_<NNNN>` substring
    replaced via `vault.resolve(token)`; a resolution failure raises the
    vault's own exception immediately (abort, no partial result).
  - A `JSONValue` recursive type alias (`str | int | float | bool | None |
    dict[str, JSONValue] | list[JSONValue]`) for the public signatures, kept
    `mypy --strict` clean.
  - The token-matching regex in `detokenize` is entity-type-agnostic
    (`PII_[A-Z]+_\d{4}`, not restricted to today's seven `EntityType`
    members) — safe because `vault.resolve()` already fails closed
    (`TokenNotFoundError`) on any token that was never actually issued, so a
    syntactically-token-shaped-but-fictitious substring is caught there
    regardless. This keeps `detokenize` forward-compatible with entity types
    the future LLM layer will introduce (e.g. `PERSON`, per BRIEF §3.2's
    example) without a code change in this file.
- `tests/test_tokenizer.py` (new): single-value substitution, multiple
  distinct entity types in one text, exact round-trip
  (`text -> tokenize -> detokenize == text`), nested structures (dict with
  lists and sub-dicts, keys/structure preserved), repeated value in the same
  text yields the same token end-to-end, an unknown/foreign-session token in
  `detokenize` raises (propagated from the vault, not swallowed).

## Non-goals

- `core/audit.py` — no audit logging in this mission.
- Any adapter (`/ask` command, hooks, gateway) — no wiring into a transport.
- `frontend/` — no observability panel changes.
- The detector's LLM layer — `tokenize` must not assume regex is the only
  span source (per `merge_spans`'s design), but this mission does not
  implement or stub that layer.
- Vault schema, crypto, or TTL/purge behavior — `core/vault.py` is consumed
  as-is, unmodified.
- A new tokenizer-specific exception type for the *unknown-token* case — the
  vault's existing exceptions are reused per the Intake Grill answer above.
  (One narrow exception, `TokenFormatOverflowError`, was added mid-mission
  for a different failure mode — a minted token's sequence number
  overflowing the fixed four-digit width — found by the security-review
  pass; see `## Post-review addendum` below. That is not the unknown-token
  case this bullet is about.)

## Acceptance criteria

1. `tokenize("contact alice@example.com", vault)` replaces the email with a
   `PII_EMAIL_NNNN` token and leaves the rest of the text untouched.
2. A text with an email, a DNI, and a phone number, all tokenized in one
   `tokenize()` call, produces three distinct tokens of the correct types,
   with unaffected surrounding text intact.
3. `detokenize(tokenize(text, vault), vault) == text` for representative
   inputs (exact round-trip).
4. A `dict` containing nested `list`s and sub-`dict`s: string leaf values are
   tokenized/detokenized, keys are untouched, and structure/type (dict vs
   list vs scalar) is preserved exactly.
5. The same PII value repeated twice in one text (or across two leaves of one
   structure) yields the same token both times, verified end-to-end through
   `tokenize()` (the vault already guarantees this; the test proves the
   tokenizer doesn't break it, e.g. via offset-replacement order bugs).
6. `detokenize()` on a token absent from the vault's session (including one
   minted by a different session) raises — proven via a test — rather than
   leaving the literal token string in the output or silently returning
   something plausible-looking.
7. `mypy --strict` passes over the new module with no `Any` around spans,
   text, or structure values.
8. `.harness/bin/verify` is green.

## Risk

Risk class: high

Touches the substitution surface `CLAUDE.md` names as high-risk: a bug here
either leaves raw PII in text that reaches an external API/LLM (missed span,
wrong offset math corrupting adjacent text) or silently breaks the
correlation guarantee (same value producing two different tokens across a
structure). Offset-based right-to-left replacement is also a classic
off-by-one surface when multiple spans are present in one string.

## Merge policy

Human approval required before merge (per `CLAUDE.md`'s high-risk review
requirement for detection/vault/tokenization changes). No commit, push, or
merge performed by the agent.

## Tooling And Approach Assessment

1. **What project type is this?** A pure-Python library module with zero new
   runtime dependencies — a string/structure transformation layer sitting
   between two already-implemented, already-tested modules (`detector.py`,
   `vault.py`). No new I/O, no new crypto, no new schema.
2. **What methods, tooling, or approaches does that imply?** Straightforward
   recursive dispatch on Python's builtin JSON-shaped types
   (`isinstance` checks), string offset slicing for substitution, and a
   compiled `re.Pattern` for the detokenize token-matching regex. Standard
   `pytest` unit tests: no fixtures beyond what `test_vault.py` already
   demonstrates (`tmp_path`-backed `Vault` with a fixed test key). Existing
   `ruff`/`mypy --strict`/`pytest` gates apply unchanged; no new tooling
   needed.
3. **What risks or missing decisions does it reveal?** (a) Offset math when
   multiple spans exist in one string — resolved by the mandated
   right-to-left replacement order, already specified in the task and
   covered by acceptance criterion 2. (b) Whether a partially-resolved
   nested structure can ever leave the function on failure — resolved by
   the Intake Grill (abort-on-first-failure, propagate vault exceptions).
   (c) Forward-compatibility of the token-matching regex with entity types
   the future LLM layer will add — resolved by making the regex
   entity-type-agnostic rather than coupled to today's `EntityType` enum.
4. **What should be added to validation?** `mypy --strict` specifically on
   the new module (already covered by the project-wide gate, but called out
   because this module is on the high-risk list); a manual/documented check
   that `tokenize`+`detokenize` are exact inverses on a representative
   corpus (covered by the round-trip test, not just eyeballing); confirming
   no plaintext PII value appears in any exception message or repr raised by
   this module (a `pytest.raises` assertion on the exception's string form,
   not just its type).
5. **What is explicitly not needed now?** Any new dependency, any new schema
   migration, any performance/caching work (BRIEF §3.1's content-block cache
   is a detector-layer concern, not this module's), and any handling for
   entity types the current `EntityType` enum doesn't define — `tokenize`
   only ever mints tokens for the seven regex-covered types today.

## Post-review addendum

A security-focused `code-reviewer` pass found that `Vault.get_or_create_token`
(`core/vault.py:218`, unmodified, out of this mission's scope) formats its
per-`(session_id, entity_type)` sequence number with `f"{seq:04d}"` — a
minimum-width zero-pad, not a cap. Past 9999 tokens of one entity type in one
session, the format silently produces a five-digit token
(e.g. `PII_EMAIL_10000`). `detokenize`'s original token-matching regex
(`PII_[A-Z]+_\d{4}`) then matched only the first four digits of such a token
and successfully resolved that truncated prefix against a different,
earlier-issued token in the same session — silently returning the wrong
value instead of failing closed. Reproduced live by the reviewer.

Given the user's explicit choice (fix confined to `tokenizer.py`, `vault.py`
left untouched): `tokenize()` now verifies, immediately after minting each
token, that it still matches the fixed four-digit shape, and raises the new
`TokenFormatOverflowError` (`ValueError` subclass, carries only the token
string — never a PII value) before ever writing an ambiguous token into
output text. `detokenize`'s regex keeps its original four-digit assumption,
which this guard keeps a true invariant of everything this module emits.
A regression test (`TestTokenFormatOverflow` in `tests/test_tokenizer.py`)
forces a session's counter to 10000 and asserts the overflow is caught at
`tokenize()` time rather than silently mis-resolving later. The root-cause
fix in `vault.py` (capping the counter itself) was declined for this
mission and is left as a possible follow-up.

## Status

approved — implementing
