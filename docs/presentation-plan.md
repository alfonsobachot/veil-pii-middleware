# Presentation plan

Remaining work before the live presentation on **Friday 2026-11-13** (in English). Written
2026-11-11 in the evening, to be executed in fresh sessions. Steps are in order; each one says
when it is done.

## Status on Thursday 2026-11-12, afternoon

- **Step 1 done.** Demo customer, prompt and setup in `docs/demo-script.md`; 6 consecutive
  recorded runs with zero leaks (regex + qwen3 4b + GLiNER in the gateway).
- **Step 2 deferred** until after the presentation (no time; the corpus stays as it is).
- **Step 3** running on the current detector (`5806341` and later: no detector change since).
- **Step 5 done** without the `server.py` split: option B in the hooks (b4ee3cb) and the
  flow view at `/veil-panel/flow` (405c363). The classic panel stays at `/veil-panel`.
- **Steps 3 and 4 done:** 9 configurations re-run at `5806341` (qwen3.8 had 1 of 1,063 cells
  fall back to regex-only; noted, not repeated); `docs/benchmarks.md` and README §8 rewritten.
- **Step 6:** rehearsal at 17:20 with the exact Friday setup (model reloaded, regex + qwen3 4b
  + GLiNER, flow view): main prompt twice and the fallback once, all three with zero PII in
  the recorded upstream requests. Q&A answers in `docs/qa-notes.md`. Pushed. Still to do on
  Friday: reload the Ollama model before starting (`docs/demo-script.md`, Setup step 1).

## Where things stood on Wednesday

- The code review (`docs/code-review-plan.md`) is finished. Every "Fix now" item in
  `docs/review-findings.md` is fixed and ticked, plus smoke-test fixes S4, S6, S7 and S9.
- `main` is at `55ee643`, in sync with `origin`. Ruff, mypy, the full pytest suite and the
  regex gate (`--mode regex`: 100% recall, 0 decoys) are green on it.
- Still open from the 2026-11-11 smoke test (details in `docs/review-findings.md`, section
  "Smoke test 2026-11-11"):
  - **S8.** `Consultoría Guillen & Asociados S.Com.` reached the API in clear when it appeared
    in tuple-shaped query output (`customer (2, 'Consultoría Guillen…', …)`); qwen3 4b missed
    it in both the post-hook and the gateway.
  - **S5.** The LLM layer over-tokenizes the prompt: "Python's sqlite3 module",
    `demo/veil_demo.db` and table names become `PII_OTHER_*` / `PII_ORG_*`, and the `email`
    column name became `PII_USERNAME_0001`. Nothing leaks, but the panel shows a mangled
    prompt, and that is the first thing the audience sees.
  - **S3.** A failing Bash call skips PostToolUse. Only matters in hooks-only mode
    (`run_demo.sh`); `ask.sh` goes through the gateway, which tokenizes it.
- `docs/benchmarks.md` is stale: old corpus (533 cells) and old scoring rules. README §8
  ([README.md:387](../README.md)) shows numbers from an even older corpus and LM Studio, and
  [README.md:209](../README.md) still says "these seven types".

## Ground rules

- Reply to the user in Spanish; code, comments, commits and docs in English.
- Commit on `main` in small commits; push only when the user asks.
- After any code change: `ruff check`, `ruff format --check`, `mypy`, `pytest`, and
  `demo.eval --mode regex` (must PASS). Commands are in `CLAUDE.md`.
- Any behaviour change to detection, tokenization or the vault needs a test that pins it.
- On long runs (benchmark), report progress to the user about every 5 minutes.
- **GPU contention.** Ollama and the encoders share one RTX 4090. Do not run a live demo with
  the LLM layer while a benchmark LLM run is in progress: models get swapped in and out of
  VRAM and both slow down or time out. UI work during the benchmark should run the gateway
  regex-only (leave `VEIL_LLM_*` unset) or wait for the encoder-only part of the run.

---

## Step 1. Fix the demo script and make it clean (S5, S8)

Goal: the exact turn shown on Friday works every time, nothing synthetic-PII reaches the API,
and the panel shows a readable prompt. Do this first: it can require a detector change, and
the detector must be frozen before the benchmark (step 3).

