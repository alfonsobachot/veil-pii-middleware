# Handoff

## Summary

Redesigned the observability panel's grouping (mission-13, per
`.harness/mission.md`) so that one logical user request — a prompt plus
however many gateway/tool round-trips it takes to answer it — renders as
one screen, instead of one chip per raw gateway HTTP call, with the
`PreToolUse`/`PostToolUse` hooks' live progress folded into the same
screen. The first implementation pass was verified only against synthetic
curl/browser traffic and, correctly, was reported broken by the human
against a real `claude -p --demo-db` session. This document covers the
**revised, live-tested** implementation.

No change to `core/detector.py`, `core/vault.py`, or `core/tokenizer.py`'s
substitution logic — entirely inside the gateway's observability
side-channel, the two hook scripts' new best-effort reporting call, and the
frontend.

## What changed (final state)

- **`adapters/gateway/server.py`:**
  - `_classify_cycle_kind` recognizes four non-`"conversation"` kinds:
    `"title_generation"` (mission-12), `"safety_check"` (new — Claude
    Code's own internal harm classifier, `_SAFETY_CLASSIFIER_MARKER =
    "respond with <severity>"` checked on the received text itself, not
    the system prompt, since the marker text sits *outside* the
    `<transcript>` wrapper tag and so survives wrapper-tag stripping),
    `"internal_continuation"` (mission-12), and `"tool_continuation"` (new
    — a pure tool_result continuation, via `_last_user_content_has_text`).
  - `PipelineEventBus` turn-tracking is keyed by `(session_id,
    received_text)`, not by session alone: the *first* `"conversation"`
    cycle seen for a given key becomes that key's canonical turn id; a
    later `"conversation"` cycle with the *same* key folds under it
    (`parent_request_id`) instead of opening a new turn or stealing the
    per-session "open turn" pointer used for `tool_continuation`/hook
    attachment. A cycle of any other kind never touches this tracking.
  - `PipelineEventBus` keeps **two independent ring buffers**: `_order`
    (real turns — `"conversation"`/`"tool_continuation"`/unclassified) and
    `_aux_order` (`"title_generation"`/`"safety_check"`/
    `"internal_continuation"`), each its own `PIPELINE_HISTORY_LIMIT`
    (20)-slot budget, so a burst of hidden-kind cycles can never evict a
    real, still-relevant turn.
  - `POST /veil-panel/hook-event`: unchanged from the first pass — fixed
    four-field schema (`session_id`/`hook`/`phase`/`tool_name`),
    structurally incapable of carrying a command string or tool output.
- **`adapters/hooks/_panel_report.py`** and the two hook scripts:
  unchanged from the first pass.
- **`frontend/panel.html`** (revised per the human's own follow-up
  request):
  - `isTopLevelCycle` now folds a cycle under its parent whenever
    `parentRequestId` resolves, regardless of the cycle's own kind — not
    just `tool_continuation` children, but same-text sibling
    `"conversation"` cycles too.
  - The client-side `order`/`HISTORY_LIMIT` trimming is split into `order`
    (real turns) and `auxOrder` (hidden-kind cycles), mirroring the
    gateway's own two-buffer split, so the same client-side eviction
    starvation can't recur even over a long-running page session.
  - **Layout, changed per explicit request:** the four classic panes
    (Input / Outbound to API / API response / Developer view) are the
    panel's main content again, in a `#panes` grid identical to
    pre-mission-13. The mission-13 Timeline moves into a `#live-sidebar`
    column on the left instead of replacing the panes. Clicking a stage
    row in the sidebar both expands it inline (unchanged) *and* focuses
    that round for the main panes (`focusedRoundId`); the panes
    auto-follow the turn's latest round until the human clicks an earlier
    one, with a "● Following an earlier round — jump to latest" button to
    snap back (mirrors the existing pin-to-latest pattern for the turn
    selector itself).

## Three real bugs found via live testing, not the earlier synthetic pass

The first implementation was verified only with hand-crafted curl payloads
and one clean single-tool-call browser pass — both worked. A real
`claude -p --demo-db` session (real gateway, real hooks, real Anthropic
API) immediately showed 3-5 chips for one query. Captured the raw
`/veil-panel/pipeline-stream` backlog from that session (see
`docs/private/` is git-ignored so the raw capture itself isn't committed,
but the shapes below are transcribed verbatim from it) and found:

1. **Claude Code's own harm/safety-classifier request misclassified as
   `"conversation"`.** Its last user message:
   ```
   <transcript>
   {"user":"...", ...}
   </transcript>
   Stage 1 does NOT apply user intent or ALLOW exceptions — stage 2 will
   handle those. Respond with <severity>N</severity> ONLY. Grade HARM ONLY
   — do NOT reduce for user intent. No other text.
   ```
   Fires roughly once per tool call. Before it was recognized, it silently
   stole the turn-tracking pointer between two stages of the real
   conversation — observed live as a `PostToolUse: started` hook event
   tagged to the wrong chip. Also revealed, in passing, that this
   classifier's transcript embeds the real local scratch-directory path
   (including the real Windows username) — not a new leak (the gateway
   tokenizes every outbound request body unconditionally regardless of
   `cycle_kind`, which is a display-only classification), but worth a
   note for BRIEF's own risk table if a human wants to add one; out of
   scope for this mission.
2. **The same real turn produced two independent, complete
   `"conversation"` round-trips with byte-identical received text** (one
   live session; not reproduced on a second, simpler run — plausibly tied
   to the demo's own documented `sqlite3` CLI-not-installed retry path,
   not confirmed). Each had its own full tool-calling chain. The
   session-only turn pointer split them across two chips instead of
   folding them.
3. **Ring-buffer starvation.** A single real conversation with several
   tool rounds produced enough interleaved safety-check cycles (finding 1,
   before its fix) to fill and evict past a shared 20-slot buffer,
   including the real conversation's own cycle — reproduced live as **zero
   chips shown** for a query that had already completed successfully.

All three are fixed per "What changed" above and re-verified live (below).

## Live verification actually performed this mission

Three full `claude -p --demo-db` sessions run against the real gateway
(`scripts/run-gateway.sh`) and real `demo/settings.hooks.isolated.json`
hooks (not mocked), each against a fresh gateway process:

1. First real session (post-fix-1/2, pre-fix-3): reproduced finding 3 live
   — `curl`'s own backlog read showed the real conversation's stage events
   gone, evicted by ~14 safety_check cycles; a Playwright pass against the
   same still-running gateway confirmed `CHIP_COUNT=0`. This is what
   surfaced finding 3 and led to the two-ring-buffer fix.
2. Second real session (post all three fixes): `curl` backlog showed
   exactly one `"conversation"` cycle, all `hook` events tagged to it, all
   `tool_continuation` cycles correctly parented to it, all `safety_check`
   cycles correctly excluded from turn-tracking. A Playwright pass
   confirmed the new sidebar+panes layout: `CHIP_COUNT=1`, the four panes
   showing the latest round's real request/response (tokenized in pane 3,
   detokenized in pane 4), clicking an earlier "Tool round 1" sidebar row
   both expanded it and switched the panes to that round's data, and the
   "jump to latest" button appeared/worked. Zero browser console errors.
   Screenshots taken (`panel_sidebar_layout.png`, `panel_sidebar_focused.png`,
   not retained in the repo).
