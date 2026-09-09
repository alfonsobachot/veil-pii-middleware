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

None run this mission — declined implicitly, same reasoning as mission-11:
every new shape (the ring buffer, the per-cycle text extraction, the
persisted comparison cache file) is this codebase's own design, not a
third-party dialect to spike against. Proven by tests plus a manual
demo-run checklist in `.harness/handoff.md`.

## Executable Checks

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore`, no implicit `Any` |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Existing gateway/tokenizer/hooks/sanitize tests still pass unmodified in behavior | `.venv/Scripts/pytest tests/test_gateway.py tests/test_tokenizer.py tests/test_hooks.py tests/test_sanitize.py` | exit `0`, no test needed to change its own assertions about the *wire* bytes relayed (only pipeline-stream-text-producing internals changed) |
| Last-user-turn extraction, multi-field payload (AC1) | pytest: POST a body shaped like a real Claude Code request (non-trivial `system` string/blocks, a `tools` array, a multi-message `messages` history) containing PII only in the *last* user message; assert the `received` pipeline event's `text` equals exactly that message's text (not the whole JSON document) and its `spans` offsets slice correctly into that text, landing on the actual PII substring | exit `0` |
| Span offsets provably wrong under the old whole-document approach (AC1, regression guard) | pytest: same payload as above, but constructed so the last user message's PII span, if `start`/`end` were instead applied against the *whole raw JSON body* text, would land on different (wrong) characters — assert the new code does *not* reproduce that miscount | exit `0` |
| Non-text last-turn content block placeholder (AC2) | pytest: last user message's `content` is a list containing only a `tool_result`-shaped block (no `text`/`thinking` block); assert the `received` event's `text` is a short bracketed placeholder, not empty, and not a JSON dump of the block | exit `0` |
| Buffered JSON response extraction (AC3) | pytest: a non-streaming upstream response shaped like `{"content": [{"type": "text", "text": "..."}]}` containing a token; assert `response_received`/`detokenized`/`displayed` events carry only the reconstructed text, no envelope fields (`id`, `role`, `stop_reason`, etc.) | exit `0` |
| SSE response extraction (AC3) | pytest: an SSE upstream response with multiple `content_block_delta` events (mixing `text_delta` and `thinking_delta`) across one or more blocks; assert the pipeline events' text is exactly the reconstructed delta text in order, with no `event:`/`data:` line, no `message_start`/`content_block_start`/`content_block_stop` JSON, anywhere in the string | exit `0` |
| Ring buffer replays completed cycles to a late subscriber, oldest first (AC4) | pytest: run two or three full request/response cycles through the gateway *before* opening `GET /veil-panel/pipeline-stream`; assert the new subscriber's first events (before any new live request) are exactly those cycles' stages, in the order the cycles originally completed | exit `0` |
| Ring buffer is bounded at 20 and evicts oldest first | pytest: run 22 cycles, then connect; assert the backlog contains only the last 20 request ids, and the earliest 2 are absent | exit `0` |
| Ring buffer never double-delivers or drops across the subscribe/publish race (AC4) | pytest: a thread publishing a burst of events concurrently with a thread calling `subscribe()` in a loop, repeated enough iterations to be a meaningful stress check; assert every subscriber's (backlog + live) event count for a given, fully-observed cycle equals exactly the number of stages published for it — never more, never fewer | exit `0` |
| Pipeline stream still never reaches the audit log (regression guard, mission-11's existing proof) | pytest: after running the ring-buffer tests above, assert `.veil/audit.jsonl` contains no `"stage"`/`"pipeline"` text | exit `0` |
| Metrics comparison cache miss (AC5) | pytest: fresh `tmp_path`-scoped `.veil/metrics_comparison.json` (does not exist yet); `GET /veil-panel/metrics/comparison` returns `{"available": false}` | exit `0` |
| Metrics comparison persists across a simulated restart (AC5) | pytest: `POST /veil-panel/metrics/comparison` (with `evaluate` monkeypatched to avoid a real LLM call) against one `make_handler`-built server; then build a *second*, independent `make_handler` instance pointed at the same cache path and `GET /veil-panel/metrics/comparison` from it; assert it returns the same persisted result | exit `0` |
| Metrics comparison partial failure (AC6) | pytest: monkeypatch `evaluate` so `mode="regex"` succeeds and `mode="llm"`/`"union"` raise; `POST /veil-panel/metrics/comparison` still returns 200 with the regex mode's real numbers and the other two modes marked unavailable with a reason, not a 500 | exit `0` |
| Comparison cache holds no raw value (Non-goals / Scrutiny) | pytest: assert the persisted `.veil/metrics_comparison.json` contains no `"value"` key and no manifest `Miss` detail — aggregate `by_type`/`by_detector` counts only | exit `0` |
| `llm_client_from_env` rename, no behavior change | pytest: `demo/eval.py main()`'s `--mode llm` path with the env vars unset still raises the same `SystemExit` message it did before the rename | exit `0` |
| Existing recall/decoy gates unaffected | `.harness/bin/verify`'s `eval` row (`--min-recall 1.0 --max-false-positives 0`, regex-only mode) | exit `0`, identical pass/fail semantics to before this mission |
| No new runtime dependency | `git diff -- pyproject.toml` reviewed by hand | no new `dependencies` entry |
| `core/detector.py`/`core/tokenizer.py`/`core/vault.py` untouched beyond the one documented `_tokenize_body` lifecycle change | `git diff -- veil_pii_middleware/core/` reviewed by hand | no change to `core/detector.py` or `core/vault.py` at all; `core/tokenizer.py` diff is empty (only the *caller*, `_tokenize_body` in `server.py`, changes how it invokes `tokenize()`) |

## Scrutiny Validator

- Confirm the actual bytes written to the real client via `_write_chunk` in
  `_relay_buffered_response`/`_relay_sse_response` are provably unchanged —
  diff those two functions and confirm every `_write_chunk`/`wfile.write`
  call site still writes from the same `body`/`out_bytes`/`out_event`
  variables as before; only the functions' *return values* (fed to
  `_publish_stage`, never to the client) change.
- Confirm the new per-cycle `detect()` call in `_tokenize_body` reuses the
  exact same `LLMClient` instance passed to `tokenize()` (not a second,
  freshly-constructed client with the same env config) — the cache-hit
  argument in `mission.md`'s Tooling Assessment depends on identity, and a
  silent divergence here would double the number of real LLM calls per
  request without any test catching it (both calls still "succeed").
- Confirm `PipelineEventBus.publish`'s ring-buffer bookkeeping and its
  subscriber-list snapshot happen inside the *same* lock acquisition as
  `subscribe()`'s backlog snapshot + subscriber registration — re-read both
  methods side by side against the four-ordering argument in `mission.md`'s
  Intake Grill evidence section before approving; a version that takes the
  lock twice (once for history, once for subscribers) reopens the race.
- Confirm `/veil-panel/metrics/comparison`'s `POST` handler closes any
  `LLMClient` it constructs (mirroring `demo/eval.py main()`'s existing
  `finally: llm_client.close()`), even when a mode's `evaluate()` call
  raises.
- Confirm the frontend's cycle selector and the server's ring buffer agree
  on "20" as the same bound in both places, not two independently-chosen
  numbers that could silently drift apart.
- Confirm no gateway stderr log line, `.veil/metrics_comparison.json`
  entry, or new endpoint response anywhere in this mission's diff contains
  a raw PII value — the pipeline-stream side channel remains the *only*
  place in this codebase that legitimately carries one in memory (per
  mission-11's existing, human-flagged design exception), and this mission
  must not quietly add a second one.

## User-Testing Validator

- No live spike was run for the ring buffer, the extraction helpers, or the
  comparison cache — pytest-level tests are the proof of correctness; a
  human must still confirm the actual panel *reads* right (spacing, whether
  a placeholder like `[tool_use]` looks sane in context, whether the
  step-through animation timing feels legible rather than jarring).
- Required manual follow-up, written into `.harness/handoff.md` as a
  step-by-step checklist: start the gateway, run 2-3 real requests through
  it (varying content: plain text, one with a tool call, one triggering
  streaming), *then* open the panel and confirm the selector already shows
  those cycles; select an older cycle, run a new request, and confirm the
  selection does not jump away; step a completed cycle's pipeline stages
  manually and via "Play"; open the Metrics tab before ever pressing "Run
  comparison" and confirm it shows "no comparison has been run yet" (or the
  actually-persisted last run) rather than an error; press "Run comparison"
  (requires LM Studio reachable for the LLM/unified numbers — otherwise
  confirm the regex numbers still show and the other two report
  unavailable, not a crash); restart the gateway process and confirm the
  Metrics tab still shows the last comparison without re-running it.
- If no LM Studio instance is reachable this mission, say so plainly in
  `handoff.md` and confirm the partial-failure path (regex succeeds, LLM/
  union report unavailable) was still exercised, either live or via the
  monkeypatched pytest coverage above.
