# Handoff

## Summary

Follow-up to mission-11, reported by the human after live use of the real
gateway (LLM + API) flow: the observability panel's four panes were
illegible (full raw JSON/SSE framing instead of the human message), a panel
opened against an already-running gateway showed nothing for cycles that
already completed, and — in the human's own follow-up spec expansion —
requests for a cycle selector, an interactive pipeline step-through, and a
persisted regex/LLM/union metrics comparison tab. No change to
`core/detector.py`, `core/vault.py`, or `core/tokenizer.py`'s substitution
logic; everything is inside `adapters/gateway/server.py`'s observability
side channel, `demo/eval.py` (one rename), and `frontend/panel.html`.

## What changed

- **Message-text extraction (the actual bug fix).** `Span` offsets are
  per string-leaf (`core/tokenizer.py`), but the pre-mission-12 code sliced
  them into the *whole raw JSON document* text — which is why spans landed
  on the wrong characters for any real, multi-field payload, and why every
  pane showed system prompt/tools/history noise instead of the message.
  `adapters/gateway/server.py` now extracts, server-side: the last
  `role: "user"` message's own text (`_extract_last_user_message_text`,
  scoped to *this turn*, not the resent conversation history), the model's
  reconstructed reply text for both response shapes
  (`_extract_response_text` for buffered JSON, `_extract_sse_display_text`
  for the SSE delta stream), and one extra, scoped `detect()` call (reusing
  the same `LLMClient` instance as the real `tokenize()` call, so it is a
  cache hit, not a second LLM round trip whenever the message is a single
  string) to get spans whose offsets are correct against that shorter
  display text. A non-text last-turn content block (e.g. a bare
  `tool_result`) renders a `[tool_result]`-style placeholder rather than an
  empty pane. **The actual bytes relayed to the real client are byte-for-byte
  unchanged** — only the pipeline-stream side channel's text changed.
- **Pipeline history ring buffer.** `PipelineEventBus` now keeps the last
  `PIPELINE_HISTORY_LIMIT` (20) cycles' events in memory and replays them to
  any new `/veil-panel/pipeline-stream` subscriber before live events
  continue. The backlog snapshot and the subscriber-list snapshot both
  happen under the same lock as `publish()`'s own bookkeeping, so an event
  is delivered to a given subscriber exactly once (backlog or live, never
  both, never neither) regardless of thread interleaving — proven by a
  direct concurrency test on `PipelineEventBus` itself (200 published
  cycles racing against repeated subscribe/unsubscribe). Still purely
  in-memory; a process restart still loses it, unchanged from mission-11.
- **Metrics comparison, persisted.** `GET`/`POST /veil-panel/metrics/comparison`
  run `demo/eval.py`'s existing `mode="regex"|"llm"|"union"` `evaluate()`
  and persist the result (aggregate `by_type`/`by_detector` counts only —
  never a manifest `Miss.value`) to `.veil/metrics_comparison.json`
  (git-ignored, same convention as the vault/audit files). One mode failing
  (no LM Studio reachable, no env config) never blocks the other two. The
  existing `GET /veil-panel/metrics` (quick regex-only report) is
  unchanged. `demo/eval.py::_llm_client_from_env` is renamed to the public
  `llm_client_from_env` so the gateway reuses it instead of a third
  duplicate of that env-reading logic.
- **Frontend (`frontend/panel.html`), rewritten:**
  - "Live" / "Metrics" top-level tabs; the old inline Metrics pane moved
    entirely into the Metrics tab, which shows a regex/LLM/union comparison
    table (by entity type and by detector), a "Run comparison" button, and
    a "last run at …" label — defaulting to whatever the persisted cache
    already holds, never running automatically.
  - A cycle selector (one chip per buffered/live request, newest first) on
    the Live tab replaces the old flat per-stage state a new request used
    to silently overwrite. Selecting an older cycle does not move when a
    new one arrives; only a cycle you were already "pinned" to (the latest)
    keeps auto-following.
  - The pipeline diagram gained Prev/Next/Play controls and a one-line
    stage caption. Play is disabled until the selected cycle has all six
    stages (a still-in-flight cycle can only be stepped through what has
    arrived so far); the four panes progressively reveal only up through
    the viewed stage.

## Proof the persisted comparison cache never carries plaintext

`tests/test_gateway.py::TestMetricsComparisonEndpoint::test_persisted_cache_holds_no_raw_manifest_value`
runs `evaluate()` (monkeypatched) returning a `Report` whose `misses` list
contains a real-looking email value, POSTs the comparison, then asserts the
persisted `.veil/metrics_comparison.json` file contains neither that email
nor a `"value"` key anywhere — `_serialize_report` only ever emits
aggregate `by_type`/`by_detector` counts, never `Miss` detail.

## Test coverage added

