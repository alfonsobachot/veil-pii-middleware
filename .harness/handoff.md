# Handoff

Mission 8: response-side (model -> client) rewrite in the gateway, with a
chunk-boundary-safe token buffer (BRIEF work-plan day-2 item 10b).

## Summary

Mission-7 tokenized only the outbound request leg and relayed the response
byte-for-byte untouched, as an explicit scope cut. This mission closes that
gap: a `PII_<TYPE>_<NNNN>` token the model echoes back now resolves to its
real value before it reaches the client, in both response shapes the
gateway can see, without ever re-implementing detection/substitution logic
in the adapter layer.

- `veil_pii_middleware/core/tokenizer.py` (additive; `tokenize()`/
  `detokenize()`/`_TOKEN_PATTERN` unchanged, confirmed byte-for-byte by
  diff): a new `IncrementalDetokenizer` class buffers only a possible
  in-progress token's prefix and resolves everything else immediately, so a
  caller feeding it text chunk by chunk never emits a partial or literal
  token; `UNRESOLVED_TOKEN_PLACEHOLDER` and `detokenize_lenient()` give the
  response leg a way to substitute a visible placeholder for a token that
  fails to resolve instead of raising.
- `veil_pii_middleware/adapters/gateway/server.py`: the response path now
  branches on content-type. `text/event-stream` responses go through a new
  `_SseResponseRewriter` that keeps one `IncrementalDetokenizer` per
  `content_block_index` and flushes a block's leftover buffered text into a
  synthetic delta event immediately before that block's
  `content_block_stop`. Buffered JSON responses go through
  `detokenize_lenient()` in one shot (skipping the vault entirely if the
  body has no `PII_` substring, preserving the pre-existing "no-op request
  never touches the vault" guarantee). Anything else — non-JSON, non-SSE —
  is relayed byte-for-byte untouched, same as mission-7.
- No new runtime dependency; `httpx`'s existing `iter_text()`/`.read()`
  already used, both already gzip-decompression-aware.
- `core/detector.py`, `core/vault.py`, `adapters/hooks/`,
  `adapters/command/`, and the request-side of `server.py`/`tokenizer.py`
  are untouched — confirmed via `git diff --stat`.

## Live spike — SSE event shape (gate before any buffer code was written)

**Method.** Reused mission-7's separate-process pattern
(`ANTHROPIC_BASE_URL=http://127.0.0.1:<port> claude -p "<prompt>"`, never
touching this session's own connection), but this time against a throwaway
capture proxy that decoded and logged the *body content* — every SSE event
— rather than only header names as mission-7's spike did. First attempt used
`httpx`'s `iter_raw()` and failed to decode every chunk; that turned out to
be a real, useful finding of its own (below), not a bug worth silently
fixing and forgetting.

**Finding 1 — the response is gzip-compressed on the wire.** The real API
returns `Content-Encoding: gzip`; raw network chunks start with the gzip
magic bytes `\x1f\x8b`. `httpx`'s `iter_raw()` does not decompress;
`iter_bytes()`/`iter_text()` do, transparently. The response-rewrite code
uses `iter_text()` for exactly this reason — no manual decompression logic
was written. (Checked incidentally: mission-7's existing `_HOP_BY_HOP` set
already drops `content-encoding` when forwarding response headers, so there
was no pre-existing mismatched-header bug to fix here, contrary to an
initial suspicion during this spike — verified by `git diff` showing zero
change needed there.)

**Finding 2 — SSE framing.** Every event is `event: <type>\ndata:
{...}\n\n`; a `data:` line is always one complete, independently-parseable
JSON object — confirmed across every event in the captured transcript, no
event was ever split mid-JSON. This is why `_relay_sse_response` buffers at
the `"\n\n"`-terminated-event granularity (safe regardless of how the
underlying `iter_text()` chunks happen to land) rather than trying to track
raw byte boundaries.

**Finding 3 — actual event/delta shapes observed live**, from a probe
prompt asking for two sentences plus a literal `PII_EMAIL_0001`-shaped
string echoed back:

```
message_start
content_block_start (content_block.type = "text")     -- a JSON "title" block, Claude Code's own title-gen
content_block_delta (delta.type = "text_delta")
content_block_stop
message_delta / message_stop
--- separate response, the actual reply ---
message_start
content_block_start (content_block.type = "thinking")
content_block_delta (delta.type = "thinking_delta")
content_block_delta (delta.type = "signature_delta")   -- base64 signature, not text
content_block_stop
content_block_start (content_block.type = "text")
content_block_delta (delta.type = "text_delta")  x N
content_block_stop
message_delta / message_stop
```

`signature_delta` was a surprise (not named in the mission's intake
question) and is **deliberately excluded** from detokenization: it carries
a cryptographic signature over the model's internal state, never freeform
text, so there is nothing to detokenize there — confirmed by inspecting its
value in the capture (a long base64-looking blob, not conversational text).

`input_json_delta` (`partial_json`, tool-call arguments) was **not**
triggered by this probe (no tool call happened) — the intake grill's answer
was to cover every string-bearing delta type "per what the spike actually
observes," and this one wasn't observed. It is documented, stable Anthropic
Messages API surface, so it was initially included anyway on that basis —
but the code-reviewer pass (below) found a real structural problem with
that and it was **removed** from scope rather than shipped half-verified.
See "Independent review findings" below.

**No natural token split was observed** in this short probe — the
`PII_EMAIL_0001`-shaped string the probe asked for landed intact in a
single `text_delta`. The split-buffer's correctness therefore rests on
deliberately-constructed unit and integration tests (below), not on having
observed a real split in the wild. This is expected and was anticipated —
BRIEF §6 and the mission's own risk framing already treated the split as a
structural possibility to defend against, not something that needed to
reproduce on demand to be real.

Evidence artifacts (proxy script, captured log) were throwaway per the
mission's Scope section and were not committed; the excerpts above are the
durable record.

## Manual end-to-end check (real gateway, real Claude Code, response-side rewrite)

After implementation and the automated test suite passed, the finished
gateway was pointed at the real `https://api.anthropic.com` and a separate
`claude -p` process was run through it (fresh `VEIL_VAULT_KEY`, `cwd`
outside this repo — see note below on why that matters) with a prompt
containing a synthetic customer email (`jane.doe@example.com`, not a real
person) asking the model to repeat it back verbatim.

**Result:** the CLI printed `Customer email on file: jane.doe@example.com`
— the real value, not a literal `PII_EMAIL_0001` token. This is a direct,
live confirmation of this mission's whole purpose: the outbound leg
tokenized the email before it reached the real API (mission-7's existing,
unmodified guarantee), the model echoed the token back, and this mission's
new response-side rewrite resolved it back to the real value before it
reached the terminal.

**Notable incidental finding, not a bug in this project's code:** the first
two attempts at this check were run with `cwd` set to this repository, and
the spawned `claude -p` process — itself loading this repo's own
`CLAUDE.md` as project context — refused both times: once citing the
system-level rule against using the operator's own account email for an
unrelated purpose, once citing this project's own "no raw PII may reach an
external API" doctrine as a reason it personally would not echo a
PII-shaped value back, even framed as a synthetic test. Both refusals are
the model behaving exactly as its instructions intend; they are not a
gateway defect. Re-running the identical prompt with `cwd` outside the
repository (so the probe process does not load this project's own
CLAUDE.md as its own operating instructions) produced the clean result
above. Noted here because it is a real, reproducible interaction between
this project testing itself with the same tool it is a policy-file for —
worth remembering for any future live check of this kind.

## Independent review findings and what was done about each

An independent `goal-verifier` and a security-focused `code-reviewer` were
run against this diff (fresh context, read-only, no edits by either).

**Fixed:**

- **Should-fix (code-reviewer):** `input_json_delta`'s `partial_json` field
  is a raw fragment of a *separate* JSON document that the client
  concatenates across deltas and re-parses — not display text. Plain-text
  substitution there is only safe if the resolved value never contains a
  `"` or `\`; true for every entity type this codebase mints today
  (EMAIL/DNI/NIE/IBAN/PHONE/CARD/IP) but not guaranteed once a future
  free-text entity type lands, and this delta type was never actually
  observed live to validate the assumption against in the first place.
  **Removed from `_TEXT_BEARING_DELTA_FIELDS`** rather than shipped with an
  untested, structurally-fragile substitution — tool-call-argument
  rewriting is deferred to a future mission, documented in the constant's
  own comment in `server.py`.
- **Should-fix (code-reviewer):** `TokenNotFoundError` and
  `VaultIntegrityError` were collapsed into the same silent placeholder
  with zero visibility. `VaultIntegrityError` specifically means a row's
  AEAD authentication failed — CLAUDE.md's vault doctrine treats that as
  evidence of tampering or corruption, not routine "token not found."
  `_resolve_or_placeholder` (`core/tokenizer.py`) now logs a stderr line
  (token id only, never plaintext or ciphertext) distinguishing this case,
  and a new test
  (`TestIncrementalDetokenizer::test_integrity_failure_gets_placeholder_and_is_logged_distinctly`)
  reuses `test_vault.py`'s own ciphertext-swap technique to prove it fires.
- **Should-fix (code-reviewer):** `_SseResponseRewriter.rewrite` assumed a
  parsed `data:` payload was always a JSON object; a non-dict top-level
  value (list/string/number/bool) would raise `AttributeError`. Not a
  security issue (the outer exception handler already degrades this to a
  truncated-not-corrupted stream, matching mission-7's own accepted
  precedent), but inconsistent with `_rewrite_delta`'s own defensive
  `isinstance` checks. Added an `isinstance(obj, dict)` guard for
  consistency.
- **Nit (code-reviewer):** `_flush_block`'s `self._delta_kinds.pop(index,
  "text_delta")` carried an unreachable default (the two dicts are always
  updated in lockstep). Removed the dead default and added a comment
  stating the invariant instead.
- **goal-verifier (blocking):** this `handoff.md` did not exist for this
  mission when the verifier ran — the mission's own acceptance criteria
  (AC1, AC7) and merge policy require it, and the project has already
  recorded this exact failure mode as a lesson from a prior mission (spike
  evidence narrated in chat but never written to `handoff.md`). Written now
  with the actual spike transcript above, not a restated claim.

**Deferred (documented, not fixed, with reasoning):**

- **Should-fix (code-reviewer):** `_relay_buffered_response` fully buffers
  a non-streaming response into memory via `upstream.read()` before writing
  anything, where mission-7's original relay streamed with bounded memory
  and progressive time-to-first-byte. Accepted as a trade-off: detokenizing
  requires the whole JSON value up front, and this path only serves
  non-`text/event-stream` Anthropic API replies, which are small in this
  tool's actual usage (a single developer's local gateway, not a
  high-throughput production proxy). Documented inline in
  `_relay_buffered_response`'s own comment rather than fixed, per the
  reviewer's own suggested alternative.
- **goal-verifier note (not a defect):** flagged that `.harness/bin/verify`'s
  `contract` gate does not itself check `handoff.md`'s content, only
  `mission.md`'s structure — correct observation, not something this
  mission's scope covers changing.

## Validation

- `.harness/bin/verify`: **PASS**, exit `0` — contract, lint, typecheck,
  test, eval, build all green (run after every fix above, not just once at
  the start).
- `pytest`: full suite green, including 12 new/changed tests in
  `tests/test_gateway.py::TestResponseSideDetokenization` (buffered JSON
  with a previously-minted token, buffered JSON with an unminted token,
  streaming resolution of a whole token, streaming resolution of a token
  **deliberately split across two separate delta events** — the core
  chunk-boundary-safety claim, verified end to end through the real
  gateway, not just the unit-level algorithm — unresolved-token placeholder
  with a raw-socket check that chunked framing stays valid (exactly one
  `HTTP/1.1` status line, ends `0\r\n\r\n`, no raw token bytes anywhere on
  the wire), and no-token responses unaffected) and 12 new tests in
  `tests/test_tokenizer.py` (`TestIncrementalDetokenizer`,
  `TestDetokenizeLenient`: every split offset of a real token exhaustively,
  a 3-way split, a one-character-at-a-time split, an unresolved-token
  placeholder, the integrity-failure case above, end-of-stream flush of
  text that never became a token, ordinary long uppercase text not
  buffered indefinitely, two tokens in one stream, independent instances
  not sharing buffered state).
- The one pre-existing mission-7 test that asserted the *literal opposite*
  of this mission's goal (`test_response_body_is_not_rewritten_even_if_it_looks_like_a_token`)
  was replaced, not silently deleted — its class was renamed
  `TestResponseWithNoRecognizedTokenStructure` and now documents exactly
  which content still passes through untouched (non-JSON, or JSON with no
  `PII_` substring). Every other mission-7 gateway test
  (`TestRequestTokenization`, `TestMidStreamUpstreamFailure`,
  `TestFailsClosed`) passes unmodified.

## Residual risks / known gaps

- `input_json_delta`/tool-call-argument rewriting is deferred (see above) —
  a token echoed inside a tool call's arguments will still appear literally
  on screen. Not a security gap (no PII crosses to the external API either
  way; this is purely a display gap on the response leg, same class as the
  already-known `MessageDisplay`/`/reveal` gap BRIEF item 14 anticipates),
  but worth listing as a known limitation for whoever picks up item 14.
- The manual live check's exact ciphertext (the `jane.doe@example.com`
  token minted in that run) is not independently re-decryptable after the
  fact — the process-local `VEIL_VAULT_KEY` was ephemeral by design, same
  documented limitation mission-7's handoff already flagged for its own
  live check. The mechanism itself is proven by the automated test suite,
  which anyone can re-run at any time without any secret.
- One `Vault` session per gateway process (mission-7's accepted limitation)
  is unchanged and now also governs response-side resolution: a token
  minted by one gateway process instance cannot be resolved by a different
  instance, or after a restart. This mission's placeholder design means
  that specific failure mode now degrades gracefully (a placeholder, not a
  crash or a leak) rather than being untested territory.

## Status

implemented — awaiting human review for merge
