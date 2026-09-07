# Mission

## Objective

Reconcile the `session_id` used to key the gateway's `Vault`
(`veil_pii_middleware/adapters/gateway/server.py::_new_session_id()`) with
the real Claude Code `session_id` the hooks adapters already use
(`adapters/hooks/pre_tool_use.py`, `adapters/hooks/post_tool_use.py`, both
reading `payload["session_id"]`). Today the gateway mints its own
`gateway-<uuid>` once per process; a token minted by one mechanism cannot be
resolved by the other. `docs/private/BRIEF.md` §6 (risk row added
2026-09-07) and §8 item 10c record this as the top-priority day-2 item: it
breaks the project's own central scenario (BRIEF §1) — gateway and hooks
both active on the same session, now the documented normal mode of use, not
an edge case (§2.1 correction, 2026-09-07) — the first time both adapters
handle the same conversation.

Fix: read the `X-Claude-Code-Session-Id` request header (confirmed present
on real Claude Code traffic by mission-7's live spike — header *name* only,
values are never logged; see Evidence below for what mission-8 did and did
not separately confirm) and key that request's `Vault` by it, instead of the
fixed per-process UUID.

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

## Intake Grill

| Question | Answer / Evidence |
|---|---|
| What should happen if the header is missing/empty/malformed on a request (universality across every `ANTHROPIC_BASE_URL`-speaking client, not just Claude Code, is unconfirmed)? | Fall back to the existing per-process UUID (`_new_session_id()`, generated once at gateway startup, unchanged mechanism) and log a stderr warning. Not a fail-closed case: CLAUDE.md's fail-closed doctrine guards detection/tokenization capability (a request that can't be scanned must not be forwarded raw) — this header only selects which vault a token is correlated under; its absence loses cross-adapter correlation for that request, not the "no raw PII to an external API" guarantee, which the outbound `tokenize()` call still provides unconditionally either way. Blocking here would break today's working gateway-only usage (no hooks in play) for any client that doesn't send this exact header, a population this project has not verified. |
| Can the header's value change across requests within one gateway process's lifetime (e.g. Claude Code varies it within one conversation)? Unconfirmed by either prior spike's evidence. | Does not need a live spike to resolve, and does not gate the design: `server.py`'s three request-time call sites (`_tokenize_body`, the SSE response path, `_relay_buffered_response`) already open a fresh `Vault` per request rather than holding one open for the process's lifetime (verified by reading current `_handle()` — each opens its own `with Vault(...)`). Reconciliation is therefore just "read the header once per request and pass that value to the `Vault(...)` calls this request already makes" — no change to `Vault`'s lifetime management, so the header varying or not varying between requests is handled correctly either way without needing to confirm which. The throwaway live spike was explicitly declined for this reason (see Non-goals). |
| How should `tests/test_gateway.py`'s fixture change, given it builds the handler with `gateway_server._new_session_id()` directly and no test request sends the header today? | The fixture gets a fixed, hook-shaped test session id distinct from the `gateway-<uuid>` format (e.g. `hooks-<uuid>`, making it unambiguous in assertions which id space a token was minted under); a small test-local request helper attaches the header by default so every existing test becomes representative of real Claude Code traffic (which always sends this header, per mission-7's spike) without individually editing each assertion. One dedicated new test omits the header explicitly to keep the fallback-to-per-process-UUID path covered. |
| Should the suite prove real cross-adapter reconciliation, not just that the gateway reads the header? | Yes: a new test opens a second `Vault(VAULT_DB_PATH, <the header's session id>)` directly — the same call shape `pre_tool_use.py` makes from a hook payload's `session_id` — and resolves a token the gateway minted under that id via the header. This is the actual proof the fix works, without invoking hooks code (out of scope). |

Evidence gathered before asking: mission-7's live spike (`docs/missions/mission-7/handoff.md`,
Spike 1) captured real outbound header *names* from a live Claude Code
request and confirmed `X-Claude-Code-Session-Id` is present — values were
never logged, so its *stability across requests* was never checked, then or
since. Contrary to `docs/private/BRIEF.md`'s §6 risk-row phrasing ("confirmed
in both mission-7's and mission-8's live spikes"), mission-8's own handoff
(`docs/missions/mission-8/handoff.md`) does not independently mention this
header at all — its spike focused on response-body/SSE shape, not request
headers. This mission's design (previous answer) does not depend on closing
that gap, so it is noted here as a documentation correction, not chased with
a new spike. `adapters/hooks/pre_tool_use.py`/`post_tool_use.py` both already
validate `session_id` as "a non-empty `str`, else block/warn" with no
further format constraint — this mission's header validation matches that
existing precedent rather than inventing a stricter schema.

## Scope

- `veil_pii_middleware/adapters/gateway/server.py`: read the
  `X-Claude-Code-Session-Id` request header once per request in `_handle()`;
  use it (if present and non-empty after stripping whitespace) as the
  `session_id` passed to every `Vault(...)` call this request already makes
  (`_tokenize_body`, the SSE response path, `_relay_buffered_response`);
  otherwise fall back to the existing per-process UUID from
  `_new_session_id()` and log a stderr warning (no PII, no secret — matching
  existing stderr-safety precedent). `make_handler`'s `session_id` parameter
  becomes explicitly the *fallback* default, documented as such. `main()`'s
  startup log line is reworded so it no longer implies one fixed session
  governs every request.
- `tests/test_gateway.py`: fixture carries a fixed, hook-shaped test session
  id and a request helper that attaches the header by default; a new test
  covering the header-missing fallback path; a new test proving real
  cross-adapter reconciliation by directly resolving a gateway-minted token
  through a second `Vault` opened with the header's session id (mimicking
  `pre_tool_use.py`'s own call shape).