1. Write the exact presentation prompt (or two, a main one and a fallback) into
   `docs/demo-script.md`, with the expected flow: prompt → tokenized → model queries the demo
   DB with a token → PreToolUse resolves it → result tokenized → answer resolved on screen.
   The plan's example: `Customer user1@... has an invoice problem, can you check the database
   and tell me what happened?`. Pick a customer whose rows exercise several types (email,
   name, company, IBAN, a log line with an IP).
2. Run it 3 times with `scripts/run-gateway.sh` + `scripts/ask.sh --demo-db "<prompt>"` and the
   Ollama LLM configured (`CLAUDE.md`). Capture what goes upstream: run a recording proxy
   between the gateway and the API via `VEIL_GATEWAY_UPSTREAM`, as the smoke test did, and
   grep the captured requests for every PII value of that customer from the demo DB.
3. **S8.** If a company or name still goes out in clear:
   - First check whether GLiNER catches it: start the gateway with
     `VEIL_ENCODER=gliner:urchade/gliner_multi_pii-v1` (regex + LLM + encoder) and repeat.
     Note the added latency per turn. Hooks do not use the encoder, but the gateway
     re-tokenizes everything on the next request, which is what protects the API leg.
   - If GLiNER fixes it at an acceptable latency, make it part of the demo setup (document
     the env var in `docs/demo-script.md` and in `scripts/run-gateway.sh` comments).
   - Otherwise pick a demo query whose output shape is tokenized correctly, and record S8 as a
     known gap for the Q&A.
4. **S5.** Make the prompt pane readable. Options, cheapest first:
   - Reword the demo prompt so it does not name files, modules or tables in prose.
   - If the over-tokenization comes from Claude Code's own injected text rather than the
     user's words, check whether it is in `messages` at all after S6's isolation.
   - Only if needed, a detector change (e.g. a stricter LLM prompt rule against code
     identifiers and file paths, or extending `_NON_PII_NAMES`). That is a detector change:
     pin it with a test and re-run the regex gate.
5. **Done when:** 3 consecutive runs of the main prompt have zero synthetic-PII values in the
   captured upstream requests, the panel's prompt pane shows only real PII as tokens, and
   `docs/demo-script.md` is committed.

## Step 2. Extend the corpus with Sentry-style events

Goal: the benchmark covers the incident-response payloads the demo story is about. The
gretel dataset from the original plan is **deferred until after the presentation**: its
labels come from GLiNER, which biases the comparison towards one of the detectors being
compared.

1. Add about 100 synthetic Sentry events to `demo/seed.py`, as a new table (e.g.
   `sentry_events` with one JSON payload column), with PII in realistic places:
   - `user` context (`email`, `username`, `ip_address`, `name`);
   - stack frame local variables (`vars`), including names inside f-strings and dicts;
   - request headers and cookies (`Authorization`, `Cookie`, `X-Forwarded-For`);
   - breadcrumbs (SQL queries with emails or DNIs, HTTP calls with IBANs in the URL);
   - exception messages.
2. Annotate every value in the manifest through the existing `note()` path, with exact
   offsets into the stored JSON text. Add a few decoys that fit the format (a request id that
   looks like a card but fails Luhn, a build version with four parts, `password=********`).
3. Keep the existing tables and seed unchanged for the demo customer chosen in step 1, so the
   demo script still matches the data (check after re-seeding).
4. Tests: extend `tests/test_demo.py` so every manifest value occurs exactly once in its cell
   and `text[start:end] == value` holds for the new table (E13 in the findings).
5. Re-seed, run `demo.eval --mode regex`: it must still PASS with 100% recall and 0 decoys.
   If a structured value inside the JSON is missed, that is a real detector bug: fix it with
   a pinning test before step 3.
6. **Done when:** the new table is seeded, the manifest checks pass, and the regex gate
   passes.

## Step 3. Freeze the detector and re-run the benchmark

From here until the presentation, no changes to `core/detector.py`, `core/encoder.py` or
`demo/eval.py` unless a bug forces it (and then re-run the affected rows).

1. Record the commit hash the benchmark runs against.
2. Run each configuration on its own, one at a time, and save each full report to a file
   under `.veil/bench/` (git-ignored) so the numbers can be copied later:
   - Regex only: `--mode regex` (baseline, seconds).
   - Encoders, `--mode regex+encoder --encoder <spec>` (threshold 0.4, GPU):
     - `gliner:urchade/gliner_multi_pii-v1`
     - `gliner2:fastino/gliner2-privacy-filter-PII-multi`
     - `hf:FacebookAI/xlm-roberta-large-finetuned-conll02-spanish`
     - `hf:OpenMed/privacy-filter-multilingual-v2`
     - `hf:mrm8488/bert-spanish-cased-finetuned-ner`

     Backend prefixes (`gliner`, `gliner2`, `hf`) match `core/encoder.py`; all three Ollama
     models are already pulled.
   - LLMs, `--mode union` with `VEIL_LLM_BASE_URL=http://127.0.0.1:11434` and
     `VEIL_LLM_TIMEOUT=180`:
     - `qwen3:4b-instruct-2507-q4_K_M` (the demo default);
     - `gpt-oss:20b` with `VEIL_LLM_REASONING_EFFORT=low`;
     - `qwen3.8:27b-q4_K_M` with `VEIL_LLM_REASONING_EFFORT=none`.
3. Rough timings on the current corpus (839 cells + 194 decoys, more after step 2): encoders a
   few minutes each; qwen3:4b about 5 min; gpt-oss about 12 min; qwen3.8 about 30 min. Run
   them in the background and report progress every 5 minutes. Encoders first, so their
   numbers are available while the LLMs run.
