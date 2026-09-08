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

## Live Spike

None run this mission. Declined by the Intake Grill (Q2): no LM Studio
instance was reachable to test against. Client and prompt are built and
tested entirely against a mocked HTTP layer (`httpx.MockTransport`, matching
`test_gateway.py`'s existing pattern for the Anthropic API). A concrete
manual verification checklist for running the client against a real LM
Studio instance is written into `.harness/handoff.md` — required human
follow-up before this mission's client code is trusted end-to-end, not a
substitute for it.

## Executable Checks

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore`, no implicit `Any` |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| LLM happy path (AC1, AC2) | pytest: mock `/v1/chat/completions` returning valid JSON spans covering at least one of each `EntityType` (the seven structured types plus `PERSON`/`ADDRESS`); assert `detect_llm`/`detect` returns correctly-offset `Span`s tagged `Detector.LLM`, unioned with regex spans via unmodified `merge_spans()` | exit `0` |
| Prompt requests every entity type (AC2) | pytest: assert the request body/prompt text built by the client enumerates all seven structured types plus `PERSON`/`ADDRESS` — not just names | exit `0` |
| Fail-closed: dead endpoint (AC3) | pytest: point `VEIL_LLM_BASE_URL` at a host the mock transport refuses to connect to (or raises `httpx.ConnectError`); assert `detect()`/`tokenize()` still returns the regex spans/tokenizes structured entities, no exception propagates | exit `0` |
| Fail-closed: timeout (AC3) | pytest: mock transport raises `httpx.TimeoutException`; same assertion as above | exit `0` |
| Fail-closed: non-2xx (AC3) | pytest: mock transport returns a 500; same assertion as above | exit `0` |
| Fail-closed: malformed JSON (AC3) | pytest: mock transport returns a 200 with a non-JSON or schema-invalid body (missing required fields, wrong types); same assertion as above | exit `0` |
| Fail-closed logging contains no PII/secret | pytest: trigger one failure case above with a request text containing a real-looking email; assert nothing logged to stderr contains that email or any secret | exit `0` |
| Cache hit (AC4) | pytest: call `detect_llm`/`detect` twice with identical text against a mock transport instrumented to count calls; assert the mock was invoked exactly once | exit `0` |
| Cache keyed by content, not identity (AC4) | pytest: two calls with equal-but-distinct string objects (or the same text appearing in two different source strings) still hit the cache; two calls with different text both invoke the mock | exit `0` |
| `VEIL_LLM_BASE_URL` unset reproduces today's behavior (AC5) | pytest: with the env var unset/deleted, call `tokenize()`/`_tokenize_text()` on text containing structured PII; assert identical output to before this mission (regex spans only, no attempt to construct an `LLMClient` or make a network call) | exit `0` |
| Existing tokenizer/gateway/hooks suites unaffected (AC5) | `.venv/Scripts/pytest tests/test_tokenizer.py tests/test_gateway.py tests/test_hooks.py` | exit `0`, no pre-existing assertion needed to change because of this mission |
| `STRUCTURED_TYPES` still exactly the original seven (Scope) | pytest: `assert STRUCTURED_TYPES == {EMAIL, DNI, NIE, IBAN, PHONE, CARD, IP}` (not including `PERSON`/`ADDRESS`) | exit `0` |
| `eval.py` per-layer modes (AC6) | `.venv/Scripts/python -m veil_pii_middleware.demo.seed && .venv/Scripts/python -m veil_pii_middleware.demo.eval --mode regex` (or equivalent flag) followed by `--mode llm` and `--mode union` against a mocked/offline LLM client (no real LM Studio available) | each mode runs and prints a report; `--mode regex`'s gate behavior is unchanged from before this mission |
| Recall gate unaffected by new entity types (AC6) | `.harness/bin/verify`'s `eval` row (`--min-recall 1.0 --max-false-positives 0`, regex-only mode) | exit `0`, identical pass/fail semantics to before this mission |
| `core/vault.py` untouched (AC7) | `git diff --stat` reviewed by hand | no lines changed in `core/vault.py` |

## Scrutiny Validator

- Confirm the diff touches only `core/detector.py`, `core/tokenizer.py`,
  `demo/eval.py`, their tests, and `.harness/*` — `core/vault.py`,
  `adapters/hooks/`, `adapters/gateway/server.py`, and `adapters/command/`
  stay untouched (`git diff --stat`); those adapters inherit the new
  behavior only through `tokenizer.py`, with no edits of their own needed.
- Confirm `detect()`'s LLM leg is wrapped so that *any* exception from
  `detect_llm()`/the HTTP client is caught at that boundary — not just the
  specific exception types exercised by tests — and never propagates out of
  `detect()` to break `tokenize()`.
- Confirm `STRUCTURED_TYPES` is now an explicit frozenset of the seven
  original members, not `frozenset(EntityType)` — the latter would silently
  include `PERSON`/`ADDRESS` the moment they're added, changing the recall
  gate's scope without anyone deciding that.
- Confirm the content-block cache key is derived from the text content
  itself (e.g. a hash of the string), not from object identity or a mutable
  external counter, so two calls with equal text always hit the cache
  regardless of where that text came from.
- Confirm the LLM prompt (or request-building code) enumerates every
  `EntityType` member programmatically (e.g. iterating the enum) rather than
  a hand-written list that silently drifts out of sync if a member is added
  later.
- Confirm the offset-recovery logic for a value the model returns without a
  trustworthy index does not guess: a value that cannot be found verbatim in
  the source text is skipped (not fabricated at some default offset), and a
  value appearing more than once has a defined, tested rule (documented in
  the implementation, e.g. first occurrence) rather than undefined behavior.
- Confirm no raw PII value is ever written to stderr/logs from anywhere in
  the new code paths — including the fail-closed warning and any debug
  logging around the LLM call.
- Confirm `VEIL_LLM_BASE_URL`/`VEIL_LLM_MODEL` are read from the environment
  only at the outer call sites (`tokenizer.py`'s wiring, `eval.py`'s CLI),
  never hardcoded as a default host/port anywhere, per BRIEF §3.1.

## User-Testing Validator

- No live spike against a real LM Studio instance was possible this mission
  (none reachable) — the mocked-HTTP tests are the proof of the client's
  contract logic, not of real-world compatibility with LM Studio's actual
  response shapes.
- Required manual follow-up, written into `.harness/handoff.md` as a
  step-by-step checklist for the human reviewer: start LM Studio, load a
  model, enable its local server (note the port), set
  `VEIL_LLM_BASE_URL`/`VEIL_LLM_MODEL` accordingly, and run a small scripted
  check (a short text with a known name/address/structured entity) to
  confirm the real response parses under this mission's schema — and to
  confirm or correct the mission's assumptions about LM Studio's actual
  `/v1/chat/completions` response shape before this code is relied on for
  real detection.
- If the human reviewer wants an extra check with no LM Studio available:
  run `demo/eval.py --mode llm` against the mocked client stand-in and
  confirm the per-entity-type report renders sensibly (even though its
  numbers reflect the mock, not a real model).
