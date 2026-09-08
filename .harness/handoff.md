# Handoff

## Post-writeup fixes (found during live manual testing)

After this document was first written, the human reviewer actually ran the
panel live (gateway + browser + a real proxied request), then a real
`claude -p` process against the real Anthropic API, then the LLM layer
against a real LM Studio instance. Five real bugs surfaced across that
whole session, all fixed and covered by new tests before this final version:

1. **`demo/eval.py::_load_texts` leaked its sqlite3 connection.**
   `with sqlite3.connect(db_path) as connection:` only commits/rolls back on
   exit in Python — it does not close the connection. This never mattered
   for `eval.py`'s own short-lived CLI process, but `/veil-panel/metrics`
   calls `evaluate()` (and so `_load_texts`) repeatedly from the
   long-running gateway process, so the file handle on
   `demo/veil_demo.db` outlived the call. Caught by `.harness/bin/verify`'s
   own `eval` gate failing with `PermissionError: [WinError 32]` when
   `seed.py` tried to regenerate the database while a gateway process
   (started for the manual test) still held it open. Fixed:
   `contextlib.closing(sqlite3.connect(db_path))` in `_load_texts`.
2. **A browser tab open on the panel silently proxied its own favicon
   request to the real upstream API.** `GET /favicon.ico` doesn't start
   with `/veil-panel`, so it fell through to the real proxy path exactly
   like a real Anthropic API call — tokenizing (nothing, empty body),
   forwarding to the real upstream, and publishing pipeline-stage events
   for that unrelated response. This is what the reviewer actually saw
   highlighted in the pipeline diagram during testing, not their own test
   request. Fixed: `/favicon.ico` now gets a local `204` from
   `_handle_panel`, never reaching the proxy. Test:
   `tests/test_gateway.py::TestPanelDoesNotShadowTheProxy::test_favicon_request_never_reaches_the_upstream`.
