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

None run this mission — declined implicitly, same reasoning as
mission-11/mission-12: every shape this mission introduces (the
`tool_continuation` reclassification, the `_open_turn_by_session` map, the
`/veil-panel/hook-event` schema, the hook's best-effort HTTP call) is this
codebase's own design, not a third-party dialect to spike against. Proven
by tests plus a manual demo-run checklist in `.harness/handoff.md`.

**Revised 2026-09-09, after the human reported the first pass still broken
live:** the "no spike needed" call above turned out to be wrong in
practice — real `claude -p --demo-db` traffic contains shapes (Claude
Code's own internal harm-classifier request; genuinely duplicate
same-text conversation round-trips) this codebase's own synthetic test
fixtures never produced, and only a real live session surfaced them (see
`.harness/handoff.md`'s "Three real bugs found via live testing" section).
Three full live `claude -p --demo-db` sessions were run against the real
gateway and real hooks as the actual validation for the turn-tracking
redesign and the ring-buffer split below — the executable checks table
still holds as the automated regression net, but was not sufficient on its
own to catch what live testing did.

## Executable Checks

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore`, no implicit `Any` |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Existing gateway/hooks/tokenizer/sanitize tests still pass unmodified in behavior | `.venv/Scripts/pytest tests/test_gateway.py tests/test_tokenizer.py tests/test_hooks.py tests/test_sanitize.py` | exit `0` |
| Pure-tool_result last user message reclassified (AC1) | pytest: build a request body whose last `role: "user"` message's `content` is `[{"type": "tool_result", ...}]` only (no `text`/`thinking` block anywhere in it); assert the published `"received"` event's `kind == "tool_continuation"` | exit `0` |
| Real text alongside a tool_result stays `"conversation"` (AC1, negative case) | pytest: same shape as above but with one additional `{"type": "text", "text": "..."}` block in the same message's content list; assert `kind == "conversation"` | exit `0` |
| `parent_request_id` attached to a tool_continuation while a turn is open (AC2) | pytest: run one `"conversation"` cycle for `session_id="s1"`, then one `tool_continuation`-shaped cycle for the same session; assert the second cycle's events carry `parent_request_id` equal to the first cycle's `request_id` | exit `0` |
| A second conversation cycle supersedes the open turn (AC2) | pytest: conversation cycle A, then conversation cycle B (both `session_id="s1"`), then a tool_continuation cycle; assert `parent_request_id == B.request_id`, not A's | exit `0` |
| No open turn → no `parent_request_id` (AC3) | pytest: publish a tool_continuation-shaped cycle for a `session_id` that has never had a `"conversation"` cycle; assert its events carry no `parent_request_id` (absent or `None`, not an empty string) | exit `0` |
| `POST /veil-panel/hook-event` with an open turn (AC4) | pytest: open a turn for `session_id="s1"`, then `POST /veil-panel/hook-event` with `{"session_id": "s1", "hook": "pre_tool_use", "phase": "start", "tool_name": "Bash"}`; assert response is `204` and exactly one `kind: "hook"` event was published, tagged with `s1`'s open turn's `request_id`, carrying only the four submitted fields | exit `0` |
| `POST /veil-panel/hook-event` with no open turn (AC4) | pytest: same POST for a `session_id` with no open turn; assert response is `204` and nothing was published to the bus | exit `0` |
| `POST /veil-panel/hook-event` malformed body (AC4) | pytest: POST invalid JSON, a JSON array instead of an object, and a JSON object missing a required field; assert `204` in every case, never `500`, and nothing published | exit `0` |
| Hook-event schema ignores extra fields (AC6) | pytest: POST a well-formed body plus an extra field named e.g. `"command"` carrying a fake PII-shaped string; assert the published event's payload contains only `hook`/`phase`/`tool_name`/`request_id`/`timestamp` — the extra field is absent from what got published, not merely unused | exit `0` |
| `_panel_report.report` never raises, bounded time (AC5) | pytest: call `report(...)` with `VEIL_GATEWAY_URL` pointed at a closed local port (bind a socket, close it, use that port); assert the call returns normally and wall-clock time is under e.g. 1 second (proving the 200ms timeout is real, not accidentally unbounded) | exit `0` |
| Hook exit code/stdout unaffected by gateway reachability (AC5) | pytest: run `pre_tool_use.main()` and `post_tool_use.main()` once with `VEIL_GATEWAY_URL` unreachable and once with the env var unset entirely; assert identical exit code and identical stdout JSON in both runs, matching today's pre-mission behavior with no reporting call at all | exit `0` |
| No PII-capable field in the new endpoint or hook payloads (AC6, Non-goals) | `git diff -- veil_pii_middleware/adapters/gateway/server.py veil_pii_middleware/adapters/hooks/` reviewed by hand | the hook-event request/response schema has no field that could hold a command string, tool output, or tokenized/detokenized value — only fixed short labels |
| `core/detector.py`, `core/vault.py`, `core/tokenizer.py` untouched | `git diff -- veil_pii_middleware/core/` reviewed by hand | empty diff |
| Existing recall/decoy gates unaffected | `.harness/bin/verify`'s `eval` row | exit `0`, identical pass/fail semantics to before this mission |
| No new runtime dependency | `git diff -- pyproject.toml` reviewed by hand | no new `dependencies` entry (hooks reuse the already-present `httpx`) |
| Ring buffer / audit log regression guard | `.venv/Scripts/pytest tests/test_gateway.py -k pipeline` | exit `0`, `.veil/audit.jsonl` still contains no `"stage"`/`"pipeline"`/hook-event text (unchanged from mission-11/12's existing proof) |
| Internal safety-classifier request hidden by default (found live) | `TestCycleKindClassification::test_internal_safety_classifier_request_is_classified` | exit `0`, `kind == "safety_check"` on every stage |
| Same-text conversation cycles merge under one turn (found live) | `TestTurnTracking::test_same_text_conversation_cycles_merge_under_one_turn` | exit `0`, both cycles' events resolve to the same canonical turn id |
| Different-text conversation cycle still opens its own turn (regression guard) | `TestTurnTracking::test_a_later_conversation_cycle_with_different_text_supersedes_the_open_turn` | exit `0` |
| Auxiliary-kind burst never evicts a real turn (found live) | `TestPipelineHistoryBuffer::test_auxiliary_kind_burst_never_evicts_a_real_turn` | exit `0`, the real cycle survives `2 × PIPELINE_HISTORY_LIMIT` auxiliary-kind publishes |

## Scrutiny Validator

- Confirm `_open_turn_by_session` is only ever mutated under the same lock
  `PipelineEventBus.publish`/`subscribe` already use — a second, unguarded
  lock (or none) reopens exactly the kind of subscribe/publish race
  mission-12 already fixed once for the ring buffer.
- Confirm `pre_tool_use.py`'s new `report(...)` calls sit at existing
  branch points and do not themselves become something that can raise
  *before* the existing detokenize/vault logic runs — a `report()` call
  that isn't actually wrapped in its own blanket `except` (relying only on
  the shared helper's internal try/except) is fine; a call site that adds
  a new `try`/control-flow branch around the *existing* logic is scope
  creep and a new place to introduce a regression.
- Confirm neither hook script's existing `return` value, `stdout` JSON
  shape, or stderr message text changed at any branch — diff both files
  and check every pre-existing line is untouched except for the inserted
  `report(...)` calls themselves.
- Confirm `/veil-panel/hook-event`'s handler never does anything with an
  unrecognized/extra field beyond ignoring it — no logging of the raw
  request body anywhere (a debug log of "here's what I received" would
  quietly reopen the exact leak this mission's schema design is meant to
  foreclose).
- Confirm the frontend's turn/child grouping and timeline merge sort by
  the event's own `timestamp` field (already present on every event type),
  not by array-append order across three different fetch/stream sources,
  which could silently misorder entries under real network jitter.
- Confirm a `tool_continuation` cycle with no `parent_request_id` really
  does render as its own visible chip in the frontend (the documented
  fallback), not silently vanish — re-read the selector's filter condition
  against this specific case before approving.

## User-Testing Validator

- No live spike was run for the reclassification, the turn map, or the new
  endpoint — pytest is the proof of correctness there; a human must still
  confirm the *live* experience: run a real demo-db query end to end
  through both the gateway and the hooks together (both processes actually
  running, not mocked), and confirm exactly one chip appears, its timeline
  shows PreToolUse/PostToolUse markers in the right place, and the gap
  between "PreToolUse done" and "PostToolUse start" visibly tracks the
  real query's wall-clock time.
- Confirm visually that an in-progress hook step actually shows a live
  pending/spinner state before its "done" event arrives, not just a
  correctly-ordered-after-the-fact log once everything has already
  finished (timing this by eye against a deliberately slow query, if one
  is easy to construct against the demo db, is the strongest version of
  this check).
- Confirm the existing `title_generation`/`internal_continuation`
  hide-by-default behavior and its "Show hidden" toggle still work exactly
  as before this mission — this mission must not regress mission-12's
  already-approved behavior while extending it.
- Write the actual checklist steps taken and their outcomes into
  `.harness/handoff.md`, including whether a real demo-db query was
  available to test against or whether this had to be simulated.
