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
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore`, no implicit `Any` on spans, offsets, or structure values |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Build | `.venv/Scripts/python -m build --no-isolation` | exit `0` (no new dependency introduced this mission, so this should be unaffected) |
| Detector recall/decoys (unchanged) | `eval` GATES row: seed, then `python -m veil_pii_middleware.demo.eval --min-recall 1.0 --max-false-positives 0` | exit `0`, unmodified — this mission does not touch `detector.py` |
| Single-value substitution | pytest: `tokenize("contact alice@example.com", vault)` replaces only the email span with `PII_EMAIL_NNNN`, surrounding text unchanged | exit `0` |
| Multi-type substitution in one text | pytest: a text containing an email, a DNI, and a phone in one `tokenize()` call yields three distinct, correctly-typed tokens; text between/around them is untouched | exit `0` |
| Exact round-trip | pytest: `detokenize(tokenize(text, vault), vault) == text` for several representative texts (single entity, multiple entities, no entities, entity at string start/end) | exit `0` |
| Nested structures | pytest: a `dict` with nested `list`s and sub-`dict`s — string leaves tokenized/detokenized, keys untouched, container types (`dict`/`list`) and non-string scalars (`int`, `float`, `bool`, `None`) preserved exactly | exit `0` |
| Repeated value → same token, end-to-end | pytest: the same PII value appearing twice in one text (or in two leaves of one structure), tokenized via one `tokenize()` call, produces the identical token both times | exit `0` |
| Unknown/foreign-session token in detokenize | pytest: `detokenize()` on a token absent from the vault (or minted under a different `session_id`) raises (`TokenNotFoundError` propagated from `vault.py`, not swallowed, not left as literal text) | exit `0` |
| No plaintext in exception surface | pytest: for the unknown-token case above, assert the fixture's real PII value (if any was used to construct the scenario) does not appear in `str(exc)` | exit `0` |
| Offset correctness under multiple spans | pytest: a text with 3+ spans of varying length in arbitrary order — assert every non-PII character survives at its expected relative position after tokenize (guards the right-to-left replacement order) | exit `0` |
| Token sequence overflow fails closed | pytest: force a session's per-entity-type counter to 10000 (past the fixed four-digit width `Vault.get_or_create_token` zero-pads but does not cap), then `tokenize()` a value of that entity type — assert `TokenFormatOverflowError` is raised before any ambiguous token is written into output, rather than `detokenize` later mis-resolving a truncated prefix to a different token's value | exit `0` |

## Scrutiny Validator

- Confirm the diff touches only `veil_pii_middleware/core/tokenizer.py`,
  `tests/test_tokenizer.py`, and the harness mission files — `detector.py`,
  `vault.py`, `audit.py`, adapters, and `frontend/` stay untouched.
- Confirm span replacement is genuinely right-to-left (by start offset,
  descending) — a left-to-right or unordered replacement would corrupt every
  offset after the first substitution once token length differs from the
  original span's length.
- Confirm `tokenize()` does not assume `detect_regex()` is the only span
  source: it must call `merge_spans()` on the span list it builds, not skip
  straight from `detect_regex()`'s already-merged output to substitution,
  since the whole point of routing through `merge_spans()` is that a future
  LLM-layer span list gets unioned through the same call without
  `tokenize()` needing to change.
- Confirm no plaintext value is ever passed to a `str`/log/exception message
  that could reach stdout, a log file, or `.harness` memory.
- Confirm `detokenize()`'s token-recognition regex does not silently accept
  a substring that merely looks token-shaped but was never issued — it must
  reach `vault.resolve()` and let that call's `TokenNotFoundError` decide,
  not pattern-match its way to a false accept.
- Confirm nested traversal does not mutate the input in place when the
  input is a `dict`/`list` (return a new structure), since a caller passing
  a live object (e.g. a parsed JSON payload it still holds a reference to)
  must not see it silently rewritten out from under it.
- Confirm `mypy --strict` passes with no new `# type: ignore`, and that the
  `JSONValue` recursive type alias is used consistently rather than `Any`
  creeping in at the recursion boundary.

## User-Testing Validator

- Run `tests/test_tokenizer.py` directly and read the output — not just the
  exit code — to confirm assertions check actual substituted text/token
  values and exact structural equality, not just "no exception raised."
- Manually run `tokenize()`/`detokenize()` on one ad hoc multi-entity,
  nested-dict example (throwaway script or REPL) and eyeball the token
  format and round-trip result once, as a sanity check the automated tests
  might miss (e.g. an off-by-one visible only by reading the string).
