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

## Post-writeup fix #2 (over-hiding regression, found during the human's own live testing)

After the first post-writeup fix above shipped, the human reported it
over-corrected: the cycle selector was now hiding essentially every real
conversational turn, and the Input pane still showed injected wrapper
text instead of exactly what they typed. Root cause: `_classify_cycle_kind`'s
`internal_continuation` check was `received_text.strip().lower().startswith("<system-reminder")`.
Claude Code prepends a `<system-reminder>` block before the human's own
typed text on essentially every real turn (visible in this very
project's own live sessions), not just on turns that are *entirely*
reminder content — so the prefix check fired on almost everything.

Fix: a new `_strip_injected_wrapper_tags` (`adapters/gateway/server.py`)
removes known Claude-Code-injected framing blocks --
`<system-reminder>`, `<local-command-caveat>`, `<command-name>`,
`<command-message>`, `<command-args>`, `<local-command-stdout>`,
`<session>` -- from `received_text`/`outbound_text` before they are
classified or displayed. `_classify_cycle_kind` now takes the
*already-stripped* text and only returns `"internal_continuation"` when
nothing human-authored survives the stripping, not on a prefix match.
`<session>` is included because a human tester live-reported it wrapping
part of an *ordinary* conversational turn's content, not only Claude
Code's dedicated title-generation request (which is still identified
independently, via `_TITLE_GENERATION_SYSTEM_MARKER` in the system
prompt) -- so its mere presence must never by itself hide a cycle.

New tests in `tests/test_gateway.py`: `TestCycleKindClassification::test_a_real_turn_prefixed_with_a_system_reminder_is_not_hidden`,
`::test_session_tag_around_real_content_does_not_force_title_generation`,
and `TestMessageTextExtraction::test_input_pane_shows_exactly_the_human_typed_text_no_more_no_less`
(asserts the Input pane text equals the human-typed string exactly, with
no wrapper-tag substrings present). Full `.harness/bin/verify` re-run:
exit 0.

**Live re-verification.** Started a real gateway process
(`VEIL_LLM_BASE_URL=http://192.168.31.123:1234 VEIL_LLM_MODEL=qwen_qwen3-4b-instruct-2507`,
a reachable LM Studio instance) and, from a separate process (never this
session's own connection, per this project's established pattern),
`ANTHROPIC_BASE_URL=http://127.0.0.1:8788 claude -p "Soy Alfonso, mi DNI
es 12345678A. Dame la informacion de esta manera: DNI: [DNI_NUMBER],
NOMBRE: [NOMBRE]"`. Captured the pipeline-stream's replayed backlog
afterward (raw socket read of `/veil-panel/pipeline-stream`, since the
in-memory ring buffer holds the last `PIPELINE_HISTORY_LIMIT` cycles).
The `"received"` stage of the conversational cycle was exactly
`"Soy Alfonso, mi DNI es 12345678A. Dame la informacion de esta manera:
DNI: [DNI_NUMBER], NOMBRE: [NOMBRE]"` -- the literal typed text, no
`<system-reminder>`/`<session>` wrapper, confirming the fix live, not
just in tests. `claude -p`'s own CLI wrapping did not add a
`<system-reminder>` block ahead of the human text in this run (an
interactive session's transcript, including this one's, shows it does in
that mode) -- both shapes are now handled correctly either way, since the
fix strips the tag if present and is a no-op if absent.

## Live investigation of the reported output-format issue (found same session, NOT fixed here)

The human separately reported: "the format is wrong" when asking, in the
same message, for the answer formatted as `DNI: [DNI_NUMBER], NOMBRE:
[NOMBRE]`. Investigated with the same live gateway/`claude -p` capture
above rather than guessing (this project's own precedent: the original
`<session>`/title-generation finding earlier in this mission was also
only found by capturing real traffic, not by reasoning about it).

**Finding: this is not a text-extraction or substitution bug.** The
`"detokenized"`/`"displayed"` pipeline stage for the cycle that produced
the actual answer read exactly `"DNI: 12345678A, NOMBRE: Alfonso\n\nNota:
..."` -- the requested template, byte-for-byte, values correctly resolved
from `PII_DNI_0001` back to `12345678A`. `response_received` (pre-
detokenization) showed `"DNI: PII_DNI_0001, NOMBRE: Alfonso"` --
confirming the LLM itself received and honored the requested format; our
substitution round-trip did not corrupt it.

**What actually looks broken:** Claude Code's own agent loop split this
one logical "turn" across *two separate gateway request/response cycles*.
The cycle whose `"received"` stage shows the human's own exact typed
text (`request_id` `8a36d68d...` in this run) got an **empty** response
in this pipeline capture. The actual formatted answer landed on a
*different* cycle (`e00a2404...`) whose own `"received"` stage is just
the generic placeholder `"[tool_result]"` -- i.e., a follow-up request
Claude Code made as part of its own agent loop, not something that reads,
in the panel, as connected to the human's question at all. A human
looking at the panel, selecting the chip that shows their own message,
would see a blank Output pane, while the real (correctly formatted)
answer sits under an unrelated-looking `"[tool_result]"` chip -- which is
exactly the kind of thing a user would describe as "the format is wrong"
even though every byte of the actual text is correct.

**Why this is not fixed in this pass:** this is a materially different,
larger problem than the wrapper-tag/over-hiding bug above -- it is about
*which gateway request/response cycles belong to the same logical
conversational turn*, a correlation this mission's per-HTTP-request cycle
model does not attempt. Fixing it needs a real design decision (e.g.,
chaining cycles within one `session_id` whose predecessor's response
`stop_reason` was `tool_use`, and how to render that chain in the
selector/panes) that deserves its own Intake Grill, not a guess bolted
onto this reopening. Flagged for human follow-up below rather than
silently scope-creeped in.

## Remaining for human follow-up

- **New, needs its own scoping decision: cross-cycle correlation for the
  "output format" report.** See "Live investigation of the reported
  output-format issue" above. The text itself is correct end to end;
  what's missing is a way for the panel to show one logical conversational
  turn (which can span multiple gateway request/response cycles once
  Claude Code's agent loop makes an intermediate tool-use round-trip) as
  one connected thing, instead of the answer appearing to vanish from the
  cycle the human recognizes as "their" message. Needs a human decision on
  approach before a mission is scoped for it.
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
