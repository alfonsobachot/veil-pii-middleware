# Mission

## Objective

Implement response-side (model -> client) rewriting for the gateway
(`veil_pii_middleware/adapters/gateway/server.py`, BRIEF work-plan day-2 item
10b, `docs/private/BRIEF.md` §8 and the SSE row of §6). Mission-7 built the
gateway tokenizing only the outbound request leg and relaying the response
byte-for-byte untouched, as an explicit, documented scope cut — this mission
closes that gap so a token the model echoes back (e.g. `PII_EMAIL_0001`)
resolves to its real value on screen instead of appearing literally.

Before designing the buffer, this mission must live-verify — not assume —
the actual shape of Anthropic's streaming response: which `content_block_delta`
event types carry incremental text, and whether a token's text can be split
between two distinct SSE events at the JSON-decoded content level (not just
between raw HTTP chunks). Mission-7's own spike already confirmed the
response is `Transfer-Encoding: chunked` (BRIEF §6), but only inspected
header names, never the body content — so the event-shape question is still
open and gates the buffer's design, the same way the auth question gated
mission-7's gateway code. This project has already been burned three times by
an unverified design assumption (`/veil`'s `$ARGUMENTS` behavior, the hooks
permission model, and near-miss on mission-7's own streaming shape) — confirm
live before building.

**Architecture decision (resolved, not open):** the buffering logic — what is
safe to flush now vs. what must be retained because it could be the prefix of
a still-arriving token — is detection/substitution logic, not transport, per
CLAUDE.md's "adapters never re-implement detection or substitution; they only
wire `core/` into a transport." It lives as a new incremental-detokenize
function in `core/tokenizer.py` (returning something like
`(safe_text_to_emit, retained_tail)`), reusing `core/vault.py::Vault.resolve()`
for the actual token -> value lookup. `adapters/gateway/server.py` only calls
that new function, the same way it already calls `tokenize()` on the request
leg. Unlike mission-7 (where touching `core/tokenizer.py` was out of scope
because nothing needed it), modifying `core/tokenizer.py` is explicitly in
scope here — CLAUDE.md's "Review and risk" convention still applies, so this
change requires human approval before merge regardless of execution lane.

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-external-write, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

Note: `before-external-write` covers the live SSE-shape spike specifically
(sends a real prompt through a throwaway pass-through proxy using a separate
`claude -p` process against the user's real subscription, same pattern as
mission-7 spike 1) — approval of the mission as a whole does not by itself
authorize running that spike; see the Validation Contract's Live Spike table.

## Intake Grill

| Question | Answer / Evidence |
|---|---|
| A token in the model's response can fail to resolve mid-stream (e.g. the gateway process restarted and lost the in-memory/db-backed mapping, or the vault DB was reset). `Vault.resolve()` raises in that case. `send_response`/status line is already on the wire by then (mission-7's own comment in `server.py` explains why a second status line would corrupt chunked framing), so the response cannot be aborted with a clean HTTP error the way the request leg does. What should happen to that fragment? | Substitute a visible placeholder (e.g. `[unresolved token]`) in place of the unresolved token and keep streaming the rest of the response normally. Chosen over truncating the stream (simpler, reuses an existing precedent, but leaves the user staring at a silently cut-off answer) and over leaving the literal `PII_<TYPE>_<NNNN>` token in place (status quo before this mission; reintroduces exactly the defect this mission exists to close, in the one case it can't otherwise fix). This is new behavior distinct from the request-leg's fail-closed rule: a resolve failure here leaks no PII either way, so the choice is a UX/legibility tradeoff, not a security one — CLAUDE.md's fail-closed doctrine (`core/detector.py`/vault/tokenizer failures must block the request, never forward raw PII) is about the *outbound* leg and is unmodified by this decision. |
| Anthropic's streaming response can carry incremental text in more than one delta shape — `text_delta` (visible reply text), `input_json_delta` (tool-call arguments), `thinking_delta` (extended reasoning) — confirmed by what the live spike actually observes. Should the incremental detokenizer apply to all of them, or only to visible reply text? | All string-bearing delta types the spike confirms exist. A token could just as easily be echoed inside a tool-call argument as in the reply text; narrowing to `text_delta` only would leave a token visibly unresolved in any other delta shape, which is the same defect this mission exists to close, just relocated rather than fixed. The exact set of delta types handled is whatever the live spike's evidence shows Anthropic actually sends — not guessed in advance. |
| What test coverage does the split-token buffer need, given `tests/test_gateway.py`'s existing style (real gateway against a fake local upstream)? | Both: unit tests for the new incremental function in `core/tokenizer.py` (splitting a known token span at every possible offset, confirming the same resolved output either way) AND at least one gateway-level integration test with a fake SSE upstream that deliberately splits a token across two separate `content_block_delta` events, confirming `server.py` actually wires the new function into the real chunked-response framing correctly — not just that the algorithm is correct in isolation. |

Evidence gathered before asking: `veil_pii_middleware/adapters/gateway/server.py`
(`_handle`, mission-7) currently relays the upstream response with
`upstream.iter_bytes()` untouched and re-chunks it manually; its own comment
block explains why `send_response` can only fire once per request and why a
failure after `response_started = True` must stop writing rather than call
`_write_error` again — this constraint carries forward unchanged and bounds
what "fail on resolve" can mean here. `core/tokenizer.py::detokenize()` and
`_TOKEN_PATTERN` (`PII_[A-Z]+_\d{4}`, anchored to exactly four digits) are
unchanged and already tested (`tests/test_tokenizer.py`); the new incremental
function reuses that same pattern rather than inventing a new token format.
`docs/private/BRIEF.md` §6's SSE row and §8 item 10b are the design of
record for this mission; mission-7's `handoff.md` confirmed
`Transfer-Encoding: chunked` on the real API's response but never inspected
body content, which is exactly the gap this mission's spike closes.

## Scope

- `core/tokenizer.py`: a new incremental-detokenize function that, given
  previously-retained pending text plus a new chunk of text, returns the
  portion now safe to emit (fully resolved, no PII tokens left) and the
  portion still to retain (a possible token prefix — computed against the
  existing `PII_[A-Z]+_\d{4}` shape, not a new token format). Reuses
  `Vault.resolve()`; on an unresolved token, substitutes the agreed visible
  placeholder rather than raising past this function, matching the Intake
  Grill decision above (a resolve failure here is a display concern, not a
  security one, so it does not propagate as a hard failure the way a
  request-leg tokenize/vault error does).
- A throwaway (not committed) live spike, run before any buffer code is
  written, that captures the actual decoded body/SSE event shape of a real
  streaming Anthropic response — reusing mission-7's separate-process
  pattern (`ANTHROPIC_BASE_URL=... claude -p "..."` against a local
  pass-through proxy) but this time logging body content (event types,
  `content_block_delta` shapes), not just header names.
- `veil_pii_middleware/adapters/gateway/server.py`: wire the new incremental
  function into the response-relay path in `_handle`, replacing the current
  untouched `upstream.iter_bytes()` relay for streaming responses whose
  content carries the delta shapes confirmed by the spike. Non-streaming
  (buffered) JSON responses get the equivalent one-shot treatment via
  `core/tokenizer.py::detokenize()`, already existing and unmodified.
- Tests: `tests/test_tokenizer.py` unit tests for the new incremental
  function (every split offset of a known token span; the resolve-failure
  placeholder path); `tests/test_gateway.py` integration test(s) with a fake
  SSE upstream deliberately splitting a token across chunks, plus a test for
  a token that never resolves.
- `.harness/handoff.md` documents the live spike's actual finding (event
  shape, whether/where splits occur) and the resulting buffer design.

