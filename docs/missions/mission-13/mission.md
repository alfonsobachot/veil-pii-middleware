# Mission

## Objective

Redesign the observability panel's grouping so that one *logical* user
request (a prompt, plus however many tool round-trips it takes to answer
it) renders as one screen, instead of one chip per raw gateway HTTP call.
Reported live by the human: a demo-db query produces several chips today
(the real turn, plus one more per tool_result continuation the gateway
never distinguishes from an ordinary conversational turn), most of which
carry no useful standalone content.

Two parts:

1. **Grouping (bug fix against the panel's own intent, not new scope).**
   `_classify_cycle_kind` mislabels a pure tool_result continuation as
   `"conversation"` because `_extract_last_user_message_text` renders it as
   the non-empty placeholder `"[tool_result]"`, so today's existing
   `internal_continuation`/`title_generation` hiding never catches it. Fix:
   a new `"tool_continuation"` kind, folded into the parent turn's screen
   as an additional timeline step rather than hidden outright or given its
   own chip.
2. **Live hook visibility (new scope, explicitly requested).** The
   PreToolUse/PostToolUse hooks that detokenize a demo-db command before it
   runs and re-tokenize its result never report anything to the panel today
   — confirmed by grep, zero references to pipeline/panel/cycle anywhere
   under `adapters/hooks/`. The human wants exactly this activity visible,
   live, inside the same screen as the turn that triggered it, specifically
   because the gap between "hook fires" and "hook finishes" is where the
   actual (sometimes slow) DB round-trip happens and today that time is
   invisible.

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

## Intake Grill

| Question | Answer |
|---|---|
| Hooks and the gateway share only Claude Code's `session_id`, never a request/turn id. What correlation criterion should attach a hook's event to the right screen? | Most-recently-opened turn per `session_id`: the gateway tracks, per session, the `request_id` of the latest cycle classified `"conversation"`; every hook event and every `"tool_continuation"` cycle for that session attaches to it until the next real `"conversation"` cycle supersedes it. Real usage is sequential (one thing in flight per session at a time), so this is safe in practice. |
| What happens to a hook event that fires with no open turn for its session (panel never opened this process's lifetime, or a demo-db command run by hand outside a real conversation)? | Dropped silently — never published to the pipeline-stream bus. This is ephemeral, best-effort observability, not the audit trail; `.veil/audit.jsonl` already records every token this hook mints regardless, so nothing durable is lost. |
| The new HTTP call *from* a hook *to* the gateway must never compromise `PreToolUse`'s fail-closed guarantee. What's the contract? | Strictly best-effort: a short, fixed timeout (200ms), wrapped in a blanket `try/except Exception: pass`. It can never change a hook's exit code, block/allow decision, stdout payload, or wall-clock behavior beyond that bounded timeout — identical to today whenever the gateway is unreachable or not running at all (the common case for anyone using hooks without the gateway). |
| How should live progress render inside one screen while a DB round-trip is slow? | A vertical timeline of steps, each moving pending → in-progress → done live as events arrive (spinner on the active step), not a single aggregate progress bar. Concretely: prompt received/tokenized → sent to model → model requests a tool → PreToolUse start/done → *(the gap here is the real DB execution time, now visible for the first time)* → PostToolUse start/done → result sent back to model → final response. |
| Once tool_continuation cycles stop being their own chips, is their raw tokenized/detokenized text still inspectable for debugging? | Yes — each step in the timeline that corresponds to a real gateway cycle (not a hook marker) stays expandable inline, reusing the existing per-stage pane rendering; nothing that was visible today becomes invisible, it just moves from "its own chip" to "an expandable row inside the parent turn's timeline". |
| This touches `adapters/hooks/` (a fail-closed surface) in addition to `adapters/gateway/server.py` and `frontend/panel.html`. Confirm the execution lane. | `agent-with-checkpoint`, human review before merge — same lane as mission-11/mission-12, the precedent for this exact panel. |

Evidence gathered before asking (no re-reading needed once the grill
started, all from this session's own exploration before the grill):
`adapters/gateway/server.py` — `_classify_cycle_kind`, `_extract_last_user_message_text`,
`_extract_text_from_content_blocks` (the `_TEXT_CONTENT_BLOCK_FIELDS`
placeholder mechanism is exactly why `"[tool_result]"` reads as non-empty
today), `_tokenize_body`'s call site, `PipelineEventBus.publish`/`subscribe`'s
existing lock discipline, `_DEFAULT_PORT = 8787`; `adapters/hooks/pre_tool_use.py`
and `post_tool_use.py` in full — both read `session_id` straight from the
hook payload already (the same real Claude Code session id the gateway's
`X-Claude-Code-Session-Id` reconciliation already keys the vault by, per
BRIEF §6's mission-8-follow-up fix), both already fail closed / degrade
independently of any new call this mission adds; `adapters/hooks/_scope.py`'s
`is_demo_db_command` gate (both hooks already no-op immediately for any
non-demo-db tool call, so the new reporting call only ever fires on the
in-scope path); `scripts/run-gateway.sh`'s `127.0.0.1`-only bind and its
`VEIL_VAULT_KEY`/`VEIL_LLM_BASE_URL`-style env var convention (the new
`VEIL_GATEWAY_URL` env var for hooks follows the same pattern); `pyproject.toml`
confirming `httpx` is already an unconditional runtime dependency, so hooks
gain no new dependency by using it; `frontend/panel.html`'s existing
`HIDDEN_KINDS`/`cyclePreview`/`CycleSelector` (the toggle-and-hide pattern
this mission extends rather than replaces).

## Scope

- `veil_pii_middleware/adapters/gateway/server.py`:
  - Refactor `_extract_last_user_message_text` to share a new
    `_find_last_user_message_content(parsed) -> JSONValue | None` helper
    with a new `_last_user_content_has_text(content) -> bool` (true iff at
    least one content block's type is a key of `_TEXT_CONTENT_BLOCK_FIELDS`
    with a real string value — i.e. the message contains actual
    human/model-authored text or thinking, not only placeholder-rendered
    blocks like `tool_use`/`tool_result`/`image`).
  - `_classify_cycle_kind` gains a `has_real_text: bool` parameter and a new
    branch: `title_generation` (unchanged) → empty stripped text →
    `internal_continuation` (unchanged) → `not has_real_text` →
    **`"tool_continuation"`** (new) → else `"conversation"` (unchanged).
  - New in-memory state on the handler/bus: `_open_turn_by_session:
    dict[str, str]` (session_id → request_id), guarded by the existing
    `PipelineEventBus` lock. Updated only when a `"received"` stage event's
    `kind == "conversation"` (opens/replaces the turn for that session).
    When a `"received"` event's `kind == "tool_continuation"`, the event
    gains a `parent_request_id` field looked up from this map at publish
    time (omitted/`None` if no turn is open for that session — the
    frontend's documented fallback, see Non-goals).
  - New endpoint `POST /veil-panel/hook-event`. Request body:
    `{"session_id": str, "hook": "pre_tool_use" | "post_tool_use", "phase":
    "start" | "done" | "error", "tool_name": str}` — **no command text, no
    tool output, no tokenized or detokenized value of any kind accepted or
    stored; the schema has no field capable of carrying one.** Looks up
    `_open_turn_by_session[session_id]`; if present, publishes a
    `kind="hook"` event tagged with that `request_id` (same `PipelineEventBus.publish`
    used for ordinary stages, distinguished by `kind`); if absent, does
    nothing. Always responds `204` — the hook does not read the response
    body, and a malformed/unrecognized payload is also swallowed into a
    no-op `204` (best-effort on both ends, not a new source of hook
    breakage). No new auth beyond the existing `127.0.0.1`-only bind.
- `veil_pii_middleware/adapters/hooks/_panel_report.py` (new, shared by
  both hook scripts, mirroring `_paths.py`/`_scope.py`'s existing
  one-small-thing-in-one-place convention): `report(session_id: str, hook:
  str, phase: str, tool_name: str) -> None`. Reads `VEIL_GATEWAY_URL` env
  var (default `http://127.0.0.1:8787`, matching `run-gateway.sh`'s own
  default port) once per call; POSTs the fixed-shape JSON body above via
  `httpx` with a `timeout=0.2` (connect+read combined); the entire call is
  wrapped in a blanket `except Exception: pass` (unreachable host,
  timeout, DNS failure, non-2xx response — all swallowed identically).
  Never raises, never returns a value the caller could branch on
  incorrectly as a success/failure signal — callers do not check anything
  after calling it.
  - `pre_tool_use.py`: calls `report(session_id, "pre_tool_use", "start",
    "Bash")` immediately after the existing `is_demo_db_command`/`session_id`
    checks pass (before the `Vault`/`detokenize()` call), and
    `report(..., "done", "Bash")` right before its existing successful
    `return 0`, or `report(..., "error", "Bash")` on its existing fail-closed
    `return 2` paths that occur *after* the start report was sent. No
    change to any existing control flow, return value, or stderr message —
    purely additive calls at existing branch points.
  - `post_tool_use.py`: same pattern around its `tokenize()` call — `"start"`
    before, `"done"` before its successful `return 0`, `"error"` on its
    existing `return 1` paths after the start report was sent.
- `veil_pii_middleware/frontend/panel.html`:
  - Selector groups by **turn**, not raw cycle: a cycle is a turn's own
    chip iff `kind === "conversation"`, or iff `kind === "tool_continuation"`
    with no resolvable `parent_request_id` (orphan fallback — see
    Non-goals for why this must stay visible, not hidden). Every other
    `tool_continuation` cycle (one with a `parent_request_id` pointing at a
    turn present in the buffered cycles) is a **child** of that turn, not a
    chip of its own. `title_generation`/`internal_continuation` are
    unaffected — still fully hidden by default behind the existing "Show
    hidden" toggle.
  - Selecting a turn renders a single chronological **timeline**: every
    stage event from the turn's own cycle, every child cycle's stage
    events, and every `kind: "hook"` event tagged with that turn, merged
    and sorted by timestamp (all three already carry an ISO timestamp
    today). A stage-type entry renders a short label plus its existing
    expandable pane content (reused, not reimplemented); a hook-type entry
    renders `<hook> <phase>` (e.g. "PreToolUse: detokenizing…" /
    "PreToolUse: done") with no expandable content (there is nothing in
    its payload to expand — see the hook-event schema above).
  - Live state: a hook `"start"` event with no matching `"done"`/`"error"`
    yet for the same `(hook, tool_name)` pair renders as in-progress
    (spinner); a cycle that is not yet `cycleIsComplete` renders a trailing
    "waiting for the next stage…" row. This is what makes the actual DB
    round-trip's duration visible live, per the human's own stated
    motivation.
  - `HIDDEN_KIND_LABEL`/`HIDDEN_KINDS` stay exactly as today; nothing about
    `title_generation`/`internal_continuation` classification, hiding, or
    the existing toggle changes in this mission.
- Tests: `tests/test_gateway.py` gains coverage for the reclassification
  (a pure-tool_result last user message → `"tool_continuation"`, not
  `"conversation"`), the `parent_request_id` attachment (with and without
  an open turn), and the new `/veil-panel/hook-event` endpoint (known open
  turn → event published and tagged; no open turn → `204` with nothing
  published; malformed body → `204`, never a 500). A new
  `tests/test_hooks.py`-adjacent test (or extending the existing hook test
  module, whichever already exists) covers `_panel_report.report`'s
  best-effort contract: pointed at a closed/unreachable port, the call
  returns (does not raise) within a bounded short time, and the calling
  hook's exit code/stdout are byte-for-byte identical to a run with
  `VEIL_GATEWAY_URL` pointed at nothing reachable versus a run where
  reporting is skipped entirely.
- `.harness/handoff.md`: a manual demo-run checklist — run a real demo-db
  query end to end through both the gateway and the hooks together, and
  confirm exactly one chip appears for the whole turn, its timeline shows
  the PreToolUse/PostToolUse markers in the right place relative to the
  request/response stages, and the gap between PreToolUse "done" and
  PostToolUse "start" visibly corresponds to the real query's wall-clock
  time.

## Non-goals

- No change to `core/detector.py`, `core/vault.py`, or
  `core/tokenizer.py`'s substitution logic — this is entirely inside the
  gateway's observability side-channel, the two hook scripts' own
  best-effort reporting, and the frontend.
- **The new `/veil-panel/hook-event` payload never carries a command
  string, a tool response, or any tokenized/detokenized value — only
  `hook`, `phase`, and a static `tool_name`.** This is deliberate, not an
  oversight: `pre_tool_use.py`'s whole reason to exist is producing a
  command containing the *real, detokenized* PII value the DB needs to
  see, so any observability path fed by that hook must be designed from
  the start to have no field capable of carrying it — not "carefully
  redacted before sending", which would need its own review every time the
  hook's internals change. The tokenized/detokenized text these hooks
  handle remains visible only where it already is today: inside the
  gateway's own `tool_continuation` cycle panes (which only ever see the
  *tokenized* wire value, never the hook's locally-detokenized one).
- A `tool_continuation` cycle with no open turn (the panel never saw a
  preceding `"conversation"` cycle for that session, e.g. process restart
  mid-turn) is **not** dropped or hidden the way an orphan hook event is —
  it falls back to being its own visible chip. Silently discarding a
  hook's ephemeral marker loses nothing (the audit log already has the
  durable record); silently discarding an entire cycle's request/response
  content, which exists nowhere else in the panel, would.
- `PipelineEventBus`'s existing 20-cycle ring buffer bound stays a bound on
  raw `request_id`s, not turns — unchanged from mission-12. A turn with
  several tool round-trips now consumes several of those 20 slots, so a
  very tool-call-heavy session may evict older *turns* from the buffer
  faster than before. Accepted for this mission; revisit the constant
  later if it proves too aggressive in real use.
- No authentication on `/veil-panel/hook-event` beyond the existing
  `127.0.0.1`-only bind, matching every other panel route.
- No animation/motion polish beyond the specific live pending → in-progress
  → done state transitions described above.
- No change to what bytes the gateway forwards upstream or relays back to
  the real client, and no change to what the two hooks actually do to
  `tool_input`/`tool_response` — every new call in this mission is
  additive observability plumbing around existing, unmodified logic.

## Acceptance criteria

1. A gateway request whose last user message's content is entirely
   non-text blocks (e.g. a bare `tool_result`) is classified
   `cycle_kind == "tool_continuation"`, not `"conversation"` — proven by
   test; a request with the same shape but a real text/thinking block
   present anywhere in that message stays `"conversation"`.
2. A `"tool_continuation"` cycle published while a `"conversation"` cycle
   for the same `session_id` is open carries `parent_request_id` equal to
   that conversation cycle's `request_id` — proven by test. A second,
   later `"conversation"` cycle for the same session supersedes the first
   as the open turn for any subsequent `tool_continuation` — proven by
   test.
3. A `"tool_continuation"` cycle published with no open turn for its
   session carries no `parent_request_id` — proven by test.
4. `POST /veil-panel/hook-event` with a known open turn publishes exactly
   one `kind: "hook"` event tagged with that turn's `request_id`, carrying
   only `hook`/`phase`/`tool_name` — proven by test. With no open turn for
   the given `session_id`, or a malformed body, it responds `204` and
   publishes nothing — proven by test, in neither case a 500.
5. `_panel_report.report`, pointed at an unreachable/closed port, returns
   without raising, within a small bounded wall-clock budget (test asserts
   an upper time bound, proving the timeout is actually short and not
   accidentally unbounded) — proven by test. The calling hook's exit code
   and stdout are unaffected by whether the gateway is reachable — proven
   by test running `pre_tool_use.main()`/`post_tool_use.main()` both ways.
6. No test payload, log line, or new endpoint request/response anywhere in
   this mission's diff contains a detokenized command string or a raw
   `tool_response` value — proven by code review plus a test asserting the
   `/veil-panel/hook-event` schema rejects/ignores any extra field beyond
   `session_id`/`hook`/`phase`/`tool_name` rather than passing it through.
7. Frontend (manual checklist in `handoff.md`): a demo-db query end to end
   produces exactly one selector chip for the whole turn; expanding it
   shows request → PreToolUse start/done → PostToolUse start/done →
   tool-result continuation → final response, in chronological order, with
   the request/response steps still expandable to their existing
   tokenized/detokenized text; `title_generation`/pure
   `internal_continuation` cycles remain hidden by default exactly as
   before this mission.
8. `.harness/bin/verify` exits 0.
9. `.harness/handoff.md` documents the manual demo-run checklist above and
   anything left for human follow-up.

## Risk

Risk class: medium

Rationale: this mission edits `adapters/gateway/server.py` (the live proxy,
observability side-channel only — same class of change mission-11/mission-12
already made, not the request/response relay bytes or any
detection/vault/tokenization logic) *and*, newly, `adapters/hooks/`, which
BRIEF does not name among the explicitly "high-risk" surface
(detector/vault/tokenizer) but which sits on `PreToolUse`'s fail-closed
path — the one place in this codebase where an unrelated bug could turn
into a hook that blocks (or, worse, silently misbehaves on) every demo-db
command instead of just failing to show a spinner. Mitigated by: the
reporting call being additive at existing branch points (no existing
branch, return value, or exit code is restructured to make room for it),
a hard, short timeout with a blanket exception swallow, and a dedicated
test proving the hook's behavior is identical whether or not the gateway
is reachable. The one genuine new security-relevant design point (the
hook-event schema must structurally exclude command/output text, not just
avoid sending it by convention) is called out explicitly in Non-goals and
covered by acceptance criterion 6.

## Merge policy

No commit, push, or merge without explicit human review of the diff and
`.harness/handoff.md`. This mission does not merge itself.

## Tooling And Approach Assessment

1. **What project type is this?** A reclassification bug fix
   (`tool_continuation` kind) plus one new additive feature (hooks
   reporting best-effort progress to a new gateway endpoint) plus a
   frontend regrouping/timeline view — all inside the existing gateway
   adapter, hooks adapters, and the unbuilt-toolchain React frontend
   mission-11/12 already established. No new architectural shapes, no new
   process boundary beyond one new local HTTP call hooks already have the
   dependency (`httpx`) to make.
2. **What methods, tooling, or approaches does that imply?** Stdlib plus
   the already-present `httpx` dependency for the hook→gateway call
   (`timeout=0.2`, no retries — a retry would only spend more of the
   hook's own wall-clock time on a best-effort signal). No new Python
   runtime dependency. Frontend stays CDN React + Babel Standalone, no
   bundler, reusing existing per-stage pane render functions rather than
   duplicating them for the timeline view.
3. **What risks or missing decisions does it reveal?** (a) The
   best-effort contract must be provably non-blocking, not just documented
   as such — validation needs a test against a genuinely closed port, not
   a mock that always succeeds instantly. (b) The hook-event schema must
   be incapable of carrying PII by construction, addressed by making the
   schema's only string fields fixed short labels, never freeform command/
   output text — see Non-goals and acceptance criterion 6. (c) The
   turn-correlation heuristic (latest open `"conversation"` cycle per
   session) depends on single-threaded-per-session real usage; a
   pathological interleaving (two genuinely concurrent conversations
   somehow sharing one `session_id`) would misattribute a hook event to
   the wrong turn — accepted as out of scope, since Claude Code does not
   produce that shape in real use and BRIEF's own threat model already
   scopes this project to a single developer's local session.
4. **What should be added to validation?** All standard gates; the
   reclassification tests; the `parent_request_id` attachment tests (with
   an open turn, without one, and after a second conversation cycle
   supersedes the first); the new endpoint's three response shapes (known
   turn, no turn, malformed body); the best-effort-timeout test against an
   actually unreachable port; a schema test proving extra fields are
   ignored, not stored or forwarded; the manual demo-run checklist for
   what only a browser can show (the live spinner states, the chronological
   ordering "reading right").
5. **What is explicitly not needed now?** Any core detection/vault/
   tokenizer change; authentication on the new endpoint; retries or
   queuing for the best-effort hook report; raising the 20-cycle ring
   buffer bound; any change to `title_generation`/`internal_continuation`
   classification or hiding.

## Status

implemented — awaiting human review for merge (see `.harness/handoff.md`).

Reopened 2026-09-09: the human live-tested the first cut (real gateway +
real `PreToolUse`/`PostToolUse` hooks + a real `claude -p --demo-db`
session, not the synthetic curl/browser smoke test the original
implementation pass used) and reported the conversation was still
splitting across several chips, screenshot attached. Proceeded under the
existing approval as a regression-fix-and-revision against this mission's
own already-approved scope, not a fresh Intake Grill round — same reasoning
mission-11's own reopening used.

Root causes, all found by capturing real pipeline-stream traffic from that
live session (raw data preserved in `.harness/handoff.md`):

1. Claude Code's own internal harm/safety-classifier request (fires
   roughly once per tool call: `<transcript>{...}</transcript>` as the
   last user message, `Respond with <severity>N</severity>` as an
   instruction *outside* that tag) read as an ordinary `"conversation"`
   cycle and repeatedly stole the turn-tracking pointer mid-turn,
   misattributing hook events to the wrong screen. Fixed: a new
   `"safety_check"` kind (`_SAFETY_CLASSIFIER_MARKER`), hidden by default
   like `title_generation`.
2. The same real human turn legitimately produced more than one
   independent, complete `"conversation"` round-trip with byte-identical
   received text (observed live). The original "most recent conversation
   cycle wins" pointer split those across two chips instead of folding
   them. Fixed: turns are now keyed by `(session_id, received_text)` --
   a same-text cycle folds under the first one seen, not the most recent.
3. A burst of auxiliary-kind cycles (mostly the safety-check calls in
   finding 1, before finding 1's own fix) was enough to evict the *real*
   conversation cycle from `PipelineEventBus`'s single shared 20-slot ring
   buffer before a freshly-opened panel ever saw it -- reproduced live with
   zero chips shown for a completed, successful query. Fixed: hidden-kind
   cycles now evict from their own separate ring buffer
   (`_aux_order`/`_AUXILIARY_CYCLE_KINDS`), on both the gateway and the
   frontend, so they can never crowd out a real turn's budget.

New, scope-expanding ask from the same reopening: keep the four classic
panes (Input/Outbound/API response/Developer view) as the panel's main
content, and move the mission-13 Timeline into a left sidebar instead of
replacing the panes outright. Implemented; see `.harness/handoff.md` for
the live-tested screenshots and evidence.

Reopened again 2026-09-09 (same day, immediate follow-up): the human
clarified the four panes' intended meaning after seeing the first sidebar
cut, which wired a sidebar row click to *change* what the panes show
(click-to-focus-a-round). That contradicts what the human actually wants:
the four panes are a fixed view of the *turn's own boundary* -- panel 1/2
always the turn's first round (the human's real input, and that input
tokenized), panel 3/4 always the turn's latest completed round (the API's
raw answer, and that answer resolved) -- never whatever round happens to
be expanded in the sidebar. Confirmed explicitly before implementing (per
CLAUDE.md's Intake Grill spirit, since this reverses a design decision
from earlier the same day) then implemented: removed the sidebar-click-
focuses-panes wiring (`focusedRoundId`/`onFocusRound`/the "jump to latest
round" button) entirely; `Panes` now takes `firstCycle`/`lastCycle`
computed once from the selected turn's own round list, never from
sidebar interaction. The sidebar itself (row list, inline expand,
hook markers, live spinners) is otherwise untouched, per explicit
instruction to leave it as-is. Re-verified live against a fourth real
`claude -p --demo-db` session: one chip, panes show input/final-answer,
expanding a "Tool round" sidebar row leaves the panes byte-for-byte
unchanged, no "jump to latest" button rendered, zero console errors.

Committed 2026-09-09 (commit `d8f51be`, no `Co-Authored-By` trailer per the
user's own standing instruction) after human review; `README.md` §7 and
its architecture diagram updated in the same commit to describe the
turn-grouping, the sidebar's live hook progress, and the fixed four-pane
semantics. Archived to `docs/missions/mission-13/` per the human's
explicit request.