3. Third real session, different query, no retry needed this run: `curl`
   backlog showed one `"conversation"` cycle and one `"safety_check"`
   cycle surviving side by side without eviction, confirming the two-lane
   fix generalizes beyond the specific reproduction case.

One cosmetic gap noticed in pass 2, not chased further (small, non-blocking,
noted for a human to decide on): focusing an early intermediate round whose
own response was very short (e.g. just a tool call, little/no narration)
can render pane 3/4 fully blank instead of the italic "not reached yet"
placeholder — the placeholder only shows for a stage that never arrived at
all, not one that arrived with empty text. Cosmetic only; the underlying
data displayed elsewhere in that same round (via the sidebar's own inline
expand) is correct.

## Test coverage added (this pass)

`tests/test_gateway.py`:
- `TestCycleKindClassification::test_internal_safety_classifier_request_is_classified`
  — the exact live-captured `<transcript>...<severity>` shape classifies
  `"safety_check"`.
- `TestTurnTracking::test_a_later_conversation_cycle_with_different_text_supersedes_the_open_turn`
  — rewritten from the original (which asserted the now-superseded
  "different text still supersedes the pointer, no merge" behavior) to
  cover a genuinely different second turn opening its own turn.
- `TestTurnTracking::test_same_text_conversation_cycles_merge_under_one_turn`
  — new: two same-text conversation cycles fold under the first one seen;
  a later tool_continuation still attaches to that same original turn.
- `TestPipelineHistoryBuffer::test_auxiliary_kind_burst_never_evicts_a_real_turn`
  — a real conversation cycle published once, followed by `2 ×
  PIPELINE_HISTORY_LIMIT` auxiliary-kind cycles, is still present in the
  backlog.

