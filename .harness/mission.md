# Mission

## Objective

Implement the probabilistic (LLM) detection layer in
`veil_pii_middleware/core/detector.py` (BRIEF §3.1, work-plan item 11),
targeting LM Studio as the actual host via its OpenAI-compatible
`/v1/chat/completions` endpoint. Today `core/detector.py` implements the
regex layer only; `Detector` already has `REGEX`/`LLM` members and `Span`
already accepts either, but nothing produces an `LLM` span. Wire the new
layer into `core/tokenizer.py::_tokenize_text()` (currently hardcoded to
`detect_regex()` + `merge_spans()`) so gateway/hooks traffic actually
benefits, and into `demo/eval.py` so regex-only / LLM-only / union recall
and precision are reported per entity type per detector — this project's
central comparison artifact per user decision (see memory:
`llm-layer-covers-all-entities`).

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

## Intake Grill

| Question | Answer / Evidence |
|---|---|
| How should the LLM layer be switchable off, mechanically? | A new `detect(text, *, use_regex=True, use_llm=True, llm_client=None) -> list[Span]` entry point in `core/detector.py`, taking plain booleans/an optional client — no env-var reads inside `core/`. `merge_spans()` is unchanged and already handles the union. Callers (tokenizer.py's wiring, eval.py's CLI, tests) decide the booleans; env vars are read only at those outer edges, matching the codebase's existing free-function style (no new config class). |
| Is there a live LM Studio instance reachable right now to spike the client against, the way mission-7/8 live-tested the gateway? | No. Build and test the client entirely against a mocked HTTP layer (an `httpx` transport mock, matching `test_gateway.py`'s own pattern for the Anthropic API). `.harness/handoff.md` will carry a concrete manual verification checklist for running it against a real LM Studio instance later, before the client is trusted end-to-end. |
| Does this mission update `core/tokenizer.py::_tokenize_text()` (currently hardcoded to `detect_regex()`+`merge_spans()`) so the LLM layer is live for gateway/hooks traffic, not just reachable from tests/eval.py? | Yes. `_tokenize_text()` switches to the new `detect()` call; whether `use_llm` is `True` is decided once per call from whether `VEIL_LLM_BASE_URL` is set (see below) — this is what tokenizer.py's own existing comment already anticipates ("that is where a future LLM-layer span list unions in"). |
| Does `demo/eval.py` need per-layer (regex-only / LLM-only / union) run modes in this mission? | Yes. Per standing project memory (`llm-layer-covers-all-entities`), this is "the demo's central comparison artifact." The existing 100%-recall gate (`.harness/bin/verify`'s `eval` row) stays bound to the regex-only configuration and `STRUCTURED_TYPES` (the original 7 regex-provable types, unchanged) — an LLM-only run is reported, never gated, since it will not reach 1.000 recall and gating it would leave the gate permanently red. |
| What EntityType taxonomy should the LLM layer target for what regex can't prove? | Add exactly two new `EntityType` members: `PERSON` and `ADDRESS`. `demo/seed.py` already uses the literal string `"PERSON"` for its manifest ground truth (full names in `users.full_name` and `error_logs.message`); `ADDRESS` has no manifest ground truth yet (0 support in `eval.py` until a future mission seeds one), but the type exists so a found address is tokenized/audited correctly rather than mis-typed. "Indirect references" (BRIEF's third example, e.g. "the son of the Acme administrator") are not a separate taxonomy leaf — the prompt still asks the model to resolve them, but the span returned is `entity_type=PERSON` (an indirect reference is a style of naming a person, not a different value type). |
| What timeout should the LLM call use before falling back to regex-only spans? | 10 seconds, single attempt, no retry. Generous enough for a LAN-local LM Studio host under light load; short enough that a hung endpoint does not stall every cache-miss block of a debugging session. Any exception on that call — connect error, timeout, non-2xx, malformed/unparseable JSON, a response shape that doesn't match the expected spans schema — triggers the same fallback path. |

Evidence gathered before asking: `core/detector.py`'s current docstring, `EntityType`/`Detector`
enums, `Span`, `merge_spans()`, `STRUCTURED_TYPES = frozenset(EntityType)`;
`core/tokenizer.py::_tokenize_text()`'s existing "future LLM-layer" comment;
`demo/seed.py`'s `PERSON` manifest constant; `demo/eval.py`'s `by_detector`
breakdown already keyed by `Detector` value; `pyproject.toml` (httpx already a
runtime dependency, no new HTTP library needed); BRIEF §3.1, §6, §7 item 4,
§8 items 11-12; memory `llm-layer-covers-all-entities` (user override:
LLM layer must attempt every entity type, not just names, and the regex
layer must be switchable off).

## Scope

- `veil_pii_middleware/core/detector.py`:
  - Two new `EntityType` members: `PERSON`, `ADDRESS`.
  - `STRUCTURED_TYPES` becomes an explicit frozenset of the original seven
    regex-provable types (`EMAIL, DNI, NIE, IBAN, PHONE, CARD, IP`) — no
    longer `frozenset(EntityType)`, since that would silently pull in the two
    new LLM-only members and break the recall gate's scoping the moment they
    are added.
  - An `LLMClient` (or equivalently named) class wrapping an OpenAI-compatible
    `/v1/chat/completions` call via `httpx`: base URL from `VEIL_LLM_BASE_URL`
    (env var, never hardcoded to any host/port), model name from
    `VEIL_LLM_MODEL`, 10s timeout, no retry. Constructed from the environment
    once by callers (tokenizer.py's wiring, eval.py's CLI), not read
    internally by `detect()` on every call.
  - A prompt requiring the model to return spans as strict JSON only (never
    rewritten text) covering every `EntityType` member — the seven structured
    types plus `PERSON`/`ADDRESS` — with an explicit instruction that an
    indirect reference to a person still resolves to `PERSON`.
  - `detect_llm(text, client) -> list[Span]`: calls the client, offsets spans
    against `text` by finding the returned value's location (the model
    returns values, not authoritative offsets, since an LLM cannot be
    trusted to count characters correctly); parses/validates the JSON
    response and raises on any malformed/unexpected shape. On success, spans
    are tagged `Detector.LLM`.
  - A content-block hash cache: an in-memory, module-level mapping from
    `sha256(text)` to that block's cached `detect_llm` result, so a repeated
    block within one process's lifetime (the gateway's target use case — one
    long-running process per debugging session) is not re-sent to the LLM.
    Scoped to one process; not persisted to disk (hooks run as short-lived
    per-invocation processes today and gain no benefit from a cache that
    resets every call — out of scope to change that).
  - `detect(text, *, use_regex=True, use_llm=True, llm_client=None) ->
    list[Span]`: unions `detect_regex()` (if `use_regex`) and `detect_llm()`
    (if `use_llm` and a client is supplied) via the existing `merge_spans()`,
    fail-closed on the LLM leg — any exception from `detect_llm()` is caught
    and logged (no PII/secret), and the call returns the regex spans (or an
    empty list if `use_regex=False` too) rather than propagating.