## Non-goals

- Any change to `core/vault.py` or `core/tokenizer.py` — this is purely a
  "which `session_id` string gets passed in" fix at the adapter layer; the
  vault/tokenizer contract is unmodified.
- Any change to `adapters/hooks/pre_tool_use.py`/`post_tool_use.py` — they
  already do the correct thing (use the real session id from their own
  payload); this mission makes the gateway match them, not the reverse.
- A live spike to confirm whether the header's value is stable or varies
  within one Claude Code process/conversation — declined; the design
  (per-request `Vault` opens, unchanged) is correct regardless of the
  answer, so nothing gates on it (Intake Grill, Q2).
- Local LLM detector layer, `input_json_delta`/tool-call-argument rewriting,
  and `MessageDisplay`/`/reveal` — all already out of scope per prior
  missions' decisions, not reopened here.
- Format/schema validation of the header value beyond "non-empty after
  stripping whitespace" — matches existing `session_id` validation
  precedent in the hooks adapters; no new validation invented.
- Multi-process/concurrent-gateway vault correlation — mission-7's accepted
  limitation, unchanged.

## Acceptance criteria

1. A token minted by the gateway for a request carrying
   `X-Claude-Code-Session-Id: <sid>` is resolvable by directly opening
   `Vault(VAULT_DB_PATH, "<sid>")` — the same shape `pre_tool_use.py` uses —
   proving real cross-adapter reconciliation, not just that the header is
   read.
2. A request with the header missing, empty, or whitespace-only still
   completes successfully (falls back to the existing per-process UUID
   behavior, unchanged from mission-7/8) and emits a stderr warning
   containing no PII or secret.
3. Every pre-existing `tests/test_gateway.py` test still passes with the
   fixture's new default header attached (i.e. this mission does not change
   request/response tokenization behavior for traffic that already carried
   no session-id concerns).
4. `core/vault.py` and `core/tokenizer.py` are unmodified (confirmed by
   `git diff --stat`).
5. `.harness/bin/verify` exits 0.
6. `.harness/handoff.md` documents the fix, the corrected evidence record
   for BRIEF §6's citation gap, and the reconciliation test's result plainly
   enough that a human can decide, without re-running anything, whether the
   fix is correct.

## Risk

Risk class: medium

Rationale: does not touch `core/vault.py` or `core/tokenizer.py` (the
designated high-risk surface per CLAUDE.md's "Review and risk" convention),
but changes which `session_id` a gateway request's `Vault` calls are keyed
by — a mistake here would either silently break correlation (a token minted
under the wrong id resolves as "not found," a display defect, not a PII
leak, same class of failure mission-8 already made non-fatal via the
placeholder path) or, in the worst case, key a vault operation using an
untrusted value in an unintended way. The header is client-supplied
(HTTP-level, not merely local-process state) governing vault keying for the
first time in this project — the same trust boundary the hooks adapters
already accept for `payload["session_id"]` from Claude Code itself (a local
process on the developer's own machine, not a hostile network peer), so this
is treated as consistent with existing precedent, not a new attack surface,
but is called out explicitly since it is a new instance of that pattern.

## Merge policy

No commit, push, or merge without explicit human review of the diff and
`.harness/handoff.md`. This mission does not merge itself.

## Tooling And Approach Assessment

1. **What project type is this?** A correctness fix reconciling an
   identity-keying mismatch between two already-implemented adapters
   (gateway vs. hooks) in an existing network-facing component — not new
   feature surface, and (per the Intake Grill) no live spike gates the
   design this time, unlike missions 7 and 8.
2. **What methods, tooling, or approaches does that imply?** Read an
   inbound HTTP header per request via the already-available
   `self.headers` on `BaseHTTPRequestHandler` (no new dependency); thread a
   per-request `session_id` value through the three call sites in
   `_handle()` that already open a `Vault` per request (no lifetime change
   needed); extend the existing httpx-based integration test harness in
   `tests/test_gateway.py` with a direct second `Vault` open to prove real
   hook-shaped resolution, without invoking any hooks code.
3. **What risks or missing decisions does it reveal?** (a) the gateway now
   lets an inbound HTTP header select which vault "session" a token is
   minted under — the same trust boundary the hooks adapters already accept
   for their own payload's `session_id`, not a new attack surface, but the
   first time an HTTP header (rather than local hook-invocation state)
   plays that role, worth stating explicitly. (b) "malformed" is scoped
   narrowly — missing, or empty/whitespace-only after stripping — matching
   the hooks adapters' own existing `session_id` validation, not a new
   schema. (c) `main()`'s startup log line currently reads
   `(session {session_id})`, implying one fixed session per process; must
   be reworded to state it is only the fallback value now.
4. **What should be added to validation?** A test proving cross-adapter
   reconciliation by directly resolving a gateway-minted token through a
   second `Vault` opened with the header's session id; a test for the
   header-missing fallback path, including its stderr warning containing no
   PII/secret (matching the existing `test_no_secret_or_pii_appears_in_gateway_stderr`
   pattern); confirmation every pre-existing test still passes with the
   fixture's new default header attached; `git diff --stat` confirming
   `core/vault.py`/`core/tokenizer.py` are untouched.
5. **What is explicitly not needed now?** Any change to `core/vault.py` or
   `core/tokenizer.py`; any change to the hooks adapters; a live spike (the
   design does not depend on confirming header stability); header
   format/schema validation beyond non-empty; multi-process/concurrent
   gateway vault correlation (mission-7's accepted limitation, unchanged).

## Status

implemented — awaiting human review for merge (see `.harness/handoff.md`)
