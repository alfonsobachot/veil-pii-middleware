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

## Live Spike (before any buffer code is written)

Not automatable — it exercises the user's real Claude Code account and a
real external endpoint. Run and record evidence before implementation
begins; a failure or a surprising shape changes the buffer design, so this
gates Stage 6, the same way mission-7's auth spike gated its gateway code.

| Spike | Method | Required result to proceed |
|---|---|---|
| SSE event shape | Start a throwaway local pass-through proxy (reuse mission-7's pattern) that this time logs the **decoded body content** of the response, not just headers — every `data:` line / SSE event. Spawn a **separate** process — `ANTHROPIC_BASE_URL=http://127.0.0.1:<port> claude -p "<probe prompt long enough to stream multiple deltas>"` — and capture the proxy's log of the real event stream. | The log shows, verbatim, which event types carry incremental text (expected: `content_block_delta` with a `text_delta`/`input_json_delta`/`thinking_delta` sub-shape — confirm the real field names, don't assume) and confirms each `data:` line is one complete JSON event (no event spans two `data:` lines). Record this evidence in `handoff.md`. If a token isn't naturally echoed by the probe prompt, a second run may ask the model to literally repeat a `PII_EMAIL_0001`-shaped string back, split by the proxy's own logging into two writes, to observe how the real deltas chunk arbitrary text — informational, not a pass/fail gate itself. |

## Executable Checks

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore`, no implicit `Any` |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Detector recall/decoys (unchanged) | `python -m veil_pii_middleware.demo.eval --min-recall 1.0 --max-false-positives 0` | exit `0`, unmodified — this mission does not touch `detector.py` |
| Existing request-side tokenizer/vault tests unaffected | `.venv/Scripts/pytest tests/test_tokenizer.py tests/test_vault.py` | exit `0`, no test in either file needed to change because of this mission's new function |
| Incremental detokenize: whole token in one call | pytest: feed the new `core/tokenizer.py` function a chunk containing a complete `PII_EMAIL_0001` token in one call; assert it returns the resolved value immediately with no retained tail | exit `0` |
| Incremental detokenize: split at every offset | pytest: for a known token span (e.g. `PII_EMAIL_0001`, 14 chars), split it at every offset 1..13 across two calls; assert every split yields the same final resolved output as the unsplit case, with no resolved text emitted early that later turns out wrong | exit `0` |
| Incremental detokenize: split across 3+ calls | pytest: split one token across three or more feed calls (e.g. one character at a time) to rule out an off-by-one that only a two-call split would hide | exit `0` |
| Incremental detokenize: ordinary long uppercase text is not buffered indefinitely | pytest: feed a long run of plain uppercase text that is not, and never becomes, a token (e.g. `"THIS IS ALL CAPS " * 50`); assert it is flushed (bounded retained tail, not accumulating without limit) | exit `0` |
| Incremental detokenize: unresolved token gets the agreed placeholder | pytest: feed a syntactically valid `PII_<TYPE>_NNNN`-shaped token that the vault does not know (never minted, or minted under a different session); assert the function substitutes the agreed visible placeholder text and does not raise, and does not emit the raw token text | exit `0` |
| Incremental detokenize: end-of-stream flush | pytest: end a call sequence with a pending retained tail that never completes a token (e.g. buffer ends in `"contact PII_EM"` with no more input coming); assert an explicit end-of-stream flush call returns that tail as plain text, unresolved-placeholder-free, since it was never actually a token | exit `0` |
| Gateway: split-token streaming response resolves end to end | pytest: fake SSE upstream (`http.server` or an `httpx`-mockable handler) emits `content_block_delta` events with a token's text deliberately split across two chunks; send a request through the real gateway; assert the client-received, reassembled response body contains the real value, not the token, and the chunked framing is well-formed (parses back into valid SSE) | exit `0` |
| Gateway: unresolved token in a streaming response | pytest: fake upstream echoes a token the gateway's vault never minted; assert the client receives the visible placeholder inline, the stream is not truncated, and the chunked framing is not corrupted (no double status line, valid final `0\r\n\r\n`) | exit `0` |
| Gateway: non-streaming (buffered) response still gets detokenized | pytest: fake upstream returns a `Content-Length` (non-chunked) JSON response containing a token; assert the client receives the resolved value | exit `0` |
| Gateway: response with no tokens is byte-identical modulo re-chunking | pytest: fake upstream returns ordinary text with no `PII_` substrings; assert the client-visible content is unchanged from mission-7's existing untouched-relay behavior | exit `0` |
| Request-side behavior (mission-7) unchanged | `.venv/Scripts/pytest tests/test_gateway.py -k "not response"` or equivalent — every pre-existing mission-7 gateway test (request tokenization, auth header forwarding, fail-closed on malformed body) still passes unmodified | exit `0` |
| `adapters/hooks/`, `adapters/command/`, `core/detector.py`, `core/vault.py`, request-side of `core/tokenizer.py`/`server.py` untouched beyond the new function/wiring | `git diff --stat` reviewed by hand; no unrelated changes | exit `0` |

## Scrutiny Validator

- Confirm the diff touches only `core/tokenizer.py` (new function, additive),
  `veil_pii_middleware/adapters/gateway/server.py` (response-path wiring),
  `tests/`, `.harness/*`, and `README.md`/`docs/private/BRIEF.md` if
  updated — `core/detector.py`, `core/vault.py`, `adapters/hooks/`,
  `adapters/command/`, and the *existing* `tokenize()`/request-side logic in
  `server.py` stay untouched.
- Confirm the new incremental function lives in `core/tokenizer.py`, not
  inline in `server.py` — per the mission's architecture decision, this is
  detection/substitution logic and CLAUDE.md reserves that for `core/`.
- Confirm `server.py` calls the new function rather than re-implementing any
  part of the buffering/retention logic itself.
- Confirm the buffer is keyed per `content_block_index` (or equivalent),
  never shared globally across a whole response, matching the Tooling
  Assessment's point that a token cannot span two content blocks.
- Confirm a resolve failure never raises out of the gateway's response loop
  in a way that would trigger a second `send_response` call — mission-7's
  own comment in `_handle` explains why that corrupts chunked framing; the
  new code must route an unresolved token through the agreed placeholder
  substitution instead of letting `Vault.resolve()`'s exception propagate
  into the streaming loop uncaught.
- Confirm no secret (master key, header values, any real PII value from the
  live spike) was written into `.harness/handoff.md`, `.harness/memory/`, or
  any committed file — the spike's captured evidence should show event
  *shape*, not real conversation content, wherever possible.
- Confirm mission-7's existing tests in `tests/test_gateway.py` were not
  edited to make them pass under the new code — they should keep passing
  unmodified, since the request leg is out of scope for this mission.

## User-Testing Validator

- **Live SSE-shape spike (must be run and recorded, not skipped):** see the
  Live Spike table above. `handoff.md` must state plainly, in one place,
  which delta event types were actually observed and whether/how a split was
  reproduced — this is the fact a human needs to judge whether the buffer's
  design actually matches reality rather than the BRIEF's original
  assumption.
- Manually point a real, interactive `claude` session at the finished
  gateway (`ANTHROPIC_BASE_URL` set for that one invocation only, never the
  user's persistent shell profile), send a prompt containing a real manifest
  PII value, and ask the model to repeat it back; confirm by eye that the
  real value — not a literal `PII_<TYPE>_<NNNN>` token — appears on screen
  in the reply. Record whether this step was actually performed, and what
  was observed, not a claim that it "should work."
