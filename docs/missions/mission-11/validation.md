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

None run this mission — declined by the Intake Grill. The audit-log JSONL
schema and the two new SSE streams (audit tail, pipeline stage) are shapes
this codebase defines itself, not a third-party dialect (unlike mission-10's
LM Studio spike). Proven by tests plus a manual demo-run checklist in
`.harness/handoff.md`. Item 15's DNI/NIE prompt fix is captured live against
LM Studio in `handoff.md` only if such an instance is reachable this
mission — otherwise documented as a required manual follow-up, matching how
mission-10 handled its own live-verification gap.

## Executable Checks

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore`, no implicit `Any` |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Audit log never writes plaintext (AC1) | pytest: `AuditLog.record(...)` called with a realistic email/DNI value; assert the value does not appear as a substring anywhere in the written file, only `sha256(value).hexdigest()` does | exit `0` |
| Audit log entry shape (AC1) | pytest: one `record()` call produces one JSON line with exactly timestamp/token/entity_type/detector/offset/value_hash/adapter fields, no extras, no raw value | exit `0` |
| Concurrent append safety | pytest: two threads/processes call `AuditLog.record(...)` interleaved a few hundred times each against the same file; assert every resulting line parses as valid JSON and the total line count equals the total call count (no merged/corrupted lines) | exit `0` |
| Tokenizer audit wiring, one entry per span (AC2) | pytest: `tokenize(text, vault, audit_log=log, adapter="test")` on text with two distinct PII spans; assert exactly two audit entries, each matching one span's token/entity_type/detector/offset | exit `0` |
| Tokenizer audit wiring, omitted stays silent (AC2) | pytest: existing tokenizer/gateway/hooks tests that never pass `audit_log` still pass unmodified, and a fresh assertion confirms no audit file is created/touched when `audit_log` is omitted | exit `0` |
| Adapter tagging (AC3) | pytest, one per adapter: gateway request tokenization, `post_tool_use.py`, `sanitize.py` each produce an audit entry tagged with their own distinct `adapter` string for a token they mint | exit `0` |
| Panel static route (AC4) | pytest: `GET /veil-panel` returns 200, `Content-Type: text/html`, and the body contains the CDN `<script>` tags for React/Babel Standalone (no bundler output, no `node_modules` reference) | exit `0` |
| Audit SSE tail (AC5) | pytest: open `GET /veil-panel/audit-stream` in a background thread, append a line to `.veil/audit.jsonl` (or trigger a real tokenize-through-gateway request), assert the SSE client receives a corresponding event within a bounded wait | exit `0` |
| Pipeline SSE stream (AC5) | pytest: open `GET /veil-panel/pipeline-stream`, issue a real proxied request through the gateway's existing test harness (fake upstream), assert at least one stage event per documented stage name arrives, and that no such event is ever written to `.veil/audit.jsonl` | exit `0` |
| Vault list never leaks plaintext (AC6) | pytest: mint a token, call `GET /veil-panel/vault`, assert the response contains no decrypted value anywhere (only token/entity_type/ciphertext_b64/created_at) | exit `0` |
| Vault reveal returns the right value (AC6) | pytest: mint a token for a known value, `POST /veil-panel/vault/reveal` with that token, assert the response is exactly that value; a token from a different/unknown session is rejected, not resolved | exit `0` |
| Metrics endpoint (AC7) | pytest: `GET /veil-panel/metrics` returns 200 JSON matching `demo/eval.py --mode regex`'s report shape (per-entity-type recall/precision) | exit `0` |
| Panel route dispatch does not shadow the proxy (Tooling Assessment 3b) | pytest: a request to a non-`/veil-panel` path (e.g. `/v1/messages`) still proxies to the fake upstream exactly as before this mission | exit `0` |
| Item 15 prompt content (AC9) | pytest: assert `_SYSTEM_PROMPT` (or the request body it builds) contains the DNI-vs-NIE leading-letter distinguishing rule and IBAN/PHONE/CARD/IP structural rules as literal text | exit `0` |
| Item 15 live before/after (AC9) | manual, only if an LM Studio instance is reachable this mission: `demo/eval.py --mode llm` DNI/NIE recall/precision, captured before and after the prompt edit, in `handoff.md` | documented either way |
| Item 16 decoy mode-routing (AC10) | pytest: `evaluate_decoys(..., use_regex=False, use_llm=True, llm_client=<mock>)` actually invokes the mock client, proving the hardcoded `detect_regex()` call is gone | exit `0` |
| Item 16 new decoy (AC10) | pytest: `demo/seed.py`'s decoys include an invalid-octet-IP entry, and `detect_regex()` does not flag it | exit `0` |
| Recall/decoy gates unaffected | `.harness/bin/verify`'s `eval` row (`--min-recall 1.0 --max-false-positives 0`, regex-only mode) | exit `0`, identical pass/fail semantics to before this mission |
| `core/vault.py` change is read-only and narrow | `git diff -- veil_pii_middleware/core/vault.py` reviewed by hand | only `list_mappings()` (or equivalently named) added; no change to `get_or_create_token`, `resolve`, `purge_expired`, the schema, or any cryptographic primitive |
| No new runtime dependency | `git diff -- pyproject.toml` reviewed by hand | no new `dependencies` entry (frontend loads React/Babel from a CDN at request time, not installed) |

## Scrutiny Validator

- Confirm `AuditLog.record()` is the *only* place that formats a line for
  `.veil/audit.jsonl`, and that it hashes the value internally — no call
  site anywhere constructs or passes a pre-hashed or raw value string that
  could be gotten wrong at the edge.
- Confirm every new gateway route lives strictly under the `/veil-panel`
  prefix and is checked before the existing unconditional proxy
  pass-through, so a real Anthropic API request can never be intercepted by
  a panel route (and vice versa — a panel request can never leak upstream).
- Confirm `/veil-panel/vault` never serializes a decrypted value under any
  field name, and `/veil-panel/vault/reveal` calls the existing
  `Vault.resolve()` (not a new decryption path) so it inherits that method's
  session-scoping and AEAD-authentication-failure handling unchanged.
- Confirm the pipeline-stage SSE stream is populated only by the gateway's
  own request/response handling code (no hook or command adapter publishes
  to it), and that no pipeline-stage event is ever written to
  `.veil/audit.jsonl` — the two streams stay structurally separate, per the
  Intake Grill decision.
- Confirm `list_mappings()` is genuinely read-only: no write, no schema
  migration, no change to any existing query in `core/vault.py`.
- Confirm the frontend has no build step: no `package.json`, no bundler
  config, no `node_modules` reference; React/Babel are `<script src="https://...">`
  CDN tags loaded at request time, and the page still functions if read
  directly as a static file.
- Confirm item 15's prompt edit changes only text in `_SYSTEM_PROMPT` (or
  clearly-scoped request-building code around it) — no change to
  `LLMClient`, `detect_llm`'s parsing/offset logic, or `detect()`'s
  fail-closed behavior.
- Confirm item 16's `evaluate_decoys()` change actually threads through the
  same `use_regex`/`use_llm`/`llm_client` values `main()` resolved for
  `evaluate()` on the same run, rather than introducing a second,
  independently-resolved set of flags that could disagree with the `--mode`
  the user asked for.
- Confirm no `.veil/audit.jsonl` entry, vault-list response, or gateway
  stderr log line anywhere in the new code contains a raw PII value.
  **Correction from an earlier draft of this bullet** (flagged by
  independent security review during implementation): the *pipeline-stage*
  SSE stream is a deliberate, mission-scoped exception, not a leak --
  `mission.md`'s own Scope section requires it to carry the actual
  request/response text (BRIEF §5's four panes cannot be built otherwise:
  "Input -- the user's text with detected spans highlighted"). This bullet
  originally banned raw PII from that stream too, contradicting the mission
  it was supposed to validate; the correct invariant is that raw PII is
  confined to the pipeline stream and the one-token `/veil-panel/vault/reveal`
  response -- both bound to the gateway's existing `127.0.0.1`-only listener,
  both requiring an active local subscriber/request, neither persisted to
  disk. See `.harness/handoff.md` for the explicit human sign-off this
  distinction needs before merge.

## User-Testing Validator

- No live spike was run for the two new SSE streams or the panel HTTP
  surface — pytest-level endpoint-contract tests are the proof of wire
  format and correctness, not of real browser rendering.
- Required manual follow-up, written into `.harness/handoff.md` as a
  step-by-step checklist: start the gateway (`python -m
  veil_pii_middleware.adapters.gateway.server`), open
  `http://127.0.0.1:8787/veil-panel` in a browser, run a real Claude Code
  session against the gateway (or a scripted `httpx` request standing in for
  one), and confirm: the four panes populate and update live, span
  highlighting visibly differs by detector color, hovering a token in the
  outbound pane shows its entity type, the vault inspector lists tokens
  without ever showing a value until "reveal" is clicked, the metrics pane
  renders `eval.py`'s numbers, and the pipeline diagram highlights each
  stage in sequence as the request proceeds.
- If an LM Studio instance is reachable this mission: re-run
  `demo/eval.py --mode llm` before and after the item 15 prompt edit and
  record the DNI/NIE recall/precision delta in `handoff.md`. If none is
  reachable, say so plainly and leave it as required follow-up, the same way
  mission-10 did for its own live verification gap.