## Non-goals

- Deciding whether this replaces or complements `MessageDisplay`/`/reveal`
  (BRIEF item 14) — open question, left for a future review.
- Any change to the outbound request-side tokenization path (`tokenize()`,
  the request-body handling in `_handle`) — mission-7's behavior there is
  correct and unmodified.
- Persisting buffered/retained text across separate HTTP connections or
  requests — the incremental buffer's lifetime is exactly one streaming
  response.
- Handling non-Anthropic-shaped response bodies beyond whatever
  pass-through/fail-closed behavior mission-7 already established for
  non-JSON request bodies (unchanged; this mission only touches the response
  leg).
- Local LLM detector layer, `eval.py`, audit log, frontend (BRIEF items
  11-13).
- Multi-session/multi-window vault correlation in gateway mode (mission-7's
  accepted limitation, unchanged).

## Acceptance criteria

1. Live-verified (not assumed): the spike's actual captured evidence states
   which `content_block_delta` event/delta types carry incremental text in a
   real streaming response, and whether a token's text was observed (or
   deliberately reproduced) split across two such events.
2. A token minted on the request leg and echoed back by the model in a
   streaming response resolves to its real value in what the client
   receives, including when the token's text is split across two separate
   upstream chunks/events.
3. A token that fails to resolve mid-stream is replaced with the agreed
   visible placeholder; the stream continues and its chunked framing is
   never corrupted (no second status line, no malformed chunk boundary).