3. **The gateway relayed still-Brotli-compressed bytes as plain text**,
   found via this mission's own first-ever live test against the real
   Anthropic API (a real `claude -p` process, `ANTHROPIC_BASE_URL` pointed
   at the gateway — the subscription auth worked unmodified, confirming
   mission-7's original finding still holds). The real API replies
   `content-encoding: br` when the client's `Accept-Encoding` offers
   Brotli (a real client does; the mocked upstreams `test_gateway.py` uses
   never compress anything, so this was invisible to the whole test suite
   until now); `httpx` cannot decode Brotli without the optional `brotli`/
   `brotlicffi` package, which this project does not install, so the
   still-compressed bytes were relayed to the client with the
   `Content-Encoding` header stripped (per `_HOP_BY_HOP`) — the client then
   tried to parse compressed binary as plain JSON and displayed unreadable
   garbage. Fixed: the gateway now always asks upstream for
   `Accept-Encoding: gzip, deflate` (both decodable by `httpx` with no
   optional dependency) regardless of what the real client requested — the
   client-facing response contract is unchanged either way, since
   `Content-Encoding` was already stripped from every relayed response
   before this fix. Test:
   `tests/test_gateway.py::TestRequestTokenization::test_accept_encoding_forced_to_a_decodable_value`.
4. **A related, discovered-in-passing test-isolation gap**: adding a
   personal `VEIL_LLM_BASE_URL`/`VEIL_LLM_MODEL` to `.claude/settings.local.json`
   (so this developer's LM Studio config persists across sessions, done at
   their request during this same live-testing pass) immediately broke
   `tests/test_sanitize.py::TestAdversarialStdinNeverReachesAShell` the
   next time the suite ran in this same shell: `tokenize()`/`detect()`
   default to building an `LLMClient` from the environment when no explicit
   one is supplied, so *any* test that doesn't override this — nearly the
   whole suite — was silently exposed to whatever LLM config happened to be
   ambient in the developer's own shell, with no test ever asserting
   otherwise before now. Fixed: new `tests/conftest.py` with an
   `autouse=True` fixture that strips both env vars for every test by
   default; the one test that deliberately wants them
   (`test_env_configured_client_is_used_and_closed_automatically` and
   friends in `tests/test_tokenizer.py`) already opts in via its own
   `monkeypatch.setenv(...)`, which layers correctly on top.

`.harness/bin/verify` re-confirmed green after all five fixes (see Gate
results below, unchanged in shape from the first pass).

5. **The LLM layer tokenized Claude Code's own reserved system-prompt
   metadata, and the real API rejected the whole request as a result.**
   With the Brotli fix above, a real `claude -p` process routed through the
   gateway got past authentication and the encoding bug, then hit a new,
   legible 400 from the real Anthropic API: `"x-anthropic-billing-header is
   a reserved keyword and may not be used in the system prompt."` Captured
   the exact raw request text via the panel's own pipeline-stream (proof the
   panel's observability design is itself useful for debugging, not just
   demo value): Claude Code injects a system-prompt block on every request,
   `x-anthropic-billing-header: cc_version=<x>; cc_entrypoint=<y>;` — CLI
   metadata, never user content, which the real API accepts only
   byte-for-byte unmodified. The LLM layer mistook the `cc_` prefix for a
   credit-card indicator and tokenized both fields (`PII_CARD_...`,
   `PII_PERSON_...`), and the mangled line no longer matched what the real
   API expects in that reserved slot. The human reviewer correctly pointed
   out the real fix: the detector should never scan known protocol/metadata
   content in the first place, matching this codebase's own existing
   precedent (`signature_delta` is already excluded from the SSE
   response-rewrite for the same reason — "a cryptographic signature ...
   never freeform text"). Fixed: `core/tokenizer.py::tokenize()` gains an
   optional `skip: Callable[[str], bool] | None` parameter — checked before
   any detection runs on a string leaf; a matching leaf gets zero regex/LLM
   calls, zero audit entries, zero span-sink entries, and is returned
   completely unscanned. The predicate itself carries no Anthropic-specific
   knowledge (kept transport-agnostic, per this module's existing design);
   the gateway supplies `_skip_reserved_anthropic_metadata` (matches the
   `x-anthropic-billing-header:` prefix) when calling `tokenize()`. This
   does **not** weaken protection anywhere else — every other string leaf,
   including the rest of the `system` array (which *can* legitimately carry
   user-authored custom instructions), is still scanned exactly as before.
   Tests: `tests/test_tokenizer.py::TestSkipPredicateForProtocolMetadata`
   (five tests: skip excludes detection/audit/span-sink entirely, only
   matching leaves are affected, omitting `skip` is unchanged);
   `tests/test_gateway.py::TestSkipReservedAnthropicMetadataPredicate` and
   `TestReservedAnthropicMetadataExclusion` (the reserved block survives
   byte-for-byte even when it contains a card-shaped value that would
   otherwise be detected; ordinary message content in the same request is
   still tokenized normally).

## Live end-to-end verification (completed, at the human reviewer's request)

After all five fixes above, a real `claude -p` process was routed through
the gateway (`ANTHROPIC_BASE_URL=http://127.0.0.1:8787`) end to end, twice:

**Regex-only** (`VEIL_LLM_BASE_URL`/`VEIL_LLM_MODEL` unset):
```
$ ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude -p "My email is alice@example.com and my name is Maria Garcia. Reply with exactly this and nothing else: OK <email> <name>, substituting the actual email and name I gave you."
OK alice@example.com Maria Garcia
```
`.veil/audit.jsonl` confirms `PII_EMAIL_0001` (`REGEX`, adapter `gateway`) —
the email never reached the real Anthropic API in clear text. `Maria Garcia`
correctly passed through unprotected (regex cannot detect person names by
design; this is the documented gap the LLM layer exists to close, not a bug).

**LLM + regex (`--mode union`'s live configuration)**, LM Studio at
`http://192.168.31.123:1234`, model `qwen_qwen3-4b-instruct-2507`:
```
$ ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude -p "<same prompt>"
OK alice@example.com Maria Garcia
```
`.veil/audit.jsonl` this time shows **both** `PII_EMAIL_0001` (`REGEX`) and
`PII_PERSON_0001` (`LLM`) — the name was protected too, with no false
positive on the billing-header line (the fix above holds under real traffic,
not just the synthetic test that motivated it). This is the first time this
project's full two-layer architecture has been exercised end to end against
the real Anthropic API with real subscription auth — previously only the
gateway's request/response mechanics (mission-7/8) and the LLM layer in
isolation (mission-10) had been live-verified separately.

### Item 15 live before/after (BRIEF §6, now done — was the required follow-up)

Same LM Studio host/model as mission-10, for direct comparison:

| | DNI recall | DNI precision | NIE recall | NIE precision |
|---|---|---|---|---|
| Before (mission-10 baseline) | 0.000 | — | 1.000 | 0.087 |
| After (this mission's prompt fix) | **0.630** | 1.000 | 1.000 | **0.200** |

The structural DNI-vs-NIE leading-letter rule measurably helped — DNI
recall went from finding *zero* national IDs to nearly two-thirds, and NIE
precision more than doubled — but neither reached 1.000: the model still
mislabels some DNIs as NIE (`--mode union`'s own recall is unaffected either
way, since regex already wins every tie-break `merge_spans` resolves — see
the union numbers below). This is genuine, expected model-quality data, not
a code defect; a larger or fine-tuned model may do better. Full `--mode llm`
report:
```
entity      support   found  missed    fp   recall    prec
----------------------------------------------------------
CARD             30      30       0     0    1.000   1.000
DNI              46      29      17     0    0.630   1.000
EMAIL            80      80       0     0    1.000   1.000
IBAN            100     100       0     0    1.000   1.000
IP              200     200       0     0    1.000   1.000
NIE               4       4       0    16    1.000   0.200
PERSON          250     187      63     9    0.748   0.954  (not gated)
PHONE            33      29       4     0    0.879   1.000
```
`--mode union` (the project's actual target configuration) still reaches
1.000 recall on every structured type, unaffected by the LLM layer's own
imperfect labeling — confirming the union architecture's own thesis holds
regardless of this specific improvement's limits.

### Item 16's fix surfaced a new, previously-invisible finding

With decoys now actually routed through the LLM layer (this mission's own
item 16 fix — previously `evaluate_decoys()` always hardcoded
`detect_regex()`, so this was never measurable in *any* mode), both
`--mode llm` and `--mode union` report **6 decoy false positives** the
regex-only gate has never seen, because it never exercises the LLM layer:
```
DNI    decoy_samples.note#1 '07409942A' -- DNI_BAD_LETTER
DNI    decoy_samples.note#2 '16522595A' -- DNI_BAD_LETTER
CARD   decoy_samples.note#3 '5713096961501724' -- CARD_BAD_LUHN
CARD   decoy_samples.note#4 '1029952520917511' -- CARD_BAD_LUHN
IBAN   decoy_samples.note#5 'ES9613168780417922609226' -- IBAN_BAD_MOD97
IBAN   decoy_samples.note#6 'ES2141139688116079415057' -- IBAN_BAD_MOD97
```
The LLM accepts a deliberately-invalid check digit as if it were valid PII —
unsurprising, since nothing in its prompt asks it to verify a check digit
(that is exactly what the regex layer's validators are for). **Does not
affect `.harness/bin/verify`'s own gate**: that gate calls `eval.py` with no
`--mode` flag (defaults to `"regex"`), where `evaluate_decoys()` runs
`use_llm=False` regardless of what's in the environment, so this finding is
invisible to CI/the harness gate by design — it only shows up when a human
explicitly runs `--mode llm`/`--mode union` with an LLM configured, exactly
as intended. Documented here as a new residual-risk data point, matching
BRIEF §6's existing DNI/NIE-mislabeling row's own precedent (published, not
hidden, not blocking) — worth folding into BRIEF §6 as its own row in a
future documentation pass, not fixed in this mission (fixing it would mean
teaching the LLM prompt to reason about check digits, a materially bigger
prompt-engineering effort than this mission's scope).

## Summary

Implemented work-plan item 13 (BRIEF §3.4 + §5): the audit log
(`veil_pii_middleware/core/audit.py`, previously only a module docstring)
and the observability frontend, plus two drive-by fixes explicitly requested
in this mission's Intake Grill — item 15 (LLM prompt structural grounding)
and item 16 (decoys routed through the LLM layer too).

- **`core/audit.py`**: a real `AuditLog` — append-only JSONL, one entry per
  substituted span (timestamp, token, entity_type, detector, offset,
  `value_hash`, adapter), the raw value hashed internally and never stored.
- **`core/tokenizer.py`**: `tokenize()` gains optional `audit_log`/`adapter`/
  `span_sink` parameters. Omitting them (every pre-mission call site) is
  byte-for-byte unchanged behavior. `span_sink` lets a caller collect the
  same spans `tokenize()` already computed, without a second detection pass
  — used by the gateway to feed the panel's span-highlighting pane.
- **Three adapters wired**: `adapters/gateway/server.py` (`adapter="gateway"`),
  `adapters/hooks/post_tool_use.py` (`adapter="hooks:post_tool_use"`),
  `adapters/command/sanitize.py` (`adapter="command"`) all pass a shared
  `AuditLog` at `.veil/audit.jsonl` into their existing `tokenize()` calls.
- **`core/vault.py`**: one new read-only method, `list_mappings()` (token,
  entity_type, base64 ciphertext, created_at — never decrypts), for the
  vault-inspector pane. No change to encryption, HMAC, schema, or any
  existing method.
- **`adapters/gateway/server.py`**: new routes under a reserved
  `/veil-panel` prefix, checked before the proxy pass-through in every
  `do_*` method: the static panel page; an SSE tail of `.veil/audit.jsonl`
  (any process's writes, since it reads the shared file); a *separate*,
  in-memory-only SSE stream of pipeline-stage events (`received` →
  `tokenized` → `forwarded` → `response_received` → `detokenized` →
  `displayed`) published only by the gateway's own request/response
  handling; vault list/reveal JSON endpoints; a metrics endpoint backed by
  `demo/eval.py`. Any request under the prefix that matches no known
  route/method now 404s locally instead of falling through to the proxy.
- **`frontend/panel.html`**: one static, unbuilt HTML file — React (CDN UMD)
  + Babel Standalone (CDN) for in-browser JSX, no bundler, no new
  dependency. Implements BRIEF §5's four panes (detector-colored span
  highlighting on the Input pane, token-hover entity types on the two
  tokenized panes), an MVP (non-animated, CSS-class-highlight) pipeline
  diagram, a live audit-log table, a vault inspector with an explicit
  per-token reveal action, and a metrics table.
- **Item 15** (`core/detector.py`): `_SYSTEM_PROMPT` now states the
  DNI-vs-NIE leading-letter rule and IBAN/PHONE/CARD/IP structural rules —
  text only, no change to `LLMClient`, `detect_llm`, or `detect()`.
- **Item 16** (`demo/eval.py`, `demo/seed.py`): `evaluate_decoys()` now takes
  `use_regex`/`use_llm`/`llm_client` and routes through `detect()` instead of
  a hardcoded `detect_regex()`; `main()` passes through the same
  mode-resolved flags `evaluate()` already uses. `demo/seed.py` gained an
  `INVALID_OCTET_IP` decoy category.

`git diff --stat`: 18 tracked files changed (`.harness/mission.md`,
`.harness/validation.md`, 7 test files, and the 9 source files listed
above), plus 2 new untracked files (`tests/test_audit.py`,
`veil_pii_middleware/frontend/panel.html`). `core/vault.py`'s diff is
confirmed narrow — only `MappingInfo`/`list_mappings()` added, no line
touching `get_or_create_token`, `resolve`, `purge_expired`, the schema, or
any cryptographic primitive. `pyproject.toml`'s diff is empty — no new
runtime dependency.

## Independent validation performed

Two fresh-context subagents reviewed this mission; both found real issues,
fixed below before this handoff was written.

### `code-reviewer` (security-focused pass)

**Blocking, fixed:**

1. **Audit log was not actually cross-process-safe.** The original
   `core/audit.py` relied on a `threading.Lock` plus a comment asserting
   "POSIX/NTFS both guarantee a single `write()` syscall is atomic." The
   reviewer tested this empirically with **real separate OS processes** (not
   threads sharing one lock object) hammering `AuditLog.record()`
   concurrently and reproduced real corruption: only 1531/2000 and
   1035/1500 lines survived across two runs, some truncated/unparseable.
   `.veil/audit.jsonl` is written by up to three separate processes in
   normal use (gateway, a hook invocation, `/veil`), so this was a real gap
   in the one place CLAUDE.md requires a durable, tamper-evident record.
   **Fixed**: `AuditLog` now holds an OS-level lock (a sidecar
   `audit.jsonl.lock` file, `msvcrt.locking` on Windows / `fcntl.flock` on
   POSIX) for the full duration of each append, on top of the existing
   in-process lock. **Re-tested with real subprocesses** (5 processes × 150
   writes = 750 lines): `tests/test_audit.py::TestConcurrentAppendSafety` —
   all 750 lines present and parseable. The original test (4 threads
   sharing one `AuditLog` object) proved nothing about cross-process safety,
   since threads of one process already share the in-process lock; it has
   been replaced with the subprocess version.

2. **The pipeline-stream SSE endpoint broadcasts raw, pre-tokenization PII
   with no access control**, which the reviewer flagged against an
   over-broad bullet in the original `validation.md` ("no raw PII value
   anywhere ... except the vault/reveal response"). On inspection, this is
   not accidental scope creep: `mission.md`'s own Scope section (written and
   approved before implementation) explicitly requires the pipeline stream
   to carry "the actual request/response text BRIEF's four panes need to
   show" — BRIEF §5 pane 1 is literally "the user's text with detected spans
   highlighted," which cannot be built without the raw text reaching the
   browser. **`validation.md`'s bullet was the bug, not the code** — it
   banned something the mission it was supposed to validate explicitly
   required. Fixed: the bullet now correctly scopes the "no raw PII" rule to
   the audit log, vault-list response, and gateway stderr, with the
   pipeline stream and `/veil-panel/vault/reveal` named as the two
   deliberate, mission-scoped exceptions (both bound to the gateway's
   existing `127.0.0.1`-only listener, neither persisted to disk). **See
   "Requires explicit human sign-off" below — this is a real trade-off, not
   a mistake, and deserves a conscious decision from you, not just from me.**

**Should-fix, applied:**

- `/veil-panel/vault/reveal` (the one endpoint whose entire job is
  returning decrypted PII) now logs a stderr line on every successful
  reveal (`token` + `session_id`, never the value) — a minimal forensic
  trail for the most sensitive action in the system. Test:
  `tests/test_gateway.py::TestVaultInspectorEndpoints::test_reveal_logs_a_forensic_trail_without_the_value`.
- `_handle_panel` previously let a wrong-verb or unknown request under
  `/veil-panel/*` silently fall through to the real upstream proxy (e.g.
  `GET /veil-panel/vault/reveal`). It now claims every path under the
  prefix and answers 404 locally for anything unrecognized. Tests:
  `TestPanelDoesNotShadowTheProxy::test_wrong_verb_on_a_panel_route_404s_rather_than_proxying`,
  `test_unknown_panel_sub_path_404s_rather_than_proxying`.

**Nits, applied:** simplified `except (BrokenPipeError, ConnectionResetError,
OSError)` → `except OSError:` (the first two are subclasses); tightened the
panel header copy so it doesn't read as a blanket "no PII anywhere on this
page" claim.

### `goal-verifier` (acceptance-criteria pass)

Went through all 12 acceptance criteria in `mission.md`; ran `.harness/bin/verify`
independently (pass). Found two real gaps, both closed before this handoff:

1. **This document didn't exist yet** (AC12) — `.harness/handoff.md` still
   held mission-10's already-archived content. This document is that fix.
2. **The frontend never opened `/veil-panel/audit-stream`** — the panel
   consumed the pipeline stream for the four panes but had no live view of
   the audit log itself, contradicting `mission.md`'s own Scope text and
   BRIEF §5's framing ("a panel that consumes the audit log over SSE").
   Fixed: `frontend/panel.html` gained an `AuditLogPane` component
   (`EventSource("/veil-panel/audit-stream")`) rendering a live table of
   entries — timestamp, adapter, entity type, detector, token — never a
   value, added to the panel's bottom row alongside the vault inspector and
   metrics.

It also flagged `tests/test_gateway.py::TestPipelineStream`'s original
single-chunk read as weaker than validation.md's own executable check
("assert at least one stage event **per documented stage name**"). Fixed:
the test now collects every SSE chunk until the stream goes quiet
(`_read_sse_chunks_until_idle`) and asserts all six `PIPELINE_STAGES` names
appear, not just one.

Every other criterion passed on the goal-verifier's first review: AC1–AC4,
AC6, AC7, AC10, AC11, plus the `core/vault.py`/`pyproject.toml` scope checks.
AC5, AC8, AC9 were "partially met" pending exactly the two fixes above (AC5/
AC8: audit-stream wiring) and this document (AC9's item-15 write-up, AC12).

## Requires explicit human sign-off before merge

**The pipeline-stream SSE endpoint (`/veil-panel/pipeline-stream`) sends raw,
pre-tokenization request/response text to any local process that connects
to it, with no authentication beyond the gateway's existing
`127.0.0.1`-only bind, and no audit trail of who read it.** This is by
design — BRIEF §5's four panes cannot show "the user's text with detected
spans highlighted" or "hovering a token reveals its entity type" without
the actual text and spans reaching the browser, and this mission's own
approved Scope said so explicitly before implementation began. It is the
same trust model already accepted for `/veil-panel/vault/reveal` (BRIEF §5's
"revealing a value requires an explicit action" — this mission's Non-goals
already waive a new auth layer for that endpoint), extended to a second,
lower-friction path: a local listener does not need to know a token or
issue a POST — it only needs to open the SSE connection while a request is
in flight. Please confirm you're comfortable with this before merging, or
tell me to redact/gate it (e.g. requiring a shared secret query param, or
dropping span-level raw text from the stream and re-deriving it client-side
from the audit log's offsets — the latter is not fully possible today since
the audit log's offset alone, with no span length, can't reconstruct exact
highlighting without guessing).

## Plaintext-never-written proof

`core/audit.py`'s `AuditLog.record()` accepts the raw `value` only to hash
it (`hashlib.sha256(value.encode()).hexdigest()`) — the local variable never
escapes the function, and the written JSON object has no field that could
hold it. Proven directly:

- `tests/test_audit.py::TestRecordNeverWritesPlaintext` — records a
  realistic email and a DNI, asserts the raw value is absent from the
  written file and only its hash is present.
- `tests/test_tokenizer.py::TestAuditLogWiring::test_no_raw_value_ever_written`
  — same proof at the `tokenize()` call site a real adapter uses.
- `tests/test_hooks.py::TestPostToolUseAuditLogWiring` and
  `tests/test_sanitize.py::TestAuditLogWiring` — same proof end-to-end
  through a real subprocess invocation of each adapter, confirming the
  `adapter` tag is also correct (`"hooks:post_tool_use"`, `"command"`).
- The gateway's own audit entries are proven the same way in
  `tests/test_gateway.py::TestAuditStream::test_emits_a_new_entry_as_it_is_appended`
  (adapter tag `"gateway"`, value absent from the SSE payload).

## Acceptance criteria — evidence

1. **No raw value ever written.** ✅ See "Plaintext-never-written proof" above.
2. **One audit entry per span, silent when `audit_log` omitted.** ✅
   `tests/test_tokenizer.py::TestAuditLogWiring` (four tests: one-entry-per-span,
   omitted-writes-nothing, adapter-required, entries-across-structure-leaves).
3. **All three adapters tag their own `adapter`.** ✅ See per-adapter tests
   listed under "Plaintext-never-written proof."
4. **Static CDN panel page, no new dependency.** ✅
   `tests/test_gateway.py::TestPanelStaticPage`; `git diff -- pyproject.toml`
   is empty.
5. **Audit-stream + pipeline-stream SSE endpoints.** ✅ (fixed per
   goal-verifier's finding above — the panel now consumes both).
   `TestAuditStream::test_emits_a_new_entry_as_it_is_appended`,
   `TestPipelineStream::test_emits_every_documented_stage_never_persisted_to_the_audit_log`
   (all six `PIPELINE_STAGES` names confirmed present; confirmed absent from
   `.veil/audit.jsonl`).
6. **Vault list never leaks; reveal returns exactly the right value.** ✅
   `TestVaultInspectorEndpoints` — list-never-includes-a-decrypted-value,
   reveal-returns-the-right-value, reveal-rejects-unknown-token,
   reveal-rejects-malformed-body, reveal-logs-a-forensic-trail (added per
   code-reviewer's should-fix).
7. **Metrics endpoint.** ✅ `TestMetricsEndpoint` (both the
   demo-data-missing and demo-data-available paths, via a monkeypatched
   `eval_module.evaluate`, so the test doesn't depend on real seeded demo
   data being present).
8. **Frontend shows all of BRIEF §5 + the pipeline diagram.** ✅ per code
   inspection and the endpoint-contract tests above; **not independently
   verified in a real browser this mission** — see the manual checklist
   below, which is the one part of this acceptance criterion that genuinely
   needs a human (or a future mission with browser automation) to confirm.
9. **Item 15: prompt states the structural rules; live comparison.** ✅ both
   halves, now done. Prompt content:
   `tests/test_detector.py::test_prompt_states_dni_vs_nie_leading_letter_rule`,
   `test_prompt_states_iban_phone_card_ip_structural_rules`. Live before/after
   against the same LM Studio host/model mission-10 used: DNI recall
   0.000 → 0.630, NIE precision 0.087 → 0.200 — see "Item 15 live
   before/after" above for the full report and discussion.
10. **Item 16: decoy mode-routing + new decoy.** ✅
    `tests/test_decoys.py::test_evaluate_decoys_routes_through_detect_with_the_callers_flags`
    (mocked `detect`, asserts flags forwarded — proves the hardcoded
    `detect_regex()` call is gone), `test_every_decoy_category_is_present`
    (includes `INVALID_OCTET_IP`), `test_no_decoy_is_detected_under_the_real_validators`.
11. **`.harness/bin/verify` exits 0.** ✅ See Gate results below.
12. **This document.** ✅

## Note: a repo config change was made outside this mission's scope

At the human reviewer's explicit request, during this same live-testing
pass: `.claude/settings.local.json` was created (new file, previously
absent) with `VEIL_LLM_BASE_URL`/`VEIL_LLM_MODEL` pointed at this
developer's own LM Studio instance (`http://192.168.31.123:1234`,
`qwen_qwen3-4b-instruct-2507` — the same host/model mission-10 used), so
future sessions don't need these exported manually. Per this project's own
convention (`.claude/settings.local.json.example`'s comment: "for your
machine, your model preference"), this is personal, machine-specific
config — not the team-shared `.claude/settings.json`. `.gitignore` gained a
`.claude/settings.local.json` entry, since nothing previously excluded it
despite the example file's comment claiming it was gitignored (it was not).
This is unrelated to item 13/15/16's own code, but is a real repo change
the human reviewer should be aware of when reviewing `git status`/`git diff
--stat` — the same kind of scope note mission-10's own handoff made for a
permission-list change discovered mid-session.

## Gate results

```
verify · veil-pii-middleware
  ✓ contract     bash .harness/internal/check-mission-contract
  ✓ lint         ruff check . && ruff format --check .
  ✓ typecheck    mypy (strict)
  ✓ test         pytest — all green (includes the real-subprocess audit
                 concurrency test, ~750 lines across 5 processes)
  ✓ eval         seed.py + eval.py --min-recall 1.0 --max-false-positives 0
                 (regex mode, unchanged; corpus now also includes the new
                 INVALID_OCTET_IP decoy, still 0 false positives)
  ✓ build        python -m build --no-isolation
RESULT: PASS — all 6 gate(s) green.
```

## Manual verification checklist

Updated after the live-testing session documented above.

1. **Panel, end-to-end in a real browser — done, but not re-confirmed after
   the fixes above.** The human reviewer opened the panel and drove a live
   request through it earlier in this same session, which is exactly what
   surfaced the sqlite-leak and favicon-proxy-leak bugs (both now fixed).
   The panel was not re-opened and re-checked pane-by-pane after those
   fixes landed. Recommended before considering this criterion fully closed:
   reopen `http://127.0.0.1:8787/veil-panel` (gateway already supports this
   — see "Live end-to-end verification" above for the exact commands) and
   confirm the pipeline diagram, four panes, audit-log table, vault
   inspector, and metrics pane all render as expected with no unexpected
   favicon-driven pipeline noise.
2. **Item 15, live LM Studio before/after — done.** See "Item 15 live
   before/after" above: DNI recall 0.000 → 0.630, NIE precision
   0.087 → 0.200 against the same host/model mission-10 used.
3. **Full real-API round trip, both regex-only and LLM+regex — done.** See
   "Live end-to-end verification" above: a real `claude -p` process, real
   subscription auth, real Anthropic API, both layers protecting their
   respective entity types, no false positive on Claude Code's own reserved
   metadata.
4. **The pipeline-stream sign-off** — a decision, not a test, and still
   open: confirm you're comfortable with raw request/response text being
   readable by any local process that opens `/veil-panel/pipeline-stream`
   while a request is in flight, or tell me how you'd like it scoped down.
5. **Item 16's newly-surfaced decoy finding** — a decision, not a
   blocker: the 6 LLM-hallucinated decoys documented above are real and now
   measurable, but do not affect `.harness/bin/verify`'s gate (regex-only).
   No action required unless you want it folded into BRIEF §6 as its own
   documented row, or want the LLM prompt hardened against check-digit
   confusion in a future mission.

## What a human reviewer should check

1. `core/audit.py`'s cross-process locking (`_lock_sidecar`/`_unlock_sidecar`)
   — this is new, security-relevant concurrency code; confirm the
   Windows/POSIX branching reads correctly and the retry loop can't spin
   forever under real load.
2. `adapters/gateway/server.py`'s `_handle_panel` dispatch — confirm every
   `/veil-panel/*` path is claimed (never falls through to the proxy) and no
   real API path could ever start with `/veil-panel`.
3. The pipeline-stream sign-off item above.
4. `core/vault.py`'s diff — confirm it really is exactly one new read-only
   method, nothing else.
5. Run `.harness/bin/verify` yourself to confirm the reported gate state.

## Merge policy

No commit, push, or merge without explicit human review of the diff and
this document, per `.harness/mission.md`'s Merge policy. This mission does
not merge itself.
