# Mission

## Objective

Implement work-plan item 13 (BRIEF §8): the audit log (`core/audit.py`, today
only a module docstring with no implementation) and the observability
frontend (BRIEF §5) — the last piece before README/demo recording (item 14).
Also fold in two BRIEF-documented follow-up items as drive-by fixes, per
explicit user decision in this mission's Intake Grill: item 15 (LLM prompt
structural grounding per entity type) and item 16 (run decoys through the
LLM layer too).

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

## Intake Grill

| Question | Answer |
|---|---|
| How do the audit log's schema (BRIEF §3.4: per-PII-instance provenance, no raw values, persisted JSONL) and the frontend's requested pipeline-stage-transition events (request received → forwarded → response received → detokenized → displayed, not about any single PII instance) relate? | Separate: `core/audit.py`'s JSONL stays exactly BRIEF §3.4's schema, unmodified. Pipeline-stage-transition events are a distinct, non-persisted, in-memory event stream the gateway publishes directly over its own SSE endpoint — nothing about a stage transition needs to survive a restart or be replayed, and mixing it into the audit file would blur that file's one documented job. |
| Which process(es) emit pipeline-stage-transition events in this mission? | Gateway only. It already owns the full request → tokenize → forward → response → detokenize → relay cycle as one long-running process. Hooks (short-lived, one process per invocation) do not emit pipeline-stage events in this mission — a separate, later concern if ever wanted. (Hooks still write ordinary audit-log entries for tokens they mint — that is the pre-existing, unrelated JSONL stream, not the new SSE one.) |
| How much polish should the pipeline-flow visualization get, given BRIEF's explicit "timebox this... most likely to swallow a whole day in animation polish" warning? | MVP first pass: static stage blocks connected by arrows; the current stage highlights (a CSS class swap) as each SSE event arrives. No motion/particle/easing animation. Further polish is left to a later mission if wanted. |
| What should the frontend be built with, given no JS build toolchain exists in this project (no package.json/bundler)? | React — loaded via CDN as a UMD/production build, with Babel Standalone (also CDN) transforming inline JSX at page-load time in the browser. No `package.json`, no bundler, no new dev-dependency; the gateway serves one static HTML file (plus any co-located static assets) exactly the way BRIEF §5 asks ("served by the proxy process itself... no CORS to fight"). |
| Does this mission need a throwaway live spike before building the pipeline-stage stream and the audit-log SSE tail? | No. Both are shapes this codebase defines itself (unlike LM Studio's third-party response dialect, which is exactly why mission-10 did spike). Build directly; prove with unit/integration tests plus a concrete manual demo-run checklist in `.harness/handoff.md`. |
| Should this mission also fix BRIEF items 15 (LLM prompt has no structural/format grounding per entity type — measured live in mission-10 causing DNI/NIE mislabeling) and 16 (`evaluate_decoys()` hardcodes `detect_regex()`, so decoys never exercise the LLM layer in any `--mode`)? | Yes, both, as drive-by fixes — explicit user decision, overriding the assistant's own recommendation to leave them out. Rationale given: the frontend's regex-vs-LLM span coloring is the visual argument for the two-layer comparison these items' correctness affects. |

Evidence gathered before asking: `core/audit.py`'s current docstring-only state;
BRIEF §3.4 and §5 verbatim; `core/tokenizer.py`'s existing `tokenize()`/
`_tokenize_text()` call chain and its three current callers (`adapters/gateway/
server.py`, `adapters/hooks/post_tool_use.py`, `adapters/command/sanitize.py`);
`core/vault.py`'s schema (no existing "list all mappings" read method); BRIEF
§6's items 15/16 rows (exact fixes already specified there); `demo/eval.py`'s
`evaluate_decoys()` current `detect_regex()`-only call; `demo/seed.py`'s
`_build_decoys()` pattern for adding a new decoy category;
`adapters/gateway/server.py`'s existing `ThreadingHTTPServer`/`BaseHTTPRequestHandler`
routing (currently unconditional proxy pass-through in every `do_*` method,
with no static/local-route dispatch yet) and its existing `_vault_lock` for
cross-request `Vault` write safety; `pyproject.toml` (no JS tooling, no new
Python runtime dependency needed for this mission either — `httpx`/`cryptography`
already present, stdlib covers JSONL/SSE/HTTP).

## Scope

- `veil_pii_middleware/core/audit.py`: implement `AuditLog` — append-only
  JSONL writer (`record(*, token, entity_type, detector, offset, value,
  adapter)`, computing `sha256(value).hexdigest()` internally so no call site
  ever has to remember not to pass the raw value through as the hash; the raw
  `value` argument is accepted only to be hashed, never persisted) plus a
  reader (`AuditLog.read_entries(path)` or equivalent) for tests and the
  gateway's SSE tail. Timestamp is an ISO-8601 UTC string (human-scannable in
  the JSONL and the frontend, distinct from `vault.py`'s internal unix-int
  convention). Frozen dataclass `AuditEntry` mirrors the five documented
  fields (BRIEF §3.4) plus `adapter`.