4. A run that exits 2 (a layer call failed) is invalid: fix the cause (timeout, model not
   pulled) and re-run that configuration.
5. **Done when:** all 9 reports are saved and none exited 2.

## Step 4. Update the benchmark and README docs

1. Rewrite `docs/benchmarks.md` with the new numbers:
   - remove the "Superseded corpus" note; update the Setup section (cell, instance and decoy
     counts, the Sentry table);
   - regenerate both results tables and the per-type table;
   - rewrite the structured-layer paragraph: with the new merge rule (regex first, soft spans
     trimmed) plates and secrets should no longer be reported as missed. Verify this, and if
     any still are, explain why;
   - drop footnote ¹ (fixed and now re-run) and the "Fixes made during benchmarking" items
     that no longer apply; revisit Findings and Caveats against the new data.
2. README §8: replace the old LM Studio tables with the new regex gate output and a short
   union summary for qwen3:4b, and link to `docs/benchmarks.md` for the full comparison.
3. README line 209: "these seven types" → the current structured type list.
4. **Done when:** both docs are committed and every number in them comes from a step 3
   report.

## Step 5. UI rewrite (can run in parallel with step 3)

The new panel shows the full flow with animations: prompt → tokenize → API → PreToolUse → DB
→ PostToolUse → detokenize, including the hooks' before/after text (option B, accepted for
this proof of concept).

1. **Partial split of `server.py`** (only if steps 1–2 are done and green): extract
   `panel.py` (panel routes, `_write_json`, `_write_html`, `_write_sse_headers`,
   `_query_param`, the body-reading helper) and `pipeline_bus.py` (`PipelineEventBus`,
   `_publish_stage`, `PIPELINE_STAGES`, history limits, `_AUXILIARY_CYCLE_KINDS`). No
   behaviour change; the existing gateway tests must pass untouched. Watch the
   `monkeypatch.setattr(gateway_server, "VAULT_DB_PATH" / "AUDIT_LOG_PATH", ...)` in
   `tests/test_gateway.py`: if those constants move, the patch silently stops working.
2. **Option B in the hooks** (design in `docs/review-findings.md`, H8):
   - `_panel_report.report()` gets optional `before` / `after` strings, truncated to about
     8 KB. Pre sends `command` → detokenized command; Post sends `tool_response` → tokenized
     output.
   - `/veil-panel/hook-event` accepts them with a length cap and puts them on the bus event
     only. Never write them to `audit.jsonl`.
   - Reporting stays best-effort: it must never change the hook's exit code or stdout. While
     there, apply H7 (one call per hook, `trust_env=False`, short timeout).
   - Update what pins the old contract: docstrings in `_panel_report.py` and the gateway's
     hook-event handler, README §7 (the "never a command string or tool output" wording),
     and the tests in `tests/test_gateway.py` that assert the exact key set and that
     `command` / the DNI are absent.
3. **New panel.** A new file next to `frontend/panel.html`, served at a new route, so the
   current panel stays available as the fallback until the new one is proven. Reuse the
   vendored React/Babel route (`/veil-panel/vendor/...`, R12); no CDN. Must work offline.
   Keep the audit-log pane and the vault inspector.
4. **Done when:** a step 1 demo run animates end to end in the new panel, with both hooks'
   before/after text visible, and the old panel still works.

## Step 6. Thursday: freeze and rehearsal

1. Code freeze Thursday evening. Full checks green, then push.
2. Rehearsal, from a fresh terminal (Git Bash), exactly as on Friday:
   - start Ollama; start the gateway with `scripts/run-gateway.sh` (it warms up and pins the
     LLM; both warm-up lines must print);
   - open the new panel; disconnect the network briefly and reload it to confirm it renders
     offline (R12), then reconnect;
   - run the main prompt from `docs/demo-script.md` with `scripts/ask.sh --demo-db`, then the
     fallback prompt;
   - check the panel: tokenized prompt, hook before/after, audit log, vault inspector;
   - do **not** click "Run comparison" in the old panel during the demo (minutes long).
3. Prepare answers for the known gaps likely to come up in Q&A: S8 (if still open), S3, the
   `/veil` `<command-args>` leak, regex-only fallback when the LLM is down (D7), LAN-only LLM
   traffic.
4. **Done when:** two consecutive clean rehearsals.

## Deferred until after the presentation

- The gretel Spanish sample (`gretelai/synthetic_pii_finance_multilingual`, ~300 docs; flag
  the GLiNER label bias).
- S3 (`PostToolUseFailure` in hooks-only mode).
- The rest of the `server.py` split and everything in the "After the presentation" section
  of `docs/review-findings.md`, notably G7 (replace known vault values everywhere, the root
  cause behind S6) and D7 (strict mode that blocks instead of falling back to regex-only).