- `veil_pii_middleware/core/tokenizer.py`: `_tokenize_text()` calls the new
  `detect()` instead of `detect_regex()`+`merge_spans()` directly. `use_llm`
  is decided once per call from whether `VEIL_LLM_BASE_URL` is set (empty/
  unset → `use_llm=False`, so every existing caller and test that does not
  set this env var is completely unaffected — no new network calls, no new
  timeouts, behavior identical to today). When set, an `LLMClient` is built
  from the environment and passed through.
- `veil_pii_middleware/demo/eval.py`: `evaluate()` gains a way to run in
  three modes — regex-only (today's default, unchanged), LLM-only, and union
  — reporting recall/precision per entity type per detector for each. The
  `--min-recall`/`--max-false-positives` gate stays scoped to
  `STRUCTURED_TYPES` under the regex-only configuration; LLM-only/union runs
  are reported, not gated.
- `tests/test_detector.py`: tests for `detect_llm`/`detect` against a mocked
  HTTP layer — happy path (valid JSON spans for each new entity type),
  malformed JSON, timeout, non-2xx, and the fail-closed fallback to regex
  spans in each failure case; cache-hit behavior (same content-block hash
  does not re-invoke the mocked client).
- `tests/test_tokenizer.py`: a test proving `_tokenize_text()`/`tokenize()`
  is unaffected when `VEIL_LLM_BASE_URL` is unset (today's existing
  behavior, still regex-only), and a test proving an LLM-detected span (via
  a mocked client) is tokenized the same way a regex span is.
- `.harness/handoff.md`: a concrete manual verification checklist for
  running the client against a real LM Studio instance (start LM Studio,
  load a model, set `VEIL_LLM_BASE_URL`/`VEIL_LLM_MODEL`, run a scripted
  check), since no live spike was possible in this mission.

## Non-goals

- Any change to `core/vault.py` — untouched by this mission.
- `adapters/hooks/pre_tool_use.py`/`post_tool_use.py` and
  `adapters/gateway/server.py` — none of them call `detect_regex()`/`detect()`
  directly (they call through `core/tokenizer.py`), so no adapter file needs
  editing; they inherit the new behavior automatically once
  `_tokenize_text()` is wired and `VEIL_LLM_BASE_URL` is set in their
  environment. Not verified live against a real LM Studio instance in this
  mission (see the live-spike answer above).
- A disk-persisted/cross-process cache for the content-block hash — the
  in-memory, per-process cache is the target use case (the gateway's
  long-running process); making hooks' short-lived processes benefit too is
  a separate, unrequested change.
- `ADDRESS` ground truth in `demo/seed.py`'s manifest — the entity type is
  added to the taxonomy so a found address is correctly typed, but seeding
  actual address instances into the demo corpus is not part of this mission.
- A generic "indirect reference" `EntityType` member — folded into `PERSON`
  per the Intake Grill answer above.
- Retry/backoff policy for the LLM call beyond a single attempt — a prior
  explicit decision (10s, no retry), not reopened here.
- Any change to the audit log (`core/audit.py` does not yet exist; wiring
  the audit log itself is a separate, later work-plan item) beyond making
  sure `Span.detector` values this mission produces (`Detector.LLM`) are
  already representable by the existing `Detector` enum — true today,
  nothing to add.

## Acceptance criteria

1. `detect_llm()`/`detect()` produce `Span`s tagged `Detector.LLM` for
   `PERSON` and `ADDRESS` entities from a mocked LLM response, and correctly
   union with regex spans via the existing `merge_spans()` (longest wins,
   ties to regex) with no changes to `merge_spans()` itself.
2. The LLM prompt (inspectable in source/tests) requests every `EntityType`
   member, not only `PERSON`/`ADDRESS` — verifiable by asserting the prompt
   text or schema enumerates all seven structured types plus the two new
   ones.
3. Fail-closed proven by test: an LLM call that times out, connects to a
   dead endpoint, returns a non-2xx status, or returns malformed/unparseable
   JSON still yields the regex layer's spans unchanged — the whole
   `detect()` call never raises out to the caller for an LLM-side failure,
   and `_tokenize_text()`/`tokenize()` still succeeds.
4. Cache proven by test: two calls to `detect_llm()`/`detect()` with the
   same text within one process invoke the mocked HTTP client exactly once.
5. `VEIL_LLM_BASE_URL` unset reproduces exactly today's regex-only behavior
   through `tokenize()` — proven by a test that the existing tokenizer/
   gateway/hooks test suite continues to pass unmodified in outcome (no new
   network calls attempted).
6. `demo/eval.py --mode regex|llm|union` (or equivalent CLI surface) reports
   recall/precision per entity type per detector for each mode; the
   `--min-recall`/`--max-false-positives` gate remains scoped to
   `STRUCTURED_TYPES` (the original seven) so `.harness/bin/verify`'s `eval`
   row is unaffected by the two new entity types or by an LLM-only run's
   necessarily-lower recall.
7. `core/vault.py` is unmodified (confirmed by `git diff --stat`).
8. `.harness/bin/verify` exits 0.
9. `.harness/handoff.md` documents the fail-closed proof, the cache proof,
   and a concrete, step-by-step manual checklist for verifying the client
   against a real LM Studio instance (since this mission has no live spike
   evidence) plainly enough that a human can decide, without re-running
   anything, whether the mission is correct and what remains to be
   confirmed live.

## Risk

Risk class: high

Rationale: this is the designated high-risk surface per CLAUDE.md's "Review
and risk" convention ("changes to detection, vault or tokenization... require
human approval before merge") — a missed span here is a data leak. This
mission also introduces the project's first outbound network call made from
inside `core/` itself (previously only the gateway adapter spoke HTTP, and
only to the upstream Anthropic API over a connection the user's own Claude
Code session already established) — a bug in the fail-closed path could
either silently stop degrading safely (violating BRIEF acceptance criterion
4) or, in the worst case, block/hang detection entirely. The LAN-unencrypted
raw-PII-to-LM-Studio trade-off is a prior, explicit, out-of-scope decision
(BRIEF §3.1), not re-litigated here, but the code implementing it is new and
warrants full human review before merge.

## Merge policy

No commit, push, or merge without explicit human review of the diff and
`.harness/handoff.md`. This mission does not merge itself.

## Tooling And Approach Assessment

1. **What project type is this?** Adding a new detection backend (a network
   client against an OpenAI-compatible chat-completions endpoint) to an
   existing hybrid-detector module, plus wiring it into one existing call
   site (`tokenizer.py`) and one existing evaluation script (`eval.py`). Not
   new architectural surface — `Detector.LLM`, `Span.detector`, and
   `merge_spans()`'s union/tie-break policy already anticipate this.
2. **What methods, tooling, or approaches does that imply?** `httpx` (already
   a runtime dependency, used identically by the gateway adapter) for the
   `/v1/chat/completions` POST; a mocked `httpx.MockTransport` for tests,
   matching `test_gateway.py`'s existing pattern for the Anthropic API rather
   than inventing a new test-double style; `hashlib.sha256` (stdlib) for the
   content-block cache key, matching `Span`'s own `frozen=True, slots=True`
   hashability design already noted as being "for the content-block cache."
3. **What risks or missing decisions does it reveal?** (a) offset recovery:
   the model returns matched *values*, not trustworthy offsets, so
   `detect_llm()` must locate each returned value in the source text itself
   (first/all occurrences) rather than trusting a model-reported index —
   this needs a defined, tested policy for a value that appears more than
   once or not at all in the text (not currently specified; resolve during
   implementation with the narrowest safe rule: search for the value and
   skip — do not guess — an instance that cannot be found verbatim, logging
   nothing PII-bearing about the miss). (b) `EntityType` gaining two new
   members changes `STRUCTURED_TYPES`'s current definition
   (`frozenset(EntityType)`) from implicit to explicit — a one-line but
   easy-to-miss change; called out explicitly in Scope above so it is not
   dropped silently. (c) the LAN-unencrypted transport to LM Studio is
   already an accepted trade-off (BRIEF §3.1) and not reopened, but this is
   the first code that actually implements sending text over it — worth the
   reviewer's attention even though the decision itself is not in question.
4. **What should be added to validation?** Mocked-HTTP tests for the happy
   path, malformed JSON, timeout, non-2xx, and the fail-closed fallback;
   cache-hit test; a test proving `VEIL_LLM_BASE_URL` unset changes nothing
   about existing tokenizer/gateway/hooks behavior; `eval.py`'s three modes
   producing a report; `git diff --stat` confirming `core/vault.py`
   untouched; a manual LM Studio verification checklist in `handoff.md`
   (since no live spike ran).
5. **What is explicitly not needed now?** A live spike against a real LM
   Studio instance (none reachable this session — deferred to the manual
   checklist); retry/backoff beyond one attempt; a disk-persisted cache;
   `ADDRESS` manifest ground truth; a separate "indirect reference" entity
   type; any change to `core/vault.py`, `core/audit.py` (not yet built), or
   the adapter files themselves.

## Status

implemented — awaiting human review for merge (see `.harness/handoff.md`;
now includes completed live LM Studio verification, not just the mocked
test suite)
