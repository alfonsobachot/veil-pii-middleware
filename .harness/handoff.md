# Handoff

## Summary

Implemented the probabilistic (LLM) detection layer in
`veil_pii_middleware/core/detector.py` (BRIEF §3.1, work-plan item 11): an
`LLMClient` speaking the OpenAI-compatible `/v1/chat/completions` schema
(never any Ollama-native-only API), a `detect_llm()` function that validates
and offsets the model's response into the source text, a content-block
cache keyed by `sha256(client_identity | text)`, and a `detect()` union
entry point that fails closed to the regex layer on any LLM-side failure.
Wired into `veil_pii_middleware/core/tokenizer.py`'s `_tokenize_text()` (gated
by whether `VEIL_LLM_BASE_URL`/`VEIL_LLM_MODEL` are set) and into
`veil_pii_middleware/demo/eval.py` as `--mode regex|llm|union`.

Two new `EntityType` members: `PERSON`, `ADDRESS`. `STRUCTURED_TYPES` is now
an explicit frozenset of the original seven (no longer `frozenset(EntityType)`),
so the recall gate stays bound to what the regex layer can prove.

No live LM Studio instance was reachable during the initial implementation
(per the Intake Grill), so all automated tests run against a real local HTTP
server standing in for LM Studio (no `httpx` transport mocking — matches
`test_gateway.py`'s own testing style). **After implementation, the user
configured a real LM Studio instance on their LAN (`192.168.31.123:1234`,
model `qwen_qwen3-4b-instruct-2507`) and this code was live-verified against
it in this same session — see "Live LM Studio verification (completed)"
below.** That verification found and fixed one real incompatibility
(`response_format`); everything else behaved exactly as designed.

Files changed (`git diff --stat`): `.harness/mission.md`, `.harness/validation.md`,
`tests/test_detector.py`, `tests/test_tokenizer.py`,
`veil_pii_middleware/core/detector.py`, `veil_pii_middleware/core/tokenizer.py`,
`veil_pii_middleware/demo/eval.py`. `core/vault.py`, every adapter
(`adapters/hooks/*`, `adapters/gateway/server.py`, `adapters/command/*`) and
`demo/seed.py` are untouched, confirmed by `git diff --stat` showing no lines
for any of them.

## Independent validation performed

Two fresh-context subagents reviewed this mission before this handoff was
written; their findings drove real changes, not just commentary:

- **`code-reviewer`** (security-focused pass) found two blocking issues,
  both fixed:
  1. **PII could reach stderr.** `LLMDetectionError` messages originally
     interpolated the raw malformed span entry (`f"...: {item!r}"`) — if a
     model returns a flat list of strings instead of `{"value",
     "entity_type"}` objects (a realistic instruction-following failure),
     that string is the exact PII value the system exists to protect, and
     `detect()`'s fail-closed handler prints `str(exc)` to stderr. Fixed:
     every `LLMDetectionError` message now states only the *shape* of the
     problem (`type(...).__name__`), never field content. Regression tests:
     `test_flat_string_span_entry_raises_without_leaking_its_content`,
     `test_bad_value_field_raises_without_leaking_other_fields`
     (`tests/test_detector.py`).
  2. **A real span could be silently dropped.** `_locate_next`'s
     "already-claimed offset" tracking was a single set shared across every
     value in one LLM response. Two *different* values that happen to start
     at the same offset (a plausible model output for overlapping
     `PERSON`/`ADDRESS` granularities, e.g. `"Calle Mayor"` and `"Calle
     Mayor 5"`) would have the second one silently vanish if no other
     occurrence of its own text existed elsewhere. Fixed: the claimed-start
     set is now scoped per distinct value string, so unrelated values never
     block each other; `merge_spans()` (unchanged) is what resolves the
     resulting overlap. Regression test:
     `test_different_values_sharing_a_start_offset_are_both_kept`.

  Two should-fix items from that review were also applied (both contained
  to files already in scope):
  - `demo/eval.py --mode llm|union` no longer swallows a genuine regex
    decoy false-positive regression under its "report-only" exit-code
    framing — the decoy gate (which always scores the regex layer,
    regardless of `--mode`) is now checked unconditionally; only the
    *recall* gate stays scoped to `--mode regex`.
  - The content-block cache key now folds in `LLMClient.identity`
    (`base_url|model`), so two clients pointed at different hosts/models
    never share a stale cached result for the same text.

  Two items were **not** fixed, and are documented here as known
  limitations instead, because fixing them would mean editing files outside
  this mission's declared scope (`adapters/gateway/server.py`):
  - The gateway's `_vault_lock` (originally scoped to guard `Vault`'s
    non-transactional counter read-then-insert) now also serializes behind
    whichever request is waiting on a real LLM network round-trip (up to
    10s) once `VEIL_LLM_BASE_URL` is configured — every concurrent gateway
    request effectively queues behind the slowest LLM call in flight,
    regardless of session. **Before enabling the LLM layer on a live
    gateway, this lock's scope should be revisited** (e.g. per-session
    locking, or moving the LLM call outside the lock).
  - Every adapter call site (`gateway/server.py`, `hooks/post_tool_use.py`,
    `command/sanitize.py`) calls `tokenize()` with no explicit `llm_client`,
    so a fresh `httpx.Client`/TCP connection to the LAN LM Studio host is
    opened and closed on every single `tokenize()` call rather than reused
    across a session/process. Not a leak (cleanup is correct), but avoidable
    latency per request. The content-block cache still works regardless
    (it's keyed by content hash, not by client instance), so this is a
    performance note, not a correctness gap.

- **`goal-verifier`** ran a criterion-by-criterion check against
  `.harness/mission.md`/`.harness/validation.md`, re-running
  `.harness/bin/verify` and the test suite independently. It found two real
  gaps, both closed before this handoff:
  1. The validation contract's "Fail-closed: non-2xx (AC3)" row had no test
     at the `detect()`/`tokenize()` level (only `detect_llm()` itself was
     tested against a 500). Added
     `TestDetectUnion::test_fail_closed_on_non_2xx_still_returns_regex_spans`
     (`tests/test_detector.py`) and
     `TestLLMLayerWiring::test_llm_non_2xx_response_still_tokenizes_structured_entities`
     (`tests/test_tokenizer.py`, at the `tokenize()` level a real adapter
     actually calls).
  2. This file (`.harness/handoff.md`) was still mission-9's write-up
     (gateway/hooks `session_id` reconciliation) — never updated for this
     mission. This document is that fix.

  Every other criterion the goal-verifier checked passed on first review:
  AC1, AC2, AC4, AC5, AC6, AC7, AC8 (see the criterion table below, which
  reflects the state *after* both rounds of fixes above).

## Live LM Studio verification (completed)

After the above, the user set up a real LM Studio instance on their LAN
(`http://192.168.31.123:1234`, model `qwen_qwen3-4b-instruct-2507`, a
llama.cpp-backed instruct model) and asked for the manual checklist to be
run live rather than left as a TODO. Findings:

1. **One real bug found and fixed.** `LLMClient.complete_spans` sent
   `"response_format": {"type": "json_object"}`. This LM Studio instance's
   backend rejects that with a 400: `'response_format.type' must be
   'json_schema' or 'text'`. Confirmed live:
   - The 400 correctly triggers `response.raise_for_status()` →
     `httpx.HTTPStatusError`, which `detect()`'s fail-closed handling caught
     exactly as designed — no crash, silent fallback to regex spans. So even
     the *broken* configuration never violated the fail-closed contract; it
     just meant the LLM layer contributed nothing until fixed.
   - With `response_format` removed entirely (relying only on the system
     prompt's own JSON-only instruction), the exact same request returns
     precisely the expected shape: `choices[0].message.content` as a plain
     JSON *string* — `LLMClient.complete_spans`'s core parsing assumption
     was otherwise correct on the first try.
   - Fixed: `response_format` removed from the request
     (`veil_pii_middleware/core/detector.py::LLMClient.complete_spans`).
     Regression test added:
     `tests/test_detector.py::TestDetectLLM::test_request_omits_response_format`.
2. **End-to-end real-model results, full demo corpus (`demo/eval.py`,
   after regenerating the corpus and pointing `VEIL_LLM_BASE_URL`/
   `VEIL_LLM_MODEL` at the real instance):**

   `--mode llm` (LLM alone):
   ```
   entity      support   found  missed    fp   recall    prec
   CARD             30      30       0    10    1.000   0.750
   DNI              46       0      46     0    0.000   1.000
   EMAIL            80      80       0     0    1.000   1.000
   IBAN            100      86      14     0    0.860   1.000
   IP              200     200       0     0    1.000   1.000
   NIE               4       4       0    42    1.000   0.087
   PERSON          250     244       6     0    0.976   1.000  (not gated)
   PHONE            33      29       4     0    0.879   1.000
   detector LLM    found 673, false positives 52
   decoys: 0 false positive(s) (max allowed 0) -- PASS
   ```
   The model confused DNI/NIE labeling on many national IDs (0% DNI recall,
   but NIE precision only 0.087 — it's mislabeling DNIs as NIE, an
   entity-type classification miss, not a missed span) and missed some
   IBAN/PHONE instances. This is genuine model-quality data, exactly what
   the eval framework exists to surface — not a code defect.

   `--mode union` (LLM + regex, the project's actual target configuration):
   ```
   entity      support   found  missed    fp   recall    prec
   CARD             30      30       0     0    1.000   1.000
   DNI              46      46       0     0    1.000   1.000
   EMAIL            80      80       0     0    1.000   1.000
   IBAN            100     100       0     0    1.000   1.000
   IP              200     200       0     0    1.000   1.000
   NIE               4       4       0     0    1.000   1.000
   PERSON          250     244       6     0    0.976   1.000  (not gated)
   PHONE            33      33       0     0    1.000   1.000
   detector LLM    found 244, false positives 0
   detector REGEX  found 493, false positives 0
   PASS -- every structured entity type is at or above 1.00 recall
   decoys: 0 false positive(s) (max allowed 0) -- PASS
   ```
   **This is the live confirmation of the project's central thesis**: union
   recovers 100% recall on every structured type (regex fills every LLM
   miss/mislabel via `merge_spans`) plus 97.6% recall on `PERSON`, which no
   regex could ever provide — with zero false positives in the union
   configuration (the LLM-only run's CARD/NIE false positives disappear once
   regex's correctly-typed, equal-or-longer spans win the tie-break).
3. Every other assumption checked out unmodified on the first real request:
   auth-free local endpoint, `/v1/chat/completions` schema, spans JSON shape,
   verbatim-substring `value` fields, and the 10s timeout (real responses
   for this model/corpus size returned in well under 1s each).

**Net effect on "Required human follow-up" below**: the manual checklist is
now done, not a TODO — kept below verbatim as a runbook for re-verifying
against a different model/host in the future, with the `response_format`
step's outcome noted inline.

## Acceptance criteria — evidence

1. **LLM spans union with regex via unmodified `merge_spans()`.** ✅
   `tests/test_detector.py::TestDetectLLM::test_happy_path_finds_person_and_address`,
   `TestDetectUnion::test_union_combines_regex_and_llm_spans`. `merge_spans()`'s
   body is untouched by this diff (the hunk starts only after `detect_regex()`).
2. **Prompt requests every `EntityType`, built programmatically.** ✅
   `_ENTITY_TYPE_NAMES = tuple(e.value for e in EntityType)` feeds
   `_SYSTEM_PROMPT` (`detector.py`) — iterates the enum, so a future member
   can't silently drop out of the prompt. Proven against a real HTTP request
   body: `test_prompt_requests_every_entity_type`.
3. **Fail-closed**: dead endpoint, timeout, non-2xx, and malformed JSON all
   proven at the `detect()` level (`TestDetectUnion`, four
   `test_fail_closed_on_*` tests) and additionally at the `tokenize()` level
   for non-2xx (`TestLLMLayerWiring::test_llm_non_2xx_response_still_tokenizes_structured_entities`).
   No raw PII in the fail-closed log line, proven by
   `test_fail_closed_path_logs_no_pii` plus the two error-message hardening
   tests above.
4. **Cache hit**: `test_repeated_call_with_same_text_hits_the_cache` (mock
   invoked exactly once for two identical calls),
   `test_different_text_misses_the_cache`,
   `test_failed_call_is_not_cached_so_a_later_call_can_recover` (a failure is
   never cached, so a transient outage can still succeed next call).
5. **`VEIL_LLM_BASE_URL` unset ⇒ today's regex-only behavior, unchanged.** ✅
   `test_env_unset_reproduces_regex_only_behavior_exactly`; full
   `tests/test_tokenizer.py tests/test_gateway.py tests/test_hooks.py` still
   pass unmodified in outcome.
6. **`demo/eval.py --mode regex|llm|union`.** ✅ Implemented and run
   end-to-end against a **real LM Studio instance** (see "Live LM Studio
   verification" above) — all three modes produced a full per-entity/
   per-detector report; `--mode llm`/`--mode union` correctly returned exit 0
   with a report-only note while the decoy gate still applies regardless of
   mode. `.harness/bin/verify`'s `eval` row calls `eval.py` with no `--mode`
   (default `"regex"`), so the recall gate is byte-for-byte the same command
   as before this mission.
7. **`core/vault.py` unmodified.** ✅ `git diff --stat -- veil_pii_middleware/core/vault.py`
   is empty.
8. **`.harness/bin/verify` exits 0.** ✅ Confirmed directly (see Gate results below).
9. **This document.**

## Gate results

```
verify · veil-pii-middleware
  ✓ contract     bash .harness/internal/check-mission-contract
  ✓ lint         ruff check . && ruff format --check .
  ✓ typecheck    mypy (strict)
  ✓ test         pytest — 233 passed
  ✓ eval         seed.py + eval.py --min-recall 1.0 --max-false-positives 0 (regex mode, unchanged)
  ✓ build        python -m build --no-isolation
RESULT: PASS — all 6 gate(s) green.
```

`git diff --stat`: exactly the 7 files listed in Scope above; nothing else.

## Manual LM Studio verification checklist (completed 2026-09-08 — kept as a runbook)

Run against `http://192.168.31.123:1234` / `qwen_qwen3-4b-instruct-2507` in
this session; see "Live LM Studio verification (completed)" above for
results. Kept here as the runbook for re-verifying against a different
model/host in the future.

1. Start LM Studio, load any instruction-following model, and enable its
   local server (note the port it prints, e.g. `1234`).
2. Set the environment:
   ```bash
   export VEIL_LLM_BASE_URL="http://<lm-studio-host>:<port>"
   export VEIL_LLM_MODEL="<the exact model identifier LM Studio shows>"
   ```
   If LM Studio runs on a different LAN machine, use that machine's address,
   not `localhost` — this is the intended deployment shape (BRIEF §3.1).
3. Regenerate the demo corpus and run all three eval modes:
   ```bash
   .venv/Scripts/python -m veil_pii_middleware.demo.seed
   .venv/Scripts/python -m veil_pii_middleware.demo.eval --mode llm
   .venv/Scripts/python -m veil_pii_middleware.demo.eval --mode union
   ```
   Confirm: the command completes without an unhandled exception; the
   per-entity report includes non-zero `found` counts for `PERSON` (the
   manifest's only current LLM-scored ground truth); `--mode union`'s
   `STRUCTURED_TYPES` recall stays at 1.000 (regex spans still all present).
4. Sanity-check one raw response shape directly, to confirm this code's
   assumption about the host's JSON structure. **Do not add
   `response_format` to this request** — that field is deliberately absent
   from `LLMClient.complete_spans` (`detector.py`) precisely because a real
   LM Studio instance (llama.cpp-backed) rejects
   `{"type": "json_object"}` with a 400 (`'response_format.type' must be
   'json_schema' or 'text'`), confirmed live 2026-09-08; the plain request
   below is what the code actually sends:
   ```bash
   curl -s "$VEIL_LLM_BASE_URL/v1/chat/completions" \
     -H "Content-Type: application/json" \
     -d "{\"model\": \"$VEIL_LLM_MODEL\", \"messages\": [{\"role\": \"user\", \"content\": \"say hi in JSON as {\\\"hi\\\": true}\"}]}"
   ```
   Confirm the response has `choices[0].message.content` as a JSON *string*
   (not a nested object) — this is the exact field `LLMClient.complete_spans`
   parses. Confirmed true for the LM Studio/Qwen instance tested 2026-09-08;
   if a different host returns `content` as a nested JSON object instead,
   `LLMClient.complete_spans` is the only place that needs to change.
5. If step 4 reveals a mismatch, `LLMClient.complete_spans` (`detector.py`)
   is the only place that needs to change — everything downstream
   (`detect_llm`'s offset recovery, `detect()`'s union/fail-closed,
   `tokenize()`'s wiring) is independent of exactly how the raw model
   response is unwrapped into a `spans` list.

## Known limitations (not fixed, out of this mission's scope)

- **Gateway lock serializes on LLM latency.** See the code-reviewer finding
  above — `adapters/gateway/server.py`'s `_vault_lock` now also blocks
  concurrent requests behind an in-flight LLM call once the LLM layer is
  configured. Revisit before enabling this on a live gateway under real
  concurrent load.
- **A fresh `LLMClient`/TCP connection per `tokenize()` call.** Every
  adapter builds its client implicitly from the environment on every call
  rather than once per process. The content-block cache still avoids
  redundant *LLM* calls (keyed by content, not by client instance); this
  only affects new-connection latency on a cache miss.
- **`ADDRESS` has no manifest ground truth.** The `EntityType` exists (so a
  found address is tokenized/audited correctly) but `demo/seed.py`'s
  manifest was not extended to seed one — `--mode llm|union`'s `ADDRESS`
  row will show 0 support until a future mission adds that data.
- **This specific model confuses DNI/NIE labeling.** Live-observed
  (`qwen_qwen3-4b-instruct-2507`, see results above): it correctly finds the
  spans but frequently mislabels a DNI as a NIE. `union` mode hides this
  entirely (regex's correctly-typed span wins), so it only matters for a
  `--mode llm`-only comparison. Not a code defect — genuine model-quality
  data the eval framework is designed to surface; a different/larger model
  may or may not reproduce it.

## Note: a repo-wide permission change was made outside this mission's scope

To run the live LM Studio verification above, `.claude/settings.json`'s
shared `deny` list had `"Bash(curl:*)"`/`"Bash(wget:*)"` removed (at the
user's explicit request, after confirming Claude Code's permission engine
has no way to let a narrower `allow` in a personal `settings.local.json`
override a broader `deny` in the shared project file — deny always wins
regardless of source or specificity). No `allow` rule was added anywhere;
curl/wget now fall through to `defaultMode: "default"`, i.e. each invocation
prompts for confirmation like any other unlisted command, same as before
this project ever added that specific deny entry. **This is a change to
team-shared policy, not personal-only, and is unrelated to the LLM detector
code itself — the human reviewer should decide whether to keep it, restore
the deny, or replace it with something narrower before merging.**

## What a human reviewer should check

1. Read the diff for `core/detector.py`'s new LLM section top to bottom —
   this is the mission's designated high-risk surface (CLAUDE.md).
2. Confirm the two code-reviewer-flagged-and-fixed issues above actually
   read correctly in the final code: no raw field content in any
   `LLMDetectionError` message (`detector.py`, `_locate_next`/`detect_llm`),
   and the per-value `taken_starts_by_value` scoping in `detect_llm`.
3. Run `.harness/bin/verify` yourself to confirm the reported gate state.
4. Decide whether the gateway-lock limitation above blocks merge or is
   acceptable to document and revisit later (this mission's non-goals
   explicitly excluded touching `adapters/gateway/server.py`).
5. Decide what to do about the `.claude/settings.json` curl/wget permission
   change noted above (kept, reverted, or narrowed) — this is a policy
   decision independent of the code, not something to silently carry into a
   merge unreviewed.

## Merge policy

No commit, push, or merge without explicit human review of the diff and
this document, per `.harness/mission.md`'s Merge policy. This mission does
not merge itself.