All of mission-13's earlier test coverage (reclassification, hook-event
endpoint, best-effort hook reporting) is unchanged and still passing.

## Gate results

- `ruff check . && ruff format --check .` — pass.
- `mypy` (strict) — pass; the only errors are the 3 pre-existing,
  unrelated `tests/test_tokenizer.py` errors present on a clean `main`
  checkout (confirmed via `git stash` in the prior pass), untouched by
  this mission.
- `pytest` — 324 passed, 1 failed (the same pre-existing, unrelated
  `TestSettingsFragmentNotWiredIntoCommittedSettings` failure documented
  below), no flakiness observed in this run.
- `.harness/bin/verify` — `contract`/`lint`/`eval`/`build` pass;
  `typecheck`/`test` show only the pre-existing findings, not a new
  regression.

## Known, pre-existing, unrelated finding (carried over from the prior pass)

`tests/test_hooks.py::TestSettingsFragmentNotWiredIntoCommittedSettings::test_settings_hooks_json_is_valid_and_hooks_only`
fails on a clean checkout of `main` too (`demo/settings.hooks.json` has
both a `permissions` and a `hooks` top-level key; the test still asserts
`hooks`-only). Predates this mission (commit `dd3a690`). Left as-is per
Non-goals; flagging again for a human decision.

## Fourth revision: the four panes are the turn's boundary, not a focused round

Immediate same-day follow-up after the human saw the sidebar+panes layout:
the panes must always show the turn's *own* input/output — panel 1/2 the
first round's received/tokenized text (what the human actually typed, and
that same text tokenized), panel 3/4 the latest completed round's
response_received/detokenized text (the final answer, tokenized and then
resolved) — never whichever round a sidebar click happens to be focused
on. Confirmed the reading with the human before touching code (they asked
explicitly: "antes de ejecutar nada, dime si lo has entendido bien").

Implemented: `Panes` takes `firstCycle`/`lastCycle` instead of a single
focused `cycle`; `LiveTab` computes both once from `getRoundIds(cycles,
selectedRequestId)` (first and last entries), with no dependency on
sidebar interaction. Removed the `focusedRoundId` state, the `onFocusRound`
wiring, `TimelineRow`'s `isFocused`/`onFocus` props, and the "● Following
an earlier round — jump to latest" button and its CSS — none of them serve
a purpose once the panes stopped tracking sidebar focus. The sidebar
itself (row list, per-row inline expand/collapse, hook markers, live
spinners, the "waiting for next stage" row) is unchanged, per explicit
instruction to leave it as-is.

Re-verified live: a fourth real `claude -p --demo-db` session (a fresh
gateway process, real hooks) — Playwright confirmed one chip; panel 1/2
showed the literal typed prompt; panel 3/4 showed the final markdown
answer identically in both, tokenized and detokenized (no PII token
present in this particular answer's text, so the two happened to render
the same string, which is expected, not a bug); clicking a "Tool round 1:
Input received" sidebar row expanded it inline but left all four panes'
text byte-for-byte unchanged (`PANES_UNCHANGED=true`); no
`#jump-to-latest-round` element present; zero console errors.

## What a human reviewer should check

1. `PipelineEventBus.publish`'s revised diff — confirm the
   `(session_id, received_text)` key lookup and the two-deque eviction
   routing both still happen inside the single existing lock acquisition;
   re-litigating that race is exactly what mission-12 and this mission's
   own earlier `_open_turn_by_session` design both had to get right once
   already.
2. `_SAFETY_CLASSIFIER_MARKER`'s exact string (`"respond with <severity>"`)
   — best-effort, not a protocol guarantee (same caveat as
   `_TITLE_GENERATION_SYSTEM_MARKER`); if Claude Code changes this
   classifier's prompt wording, the marker will need updating the same way
   the title-generation one might.
3. `frontend/panel.html`'s `Panes`/`Timeline`/`#live-body` layout — confirm
   the sidebar+panes split reads the way you asked for, and that the panes
   now correctly stay fixed to the turn's first/last round regardless of
   sidebar clicks (see "Fourth revision" above).
4. The noted cosmetic gap (blank vs. "not reached yet" for an
   empty-but-arrived stage) — low priority, your call whether it's worth a
   follow-up.
5. Run `.harness/bin/verify` yourself, and ideally one more live
   `claude -p --demo-db` session of your own choosing, since this
   mission's own live-testing already found real gaps two rounds in a row
   that synthetic testing missed.

## Merge policy

No commit, push, or merge without explicit human review of the diff and
this document, per `.harness/mission.md`'s Merge policy. This mission does
not merge itself.