4. The response reaching the client for content with no tokens in it is
   unaffected — same bytes as mission-7's existing untouched relay, module
   the one-token-buffer-worth of latency inherent to any streaming rewrite.
5. `core/tokenizer.py`'s existing request-side `tokenize()`/`detokenize()`
   behavior and its existing tests are unaffected.
6. `.harness/bin/verify` exits 0.
7. `.harness/handoff.md` states the spike's finding and the resulting design
   plainly enough that a human can decide, without re-running anything,
   whether the response-side rewrite is correct.

## Risk

Risk class: high

Rationale: modifies `core/tokenizer.py`, the designated high-risk surface
per CLAUDE.md's "Review and risk" convention (a missed span is a leak, an
unstable token silently breaks correlation — here, a buffering bug could
either leak a raw token to the screen, which is a display defect but not a
security one since the token itself is not PII, or worse, mis-resolve a
token to the wrong value if buffering logic is sloppy about session/entity
binding). Also runs a live spike sending a real prompt through the user's
authenticated Claude Code session/subscription (same class of action as
mission-7 spike 1), and touches the same network-facing response-relay code
mission-7 flagged as easy to corrupt (framing/chunked-encoding bugs).

## Merge policy

No commit, push, or merge without explicit human review of the diff, the
live spike's captured evidence, and `.harness/handoff.md`. This mission does
not merge itself.

## Tooling And Approach Assessment

1. **What project type is this?** An extension of an existing network-facing
   component (mission-7's gateway) to parse and rewrite streamed response
   content in place, rather than pure pass-through — a materially different
   risk than mission-7's request-only tokenization, because now the gateway
   must reconstruct meaning across chunk boundaries instead of just
   forwarding bytes.
2. **What methods, tooling, or approaches does that imply?** A live spike
   first (same throwaway separate-process pattern as mission-7), this time
   logging body content; a streaming-safe algorithm in `core/tokenizer.py`
   for "longest safe-to-flush prefix vs. retained possible-token-tail" against
   the existing `PII_[A-Z]+_\d{4}` pattern (an unbounded `[A-Z]+` run means
   the "is this still a possible token prefix" check must handle an
   arbitrarily long trailing all-caps run, not just a fixed lookahead
   window); reuse of `httpx`'s existing SSE/chunk iteration already present
   in `server.py`, no new dependency.
3. **What risks or missing decisions does it reveal?** (a) SSE framing is
   line-oriented (`data: {...}\n\n`) and each `data:` line is a complete JSON
   event by construction — so a split can only happen at the
   reassembled-text level across multiple complete events, never mid-JSON or
   mid-UTF-8-byte; the spike must confirm this holds for the real API before
   the buffer is built against decoded text rather than raw bytes. (b) An
   unbounded uppercase run (e.g. a long all-caps sentence in the reply) looks
   like an indefinitely-growing possible-token-prefix under a naive
   check — the algorithm needs a bound (e.g. stop treating a run as a
   candidate once it's longer than any real entity-type name could be) or it
   will buffer unboundedly on ordinary text. (c) Each `content_block_index`
   is logically independent (a token cannot span two different content
   blocks), so the buffer must be keyed per block, not global to the whole
   response.
4. **What should be added to validation?** The live spike's captured
   evidence (event types actually observed) as committed-to-handoff proof,
   not a claim; a unit test sweeping every split offset of a real token
   span; an integration test proving the gateway's chunked framing survives
   a split-token rewrite; a test for the resolve-failure placeholder path; a
   test that ordinary long all-caps text does not get buffered
   indefinitely/dropped.
5. **What is explicitly not needed now?** Any change to request-side
   tokenization; MessageDisplay/`/reveal` design; the local LLM detector,
   eval, audit log, or frontend; TLS/cert work (mission-7 already answered
   this); multi-session vault correlation.

## Status

implemented — awaiting human review for merge (see `.harness/handoff.md`)