- `veil_pii_middleware/core/tokenizer.py`: `tokenize()`/`_tokenize()`/
  `_tokenize_text()` gain optional `audit_log: AuditLog | None = None,
  adapter: str | None = None` parameters. When `audit_log` is supplied,
  `adapter` must be a non-empty string (raises `ValueError` otherwise) and
  exactly one `AuditLog.record(...)` call is made per span actually
  substituted, using that span's own offset/entity_type/detector/text. When
  `audit_log` is omitted (every existing call site and test, unchanged), no
  file is touched — identical behavior to before this mission.
- `veil_pii_middleware/core/vault.py`: one new read-only method,
  `list_mappings() -> list[MappingInfo]` (a small frozen dataclass:
  `token`, `entity_type`, `ciphertext_b64`, `created_at`), scoped to this
  vault's own `session_id`, for the vault-inspector pane. No schema change,
  no change to encryption/HMAC/key-derivation, no change to
  `get_or_create_token`/`resolve`/`purge_expired`.
- `veil_pii_middleware/adapters/gateway/server.py`:
  - A shared, fixed-path `AuditLog` (`.veil/audit.jsonl`, git-ignored,
    matching `VAULT_DB_PATH`'s convention) passed into `tokenize()` at the
    existing request-tokenization call site, with `adapter="gateway"`.
  - New local routes intercepted in `do_GET`/`do_POST` *before* the existing
    unconditional upstream-proxy pass-through, under a reserved
    `/veil-panel` path prefix (chosen to have zero realistic collision with
    any real Anthropic API path):
    - `GET /veil-panel` — serves the static frontend page (see below).
    - `GET /veil-panel/audit-stream` — SSE, tails `.veil/audit.jsonl` from
      wherever it is when the connection opens, emitting each new line as an
      SSE event as it is appended (by *any* process — gateway, hooks, or
      `/veil` — since this reads the shared file, not an in-process queue).
    - `GET /veil-panel/pipeline-stream` — SSE, in-memory only. The gateway's
      own request handler publishes one event per pipeline stage
      (`received`, `tokenized`/`forwarded`, `response_received`,
      `detokenized`, `displayed` — exact names finalized in implementation)
      for each request/response cycle it itself processes, including the
      stage's own text content (this is a local, ephemeral, developer-facing
      stream, not the audit log — BRIEF §5's four panes require showing the
      actual text, which is exactly why this must not be the same stream as
      the no-raw-values audit log).
    - `GET /veil-panel/vault` — JSON list from `Vault.list_mappings()` for
      the most-recently-seen `X-Claude-Code-Session-Id` (or an explicit
      `?session_id=` override); never includes a decrypted value.
    - `POST /veil-panel/vault/reveal` — body `{"token": "..."}`; returns the
      one decrypted value for that token via the existing `Vault.resolve()`
      (explicit action, never included in the list response, matching
      BRIEF §5's "revealing a value requires an explicit action, never shown
      by default").
    - `GET /veil-panel/metrics` — JSON summary of `demo/eval.py`'s
      `evaluate()` regex-mode report (recall/precision per entity type).
  - Every new route stays inside the gateway's existing `127.0.0.1`-only
    bind — no new authentication layer is introduced (matches the project's
    existing local-only trust model; see Non-goals).
- `veil_pii_middleware/frontend/`: a static page (HTML/CSS/JS, no build
  step) implementing BRIEF §5's four panes, the vault inspector, the metrics
  pane, and the new pipeline-stage flow diagram (MVP: static blocks/arrows,
  current stage highlighted). Built with React (CDN UMD build) + Babel
  Standalone (CDN) for in-browser JSX, consuming `/veil-panel/audit-stream`
  and `/veil-panel/pipeline-stream` via `EventSource`, and the vault/metrics
  endpoints via `fetch`.
- `veil_pii_middleware/adapters/hooks/post_tool_use.py`: passes the same
  shared `AuditLog` and `adapter="hooks:post_tool_use"` into its existing
  `tokenize()` call.
- `veil_pii_middleware/adapters/command/sanitize.py`: passes the same shared
  `AuditLog` and `adapter="command"` into its existing `tokenize()` call.
- `veil_pii_middleware/core/detector.py` (item 15): `_SYSTEM_PROMPT` gains
  per-type structural rules and short examples — the DNI-vs-NIE
  leading-letter rule, IBAN/PHONE/CARD length/prefix rules, and IP's
  four-octet 0-255 range — per BRIEF §6's "no structural/format grounding"
  row. No signature or behavioral change outside the prompt text itself.
- `veil_pii_middleware/demo/eval.py` (item 16): `evaluate_decoys()` gains
  the same `use_regex`/`use_llm`/`llm_client` parameters `evaluate()`
  already takes, routing through `detect()` instead of hardcoding
  `detect_regex()`; `main()` passes through the `--mode`-resolved values so
  the decoy check exercises whatever layer(s) the calling mode enabled.
- `veil_pii_middleware/demo/seed.py` (item 16): a new
  `INVALID_OCTET_IP`-category decoy (an IP-shaped string with an octet above
  255) added to `_build_decoys()`, per BRIEF §6's "decoy check never
  exercises the LLM layer" row.
- Tests: `tests/test_audit.py` (new — append/read round trip, no plaintext
  ever written including on the value-hash path, concurrent-append safety
  across two writers), extended `tests/test_tokenizer.py` (audit entries
  emitted correctly and only when `audit_log` is supplied),
  `tests/test_gateway.py` (new panel routes' contracts), `tests/test_hooks.py`
  and `tests/test_sanitize.py` (audit wiring), `tests/test_detector.py`
  (item 15's prompt content), `tests/test_decoys.py` (item 16's
  mode-routing and the new decoy).
- `.harness/handoff.md`: the audit-log-never-contains-plaintext proof, a
  manual demo-run checklist (start the gateway, open the panel, run a real
  Claude Code session against it, observe the live panes/pipeline/vault/
  metrics), and — if a live LM Studio instance is reachable this mission the
  way one was in mission-10 — a before/after `--mode llm` DNI/NIE recall
  comparison for item 15; otherwise that comparison is documented as a
  required manual follow-up instead.

## Non-goals

- Any change to `core/vault.py`'s encryption, HMAC, key derivation, table
  schema, or existing methods (`get_or_create_token`, `resolve`,
  `purge_expired`) — only one new read-only listing method is added.
- Hooks emitting their own pipeline-stage-transition SSE events — gateway
  only, per the Intake Grill. Hooks still write ordinary audit-log entries
  (the separate, pre-existing-by-design JSONL stream), which is unrelated.
- Full animation polish (moving indicators, easing, particles) for the
  pipeline-flow diagram — MVP static-block-plus-highlight only, per the
  Intake Grill's explicit scope/budget answer.
- A live spike against real traffic before building the two new SSE
  streams — built directly, proven by tests + a manual checklist, per the
  Intake Grill.
- Any new authentication/access-control layer on the gateway's HTTP
  surface — the existing `127.0.0.1`-only bind is the only protection, same
  as every other endpoint this gateway already serves.
- A JS build toolchain, bundler, or `package.json` — React + Babel Standalone
  load from a CDN into one static, unbuilt HTML file.
- Multi-session viewing in the panel — the vault/audit views default to the
  most-recently-seen session id, with an optional explicit override; this is
  a single-active-debugging-session demo tool, not a multi-tenant dashboard.
- Any change to the gateway's existing request/response tokenization,
  streaming SSE rewrite, or session-id reconciliation logic (mission-7/8/9)
  beyond adding the `audit_log`/`adapter` passthrough at its existing
  `tokenize()` call site.
- Retry/backoff, caching, or any other `core/detector.py` behavior change
  beyond item 15's prompt text edit.
- Seeding `ADDRESS` ground truth or any other manifest change beyond item
  16's one new decoy category.

## Acceptance criteria

1. `core/audit.py`'s `AuditLog.record(...)` never writes the raw `value`
   argument anywhere in the file it appends to — proven by a test that
   passes a realistic-looking PII value and asserts it does not appear as a
   substring anywhere in the written file, only its sha256 hex digest does.
2. `tokenize()`/`_tokenize_text()` write exactly one audit entry per
   substituted span when `audit_log` is supplied, with matching
   token/entity_type/detector/offset; omitting `audit_log` (every pre-mission
   call site/test) writes nothing and changes no other behavior.
3. The gateway, `PostToolUse`, and `/veil`'s `sanitize.py` all pass a shared
   `AuditLog` with their own distinct `adapter` string into their existing
   `tokenize()` calls — proven by a test per adapter that a minted token
   produces an audit entry tagged with that adapter's name.
4. `GET /veil-panel` serves a static page built with CDN-loaded React +
   Babel Standalone, no build artifacts, no new Python or JS runtime
   dependency added to `pyproject.toml`.
5. `GET /veil-panel/audit-stream` is a working SSE endpoint that emits a new
   event when a line is appended to `.veil/audit.jsonl` by any process;
   `GET /veil-panel/pipeline-stream` is a separate SSE endpoint emitting one
   event per pipeline stage for a request the gateway itself proxies, never
   persisted to disk.
6. `GET /veil-panel/vault` never returns a decrypted value; `POST
   /veil-panel/vault/reveal` returns the decrypted value for exactly one
   requested token via the existing `Vault.resolve()`, proven by a test.
7. `GET /veil-panel/metrics` returns `demo/eval.py`'s regex-mode
   recall/precision report as JSON.
8. The frontend (manually verified per `handoff.md`'s checklist, since a
   browser UI is not exercised by `pytest`) shows BRIEF §5's four panes with
   detector-colored spans, a vault inspector requiring an explicit reveal
   action, a metrics pane, and an MVP pipeline-flow diagram that highlights
   the current stage as real SSE events arrive.
9. Item 15: `_SYSTEM_PROMPT` states the DNI-vs-NIE leading-letter rule and
   the IBAN/PHONE/CARD/IP structural rules, verified by a test asserting the
   prompt text contains the specific distinguishing language; a live
   `--mode llm` before/after DNI/NIE comparison is captured in
   `handoff.md` if an LM Studio instance is reachable this mission.
10. Item 16: `evaluate_decoys()` accepts and uses `use_regex`/`use_llm`/
    `llm_client`, routing through `detect()` instead of a hardcoded
    `detect_regex()` call; `demo/seed.py` includes a new invalid-octet-IP
    decoy; both proven by test.
11. `.harness/bin/verify` exits 0.
12. `.harness/handoff.md` documents the plaintext-never-written proof, the
    manual demo-run checklist, and the item 15 live-comparison result (or
    its absence and why), plainly enough for a human to decide, without
    re-running anything, whether the mission is correct and what remains to
    be confirmed live.

## Risk

Risk class: high

Rationale: this mission touches every one of CLAUDE.md's designated
high-risk surfaces — `core/vault.py` (a new read method, however narrow),
`core/tokenizer.py` (the audit-wiring signature change threading through
every call site), and `core/detector.py` (the item 15 prompt edit, which
changes what the LLM layer is asked to do). It also adds the project's first
locally-served HTTP surface beyond the proxy itself (the `/veil-panel/*`
routes), including one endpoint (`/veil-panel/vault/reveal`) whose entire
job is to return decrypted PII on request — by design (BRIEF §5), but new
code on that path warrants full review before merge. The audit log's
cross-process JSONL append (gateway + hooks + `/veil` all appending to the
same file) is a new concurrency surface this project has not exercised
before. Scope is also unusually large for one mission (audit log + frontend
+ two drive-by fixes); BRIEF's own explicit timebox warning about the
frontend applies in full.

## Merge policy

No commit, push, or merge without explicit human review of the diff and
`.harness/handoff.md`. This mission does not merge itself.

## Tooling And Approach Assessment

1. **What project type is this?** Three mostly-independent additions bolted
   onto an existing hybrid-detector/tokenizer/gateway codebase: (a) a new
   `core/` module (`audit.py`) and its wiring through an existing call chain,
   (b) a new local HTTP surface (static file + two SSE feeds + two JSON
   endpoints) added to an existing raw `BaseHTTPRequestHandler`-based server
   that today only proxies, plus a no-build-step React frontend consuming
   it, and (c) two narrowly-scoped fixes to existing detector/eval code
   (items 15/16). None of these are new architectural shapes — `Detector`,
   `EntityType`, and the gateway's existing SSE-rewrite machinery already
   anticipate most of what is needed.
2. **What methods, tooling, or approaches does that imply?** Stdlib only for
   the new gateway routes and `audit.py` (`json`, `hashlib`, `time`/
   `datetime`, `http.server` routing already in use) — no new Python runtime
   dependency. SSE tailing of a growing file uses the same
   read-until-blocked-then-poll pattern already implicit in the gateway's
   existing streaming write helpers (`_write_chunk`), applied to a file
   handle instead of an `httpx` response iterator. The frontend uses CDN
   React + Babel Standalone (`<script type="text/babel">`) — no bundler,
   matching this project's "no runtime dependencies yet" posture and BRIEF
   §5's "no CORS to fight, one command" requirement.
3. **What risks or missing decisions does it reveal?** (a) Cross-process
   JSONL append safety: `.veil/audit.jsonl` is written by up to three
   separate OS processes (gateway, a hook invocation, `/veil`) — this needs
   an explicit, tested assumption (single `json.dumps(...) + "\n"` writes in
   append mode are effectively atomic at this line length on the platforms
   this project targets) rather than silently hoping interleaving never
   corrupts a line; call out in validation. (b) The gateway's `do_GET`/
   `do_POST` currently assume every request is a proxy pass-through — adding
   local routes needs a clear, narrow dispatch (a fixed `/veil-panel` prefix
   checked first) so it cannot accidentally intercept a real Anthropic API
   path. (c) `Vault.list_mappings()` is a new read path into vault
   internals — narrow (three non-secret columns, no ciphertext decryption)
   but still touches the project's most sensitive file; keep it read-only
   and unit-tested directly. (d) The panel's "most-recently-seen session"
   convenience needs the gateway to track one extra piece of shared state
   (guarded by the existing `_vault_lock` or a new dedicated lock) — a small,
   contained addition, not a redesign of session handling. (e) Items 15/16
   are additive/corrective to existing, already-tested code paths and do not
   change `detect()`'s or `evaluate()`'s public signatures in a
   backward-incompatible way — the main risk is scope creep into the LLM
   client itself, which is explicitly out of scope beyond the prompt text
   and the decoy-routing parameter passthrough.
4. **What should be added to validation?** All the standard gates; a
   concurrent-append test for `audit.py` (two writers appending
   interleaved, asserting every line still parses as valid JSON and none is
   corrupted/merged); endpoint-contract tests for every new gateway route
   (status codes, content-types, SSE event shape, vault list never
   containing a decrypted value, reveal returning the correct one); a prompt-
   content test for item 15; a decoy-routing test for item 16; a manual
   demo-run checklist for the parts only a browser can truly verify (visual
   rendering, span highlighting, the pipeline diagram's stage highlight).
5. **What is explicitly not needed now?** Any change to vault encryption/
   schema beyond the one new read method; a live spike; animation polish
   beyond MVP stage-highlighting; hooks-originated pipeline-stage events; a
   new auth layer on the panel; a JS bundler; multi-session panel viewing;
   `ADDRESS` manifest ground truth; any other `core/detector.py` behavior
   change beyond items 15/16 as scoped above.

## Status

implemented — awaiting human review for merge (see `.harness/handoff.md`).
Five bugs found and fixed during live testing this mission (sqlite
connection leak, favicon proxy leak, Accept-Encoding/Brotli decoding,
test-suite LLM-env-var isolation, and the LLM layer tokenizing Claude
Code's own reserved billing-header metadata). Full end-to-end live
verification completed against the real Anthropic API (both regex-only and
LLM+regex configurations) and against LM Studio (item 15's before/after).
Two open items remain for human decision, not further implementation: the
pipeline-stream SSE endpoint's raw-PII-carrying design (sign-off requested),
and whether item 16's newly-surfaced LLM-decoy-hallucination finding should
be folded into BRIEF §6 documentation now or left for a later mission.
