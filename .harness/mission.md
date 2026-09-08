# Mission

## Objective

Follow-up to mission-11 (audit log + observability panel), reported after
live use of the real gateway (LLM + API) flow. Three problems, all in the
observability surface only — no change to detection, vault, or
tokenization/substitution logic:

1. The four panes render the *entire raw* request/response JSON (system
   prompt, tools, metadata, full conversation history) instead of BRIEF
   §5's actual spec ("the user's text ... the same text, tokenized ...").
   This is a bug against BRIEF's own design, not new scope: `_publish_stage`
   calls in `adapters/gateway/server.py::GatewayHandler._handle` pass
   `_decode(raw_body)`/`_decode(forward_body)`/the whole joined SSE stream
   text verbatim. A side effect of the same bug: `Span.start`/`Span.end` are
   offsets into one string *leaf* (`core/tokenizer.py::_tokenize_text`), but
   the frontend slices them into the *whole document* text — spans land on
   effectively random characters near the front of the payload for any
   real (multi-field) request, which is the concrete reason the panes read
   as illegible garbage.
2. `PipelineEventBus` (in-memory pub/sub) delivers only events published
   *after* a subscriber connects. Opening the panel against an
   already-running gateway shows nothing for cycles that already completed
   in that same process's lifetime — accepted as fine across a *restart*
   (mission-11's Intake Grill), but not within one running process.
3. New, scope-expanding asks from this mission's own Intake Grill: (a) a
   selector so a new request cycle does not overwrite the one you are
   looking at; (b) the metrics pane becomes its own detailed tab comparing
   regex/LLM/unified, expensive enough (real LLM calls) that its result
   must be cached to disk and reused across gateway restarts, refreshed only
   on an explicit action; (c) an interactive step-through of a *completed*
   cycle's six pipeline stages (manual prev/next and an auto-play), because
   the real live timing is too fast to watch meaningfully.

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

## Intake Grill

| Question | Answer |
|---|---|
| Is showing only the human-readable message (vs. the full raw JSON) a bug fix against BRIEF §5's original spec, or something broader (e.g. keep raw JSON available, collapsed)? | Bug fix: each pane shows only the human message. No new "raw JSON, collapsible" affordance. |
| Where should message-text extraction happen — server (gateway/server.py, which already has the parsed JSON) or frontend (JS re-parsing the Messages API shape)? | Server. The frontend receives plain display text and detector spans already relative to that text; it does not need to understand the Anthropic Messages API shape at all. |
| Claude Code resends the full conversation history every request. What should pane 1 ("Input") show? | Superseded by the user's own follow-up: instead of picking "last turn only" vs "whole history" once, the panel gets a selector across cycles (see below) — each cycle's pane 1 shows that one request's latest user turn only (the newest content, not the repeated history), scoped per-cycle rather than per-pane. |
| The pipeline-stream is purely in-memory; what should happen for cycles that completed before the panel connected? | Bounded in-memory ring buffer of the last N cycles, replayed to a new SSE subscriber the moment it connects, then live events continue. Still lost on process restart — consistent with mission-11's existing non-persistence decision for this stream. |
| When a new request cycle starts while you are looking at an older one, what happens? | You are not pulled away. The new cycle is added to the selector as the newest entry; your current selection changes only if you were already viewing "the latest" (auto-follow), not if you had explicitly picked an older one. |
| How many cycles to buffer (both for replay-on-connect and for the selector)? | 20 (same order of magnitude as the existing `MAX_AUDIT_ROWS=50` convention). |
| The metrics tab compares regex/LLM/unified; LLM/unified modes call the local LLM once per manifest entry — slow, and can fail if LM Studio is not running. How should that comparison be triggered, and what should persist? | Explicit "Run comparison" action (never automatic). Result is cached to disk (`.veil/`, git-ignored, like the vault/audit files), not just in-memory for the session — the panel's default view on open is *the last comparison that was ever run*, surviving a gateway restart, because rerunning it is expensive. |
| For the interactive step-through of a completed cycle's pipeline stages, is manual (prev/next) enough, or is an auto-play needed too, given real-time is too fast to watch? | Both. Manual prev/next at any time; a "Play" button auto-advances through all six stages with a fixed per-stage pause, for a completed cycle only (a still-in-flight cycle only has the stages it has reached so far, so `Play` is disabled until all six have arrived). |

Evidence gathered before asking (no re-reading needed once the grill
started): `adapters/gateway/server.py` read in full this session — the
`_publish_stage` call sites in `_handle`, `PipelineEventBus.subscribe`/
`publish`, `_tokenize_body`, `_relay_sse_response`/`_relay_buffered_response`
and their `(raw_text, rewritten_text)` return contracts, `_TEXT_BEARING_DELTA_FIELDS`,
`_split_sse_event`; `frontend/panel.html` read in full — `Panes`,
`renderSpannedText`/`renderTokenizedText`, `useEventSource`, `AuditLogPane`,
`VaultInspector`, `Metrics`, `App`'s flat per-stage `texts` state (the thing
that "overwrites the last message" per the user's own diagnosis);
`core/tokenizer.py::_tokenize_text`/`tokenize` confirming `Span` offsets are
per-leaf, not per-document, and that `context.span_sink` accumulates spans
from every leaf in tree-walk order with no leaf identity attached (why
per-cycle re-extraction, not post-hoc filtering of the existing span list,
is the only safe fix); `core/detector.py` confirming `LLMClient`/`detect()`
cache LLM results process-wide keyed by `(text, client.identity)`
(`_llm_span_cache`), so re-running `detect()` on the exact same leaf text
already scanned by `tokenize()` is a guaranteed cache hit, not a second LLM
call, provided the *same* `LLMClient` instance is reused across both calls;
`demo/eval.py::evaluate()` already accepts `mode: Literal["regex","llm","union"]`
plus `llm_client` (mission-11 item 16) — no core/demo signature change
needed for the comparison tab, only a new gateway endpoint that calls it
three times and persists the result; `.gitignore` already excludes `.veil/`.

## Scope

- `veil_pii_middleware/adapters/gateway/server.py`:
  - New pure-extraction helpers (no detection, no substitution — this stays
    firmly in the adapter, not `core/`): `_extract_text_from_content_blocks`
    (handles both a plain string `content` and a list of content blocks,
    joining `text`/`thinking` blocks and substituting a short bracketed
    placeholder like `[tool_use]` for any other block type so a turn that is
    e.g. a bare tool_result never renders as a mysteriously empty pane),
    `_extract_last_user_message_text` (walks a request body's `messages`
    array backwards for the last `role == "user"` message), `_extract_response_text`
    (same block-joining over a Messages API response's `content`), and an SSE
    variant that walks a list of raw SSE event strings collecting
    `content_block_delta` text/thinking deltas in order (reusing the already-
    parsed `_split_sse_event`/`_TEXT_BEARING_DELTA_FIELDS`).
  - `_tokenize_body` builds its own `LLMClient` (via the same
    `LLM_BASE_URL_ENV_VAR`/`LLM_MODEL_ENV_VAR` env gating `tokenizer._default_llm_client`
    already uses) and passes it into `tokenize()` explicitly, instead of
    letting `tokenize()` build+close an invisible one — needed so the same
    client can be reused for one extra `detect()` call, scoped to only the
    extracted last-user-message text, to get spans whose offsets are
    correctly relative to that exact display text (not the whole document).
    Closed in a `finally` in `_tokenize_body` itself. Returns the tokenized
    body bytes plus `(received_text, received_spans, outbound_text)` in
    place of today's single flat `spans` list.
  - `_relay_buffered_response`/`_relay_sse_response` return
    `(raw_display_text, rewritten_display_text)` extracted via the helpers
    above, instead of the full joined raw/rewritten stream text. The actual
    bytes written to the real client via `_write_chunk` are unchanged byte-
    for-byte — only the pipeline-stream's own side-channel text changes.
  - `_handle`'s `_publish_stage` calls pass these new display texts/spans
    instead of `_decode(raw_body)`/`_decode(forward_body)`/the full relay
    text. The two early-failure branches (invalid JSON, tokenize exception)
    keep falling back to the raw decoded body — there is no structured
    message to extract when parsing itself failed.
  - `PipelineEventBus` gains a bounded ring buffer (`collections.deque(maxlen=20)`
    of request ids in first-seen order, plus a dict of that request id's
    events so far) updated inside `publish`'s existing lock. `subscribe()`
    now returns `(queue, backlog_events)`: the backlog is a snapshot (under
    the same lock used by `publish`, so no event is ever delivered twice or
    dropped across the subscribe/publish race) of every buffered event for
    every currently-buffered request id, oldest cycle first; `_serve_pipeline_stream`
    writes the backlog as SSE chunks before entering its existing blocking
    live loop.
  - New endpoints for the metrics comparison tab:
    - `GET /veil-panel/metrics/comparison` — returns the persisted
      `.veil/metrics_comparison.json` cache (`{"available": false}` if it
      has never been run).
    - `POST /veil-panel/metrics/comparison` — runs `demo.eval.evaluate()` for
      `mode="regex"`, `"llm"`, `"union"` (each independently wrapped so one
      mode's failure, e.g. LM Studio unreachable, does not block the
      others), serializes `by_type`/`by_detector` per mode, writes the
      result plus an ISO timestamp to `.veil/metrics_comparison.json`
      (git-ignored, matching `VAULT_DB_PATH`'s convention), and returns it.
      `demo/eval.py`'s private `_llm_client_from_env` is promoted to a
      public `llm_client_from_env` (added to `__all__`) so the gateway can
      build the same kind of client `main()` already does, instead of
      duplicating that env-reading logic a third time.
  - The existing `GET /veil-panel/metrics` (quick regex-only report) is
    unchanged — it remains the fast, always-fresh summary; the new
    comparison endpoints are additive.
- `veil_pii_middleware/demo/eval.py`: rename `_llm_client_from_env` to
  `llm_client_from_env`, export it in `__all__`; `main()` updates its one
  call site. No behavior change.
- `veil_pii_middleware/frontend/panel.html`:
  - Top-level tabs: "Live" (default) and "Metrics". The old inline `Metrics`
    pane moves entirely into the new "Metrics" tab and gains the
    regex/LLM/unified comparison table (by entity type and by detector),
    an explicit "Run comparison" button, a "last run at <time>" label
    sourced from `GET .../comparison` on load, and per-mode
    loading/error states from `POST .../comparison`.
  - "Live" tab: a cycle selector (one entry per buffered/live `request_id`,
    newest last, labeled by a short id fragment + timestamp + a truncated
    preview of that cycle's input text) replaces the old flat per-stage
    `texts` state that a new request silently overwrote. Selecting an older
    cycle does not move when a new one arrives (pin-to-latest only follows
    while the latest cycle is the one selected); the four panes render the
    *selected* cycle's stages.
  - The pipeline flow diagram gains prev/next controls and a play/pause
    auto-play (fixed per-stage delay) that step a per-cycle "viewed stage"
    index, highlighting the corresponding stage box and showing a short
    canned one-line explanation of what that stage does; the four panes
    progressively reveal only up through the viewed stage. `Play` is
    disabled until the selected cycle has all six stages. A still-in-flight
    live cycle keeps auto-advancing its viewed index as real events arrive
    (matching today's live-highlight behavior) unless the user has manually
    stepped it, in which case it stays put until they return to "latest".
  - Both the selector list and the per-cycle stage map are capped
    client-side at the same 20-cycle bound the server buffers, so a
    long-running session cannot grow this state unboundedly.
- Tests: extended `tests/test_gateway.py` — message-text extraction (last
  user turn only, non-text content blocks placeholder-rendered, SSE
  text/thinking delta reconstruction), span offsets now correctly relative
  to the extracted display text (a multi-field payload where the naive
  whole-document offset would land on the wrong characters), the pipeline
  ring buffer (a late-connecting subscriber receives already-completed
  cycles, oldest first, still bounded at 20, still never persisted to the
  audit log), and the two new metrics-comparison endpoints (cache miss vs.
  hit, one mode failing does not block the others, the persisted file
  survives across two separate `make_handler` instances the way the vault/
  audit files already do). `tests/test_eval.py` (or wherever `demo/eval.py`
  is currently tested) covers the `_llm_client_from_env` → `llm_client_from_env`
  rename.
- `.harness/handoff.md`: an updated manual demo-run checklist covering the
  cycle selector's pin/no-pin behavior, the step-through/auto-play, and the
  metrics comparison tab's persistence across a gateway restart.

## Non-goals

- Any change to `core/detector.py`, `core/tokenizer.py`'s substitution
  logic, or `core/vault.py` — this mission is entirely inside the gateway
  adapter's observability side-channel and the frontend. The one
  `core/tokenizer.py`-adjacent change is `_tokenize_body` now managing its
  own `LLMClient` lifecycle explicitly instead of letting `tokenize()` do it
  invisibly — `tokenize()`'s own signature, defaults, and behavior for every
  other caller are untouched.
- Changing what bytes the gateway actually forwards upstream or relays back
  to the real client — only the pipeline-stream's own side-channel display
  text changes; the wire-level relay (`_write_chunk` calls, SSE rewriting,
  detokenization) is byte-for-byte identical to before this mission.
- Persisting the pipeline-stream itself to disk, or surviving a gateway
  *restart* — the 20-cycle ring buffer is still purely in-memory, per
  mission-11's existing decision; only the *within-one-running-process,
  connect-after-the-fact* gap is fixed.
- Any authentication layer on the new `/veil-panel/metrics/comparison`
  routes — same `127.0.0.1`-only trust model as every other panel route.
- A full animation/motion polish pass beyond the specific step-through
  interaction requested (fixed-delay auto-play, stage-box highlight, one-line
  captions) — no easing curves, particles, or transitions beyond what
  mission-11's MVP CSS already has.
- Showing the manifest's raw `Miss.value` (the concrete missed synthetic PII
  value `demo/eval.py` already prints to stdout) in the comparison tab —
  only the aggregate `by_type`/`by_detector` numbers, to keep the persisted
  cache and the new endpoints small and scoped to what was asked.

## Acceptance criteria

1. For a realistic multi-field Claude Code-shaped request (system prompt +
   tool definitions + multi-turn `messages` history), pane 1 ("Input")
   shows only the latest user turn's text, and any regex/LLM span
   highlighting lands on the correct characters of *that* text — proven by
   a test with a payload where the naive whole-document-offset approach
   would provably highlight the wrong substring.
2. A non-text last-user-turn content block (e.g. a bare `tool_result`, no
   text block) renders a placeholder rather than an empty pane, proven by
   test.
3. Pane 2/3/4 show only reconstructed human-readable text (the tokenized
   user message; the model's response text; the detokenized response
   text) for both the buffered-JSON and the SSE streaming response shapes,
   proven by test for each shape — no `event:`/`data:` framing, no
   `message_start`/`content_block_start` envelope JSON, leaking into any
   pane's text.
4. A client connecting to `/veil-panel/pipeline-stream` *after* one or more
   complete request/response cycles have already run receives every stage
   event of up to the last 20 of those cycles (oldest first) before any new
   live event, proven by test; the audit log still never contains any of
   this, unchanged from mission-11's existing proof.
5. `GET /veil-panel/metrics/comparison` before any `POST` returns
   `{"available": false}` (or the actually-persisted last run, if a prior
   test/process already wrote one to the same file;
   test isolates this with a `tmp_path`-scoped cache file). `POST
   /veil-panel/metrics/comparison` persists a file that a *second*,
   independently-constructed handler (simulating a gateway restart) can
   read back via `GET`, proven by test.
6. `POST /veil-panel/metrics/comparison` still returns the regex mode's
   result when the LLM mode fails (e.g. no `LLM_BASE_URL_ENV_VAR`/
   `LLM_MODEL_ENV_VAR` configured), marking only the failing mode(s)
   unavailable, proven by test.
7. The frontend (manually verified per `handoff.md`'s checklist): opening
   the panel against a gateway that already processed requests shows their
   cycles in the selector; selecting an older cycle and then triggering a
   new request does not change the selection; the pipeline diagram's
   prev/next/play controls step a completed cycle's six stages with the
   panes progressively revealing; the Metrics tab shows a regex/LLM/unified
   comparison that survives a gateway restart until "Run comparison" is
   pressed again.
8. `.harness/bin/verify` exits 0.
9. `.harness/handoff.md` documents the plaintext-never-in-the-persisted-cache
   proof (the comparison cache holds only aggregate counts, never a value),
   the updated manual demo-run checklist, and anything left for human
   follow-up.

## Risk

Risk class: medium

Rationale: this mission edits `adapters/gateway/server.py`, one of the
project's designated high-risk files by virtue of being the live proxy —
but strictly on its observability side-channel (what the panel is shown),
never on the actual request/response bytes relayed to the real client or
on `core/vault.py`/`core/tokenizer.py`'s substitution/detection logic, which
CLAUDE.md's own risk doctrine reserves the "high" class for. The main
failure mode to guard against in review is a regression in the *real*
relay path while refactoring `_relay_buffered_response`/`_relay_sse_response`'s
return values — mitigated by keeping the actual `_write_chunk`/detokenize
calls untouched and only changing what the two functions *return* for the
pipeline-stream side channel, plus full existing-test-suite re-runs.
The new disk-persisted metrics cache is a new git-ignored file next to the
vault/audit files, following their existing convention, and holds no PII
(aggregate counts only).

## Merge policy

No commit, push, or merge without explicit human review of the diff and
`.harness/handoff.md`. This mission does not merge itself.

## Tooling And Approach Assessment

1. **What project type is this?** A bug fix (message-text extraction
   replacing whole-document text in the observability side channel) plus
   three additive frontend/backend features (cycle selector with pin-to-
   latest, disk-persisted metrics comparison, interactive stage step-
   through) — all inside the gateway adapter and the unbuilt React
   frontend already established by mission-11. No new architectural shapes.
2. **What methods, tooling, or approaches does that imply?** Stdlib only
   again (`collections.deque` for the ring buffer, `json`/`Path` for the
   comparison cache file) — no new Python runtime dependency. Frontend
   stays CDN React + Babel Standalone, no bundler. Reuses `core.detector`'s
   existing process-wide LLM response cache (`_llm_span_cache`) to make the
   one extra per-cycle `detect()` call for display-span offsets effectively
   free rather than a second network round trip, as long as the same
   `LLMClient` instance is threaded through.
3. **What risks or missing decisions does it reveal?** (a) The
   `PipelineEventBus` backlog-snapshot-under-lock design must be exactly
   right or a subscriber can double-receive or drop an event at the
   subscribe/publish race boundary — worked through in the Intake Grill's
   evidence section; validation needs a concurrency-shaped test, not just a
   sequential one. (b) Reusing one `LLMClient` across `tokenize()`'s
   document-wide pass and the extra per-leaf `detect()` call only avoids a
   second network call when the extracted display text is byte-identical
   to a leaf `tokenize()` already scanned — true for the common single-
   string-content case, not guaranteed if a message's content is multiple
   text blocks joined with `"\n"` (a string tokenize() never scanned as one
   piece); accepted as a minor, documented cache-miss edge case, not a
   correctness bug. (c) The metrics comparison cache file must never grow
   into a de facto second vault — validation asserts it holds only
   aggregate counts, no `Miss.value`. (d) Frontend state complexity grows
   meaningfully (per-cycle stage maps, pin/no-pin selection, manual vs.
   auto-follow viewed-stage index) — this is a "verify by hand" surface
   pytest cannot reach; the manual checklist in `handoff.md` must be
   concrete enough to actually catch a regression here.
4. **What should be added to validation?** All the standard gates; a
   concurrency test for the pipeline ring buffer's subscribe/publish
   ordering; span-offset-correctness tests against a realistic multi-field
   payload (the actual bug being fixed); comparison-cache persistence
   across two independently-constructed handlers; partial-failure
   (regex-succeeds-llm-fails) coverage for the comparison endpoint; a
   detailed manual checklist for everything only a browser can show.
5. **What is explicitly not needed now?** Any core detection/vault/
   tokenizer change; disk persistence of the pipeline-stream itself; a new
   auth layer; animation polish beyond the specific step-through ask;
   surfacing raw missed-value detail in the comparison tab.

## Status

implemented — awaiting human review for merge (see `.harness/handoff.md`).

Root cause of the three live-testing observations, confirmed by capturing
raw traffic through a throwaway diagnostic proxy that bypassed this
module's own extraction entirely: all three are genuine Claude Code
requests, not bugs. (1)/(3) `<session>...</session>` plus "write a title"
plus a `{"title": "..."}` response is Claude Code's own dedicated
session-title-generation request — its own system prompt literally says
"You are naming a coding session...". (2) The `<system-reminder>` cycle is
a real agent-loop continuation turn (token-budget reminders, tool
results), not human-authored input. Both are legitimate but not
conversational, so the fix is: classify each cycle server-side
(`_classify_cycle_kind` in `adapters/gateway/server.py`, a new `"kind"`
field on every pipeline-stage event) and hide `"title_generation"`/
`"internal_continuation"` cycles from the panel's selector by default,
with a "Show N hidden" toggle to reveal them. Covered by three new tests
in `TestCycleKindClassification` (`tests/test_gateway.py`) and re-verified
live with a second Playwright pass, which also caught and fixed a real,
unrelated latent bug: the "Jump to latest" button never actually moved the
selection, only recomputed the pinned flag against whatever was already
selected.