`tests/test_gateway.py`: `TestMessageTextExtraction` (last-turn-only
extraction with correctly aligned spans against a realistic multi-field
payload; an explicit regression guard proving the naive whole-document
offset would have been wrong; the non-text-block placeholder),
`TestResponseTextExtraction` (buffered JSON and multi-block SSE response
reconstruction, both pre- and post-detokenization), `TestPipelineHistoryBuffer`
(late-subscriber backlog replay, oldest first; the 20-cycle bound and
oldest-first eviction), `TestPipelineEventBusConcurrency` (the
subscribe/publish race, directly on `PipelineEventBus`), and
`TestMetricsComparisonEndpoint` (cache miss, persistence across a simulated
restart via a second independent `make_handler`, partial-mode failure, no
raw value in the cache). `tests/test_demo.py` covers the
`llm_client_from_env` rename. All pre-existing tests pass unmodified.

## Manual verification actually performed this mission

Ran the real gateway (`python -m veil_pii_middleware.adapters.gateway.server`)
against a small local fake upstream (no LM Studio instance was reachable
this mission, so the LLM/union comparison modes were only exercised via the
partial-failure test above, not live) and drove a real headless Chromium
browser against it with Playwright (`npx playwright install chromium`,
scripted, not committed to the repo):

- Panel loads with no console/page errors.
- Two real requests through the gateway produced two selectable cycle
  chips; the Input pane showed only the second request's own message text
  (not the first's, not JSON structure); the Developer-view pane showed
  the model's reconstructed reply.
- Selecting the older cycle, then firing a third request, left the
  selection on the older cycle (did not jump to the new one) while the
  chip count grew to three — the pin-to-latest behavior works as specified.
- "Jump to latest" plus Prev/Next moved the stage caption/highlight as
  expected; "Play" was enabled only once the selected cycle had all six
  stages, and auto-played through to "displayed".
- Metrics tab showed "no comparison has been run yet" before any POST.
- `POST /veil-panel/metrics/comparison` returned 200 (not 500) with all
  three modes correctly reporting `available: false` and a real error
  reason (missing demo manifest in that scratch run) rather than crashing;
  a subsequent `GET` read back the exact same persisted result.

## Post-writeup fix (found during the human's own live testing)

After this document was first written, the human tested the panel live
against the real Anthropic API and reported three things that looked
wrong. Investigated with a throwaway raw-capture MITM proxy (never
committed) standing in for the real upstream, so the raw request bodies
could be inspected completely independent of this codebase's own
extraction logic:

- The `<session>...</session>` wrapper plus a trailing "write a title"
  instruction the human saw in the Input pane, and a separate cycle's
  response being a bare `{"title": "..."}` JSON object, are **both real**:
  Claude Code fires a dedicated request once per session whose own system
  prompt says "You are naming a coding session so the user can pick it out
  of a long list of sessions" — our extraction was faithfully showing
  exactly what was sent, not a bug.
- The cycle whose only content was a `<system-reminder>` block is a real
  agent-loop continuation turn (token-budget reminders, tool results,
  sub-agent instructions) — also not human-authored input, also not a bug
  in the extraction.

Both are legitimate Claude Code requests, just not conversational ones the
human wants cluttering the cycle selector. Fixed by adding
`_classify_cycle_kind` (keys off a stable phrase in the title-generation
system prompt, and off the `<system-reminder` prefix Claude Code itself
uses for injected content) and publishing its result as a `"kind"` field
on every pipeline-stage event; `frontend/panel.html`'s cycle selector now
hides `"title_generation"`/`"internal_continuation"` cycles by default,
with a "Show N hidden" toggle. New tests: `TestCycleKindClassification` in
`tests/test_gateway.py`. Explicitly a best-effort heuristic, not a
protocol guarantee — documented as such in the code, since Claude Code's
exact wording could change between CLI versions.

Re-verifying this live (a second Playwright pass) also caught a real,
unrelated latent bug introduced earlier in this same mission: the "Jump to
latest" button never actually changed the selection — it only recomputed
the `pinnedToLatest` flag by comparing whatever was *already* selected
against the newest cycle, which is a no-op unless they already matched.
Fixed to actually select the latest visible cycle.

## Remaining for human follow-up

- **The live LLM/union comparison numbers were never exercised against a
  real LM Studio instance this mission** (none was reachable) — only the
  partial-failure path (regex real, LLM/union unavailable) was proven, live
  and in tests. A human with LM Studio running should press "Run
  comparison" once and confirm the LLM/union numbers render sensibly.
- **Visual/UX judgment calls pytest cannot make**: whether the
  `[tool_result]`-style placeholder reads sensibly in context, whether the
  900ms auto-play pace feels right, whether the cycle-chip preview text
  truncation (36 chars) is a good length. Easy to tune in `panel.html`
  alone if not.
- Mission-11's still-open item (the pipeline-stream's deliberate raw-PII
  in-memory design, human sign-off requested) is unchanged by this mission
  and remains open from before.
