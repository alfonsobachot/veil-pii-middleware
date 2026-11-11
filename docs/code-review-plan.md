# Code review plan

Plan for a full review of the codebase, to be run in a fresh session. Written 2026-11-11.

## Context

- The project was built with an earlier model (Sonnet 5.1) through 13 iterations, then
  extended heavily on 2026-11-10/11: encoder detectors, new PII types, a larger benchmark
  corpus, harness removal. Expect leftovers from all of that.
- It is a proof of concept that will be shown live at a company presentation on **Friday
  2026-11-13**. Everything shown runs from this repo: gateway, hooks, panel, `scripts/ask.sh`,
  `demo/eval.py`.
- Read `CLAUDE.md` and `README.md` first, then `docs/benchmarks.md`.
- State at the time of writing: all checks are green (ruff, mypy strict, 380 tests in about
  2.5 min, regex gate at 100% recall and 0 decoys). Last commit: `b9d5c1f`.

## Goal and scope

Find what is wrong or clearly improvable, rank it, and fix what matters **before Friday**,
without destabilising the live demo.

Every finding goes into one of three buckets:

1. **Fix now:** correctness bugs, any path where PII could leak, demo-breaking risks, and
   small low-risk simplifications in code the demo exercises.
2. **Fix after the presentation:** large refactors, file splits, API redesigns. Record them
   with enough detail to act on later.
3. **Won't fix:** intentional trade-offs already documented (README section 9).

**Hard rule:** no behaviour change to detection, tokenization or the vault without a test that
pins it. Re-run the regex gate after any detector change.

## Process

1. **Survey.** Read every module once. Map the call paths:
   - gateway request: `tokenize` → upstream → SSE detokenize;
   - hook: Pre/Post → vault;
   - eval: `evaluate` → `detect`.
2. **Review by area** (checklist below). Prefer reading code over re-running things. Use
   parallel subagents per area if it helps, but give each one the area's file list and
   questions, not conclusions.
3. **Report to the user before fixing.** Present a findings table: id, file:line, bucket,
   severity (leak / bug / demo risk / quality), and a one-line fix. Let the user prune it. The
   user communicates in Spanish; keep code, comments and commits in English.
4. **Fix the "fix now" bucket** in small commits on `main` (the user does not want feature
   branches). After each logical group, run:
   - `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .`
   - `.venv/Scripts/mypy`
   - `.venv/Scripts/pytest` (about 2.5 min)
   - `.venv/Scripts/python -m veil_pii_middleware.demo.eval --mode regex` (must PASS)
5. **End-to-end smoke test:**
   - start the gateway with `scripts/run-gateway.sh`;
   - run `scripts/ask.sh --demo-db "Customer user1@... has an invoice problem..."` with
     the Ollama LLM configured (see `CLAUDE.md`);
   - check the panel at `http://127.0.0.1:8787/veil-panel`.
6. **Write up the "after the presentation" bucket** as `docs/backlog.md`.

## Review checklist by area

### Core: `core/detector.py` (about 1200 lines), `core/tokenizer.py`, `core/encoder.py`

- **Merge rule (known issue, needs a decision).** `merge_spans` keeps the longest span, so a
  soft span can swallow a validated regex span. Examples: GLiNER tags `Courier van 5219 MDX` as
  ORG; qwen3.8 tags `billing:<password>` as SECRET. Nothing leaks, but it over-tokenizes and
  skews the eval. Proposed fix: regex spans take precedence, and an overlapping soft span is
  trimmed to its non-overlapping remainders. This changes eval numbers, so do it before the
  benchmark re-run.
- **Pattern ordering.** `_PATTERNS` order decides exact ties (e.g. NSS before CARD).
  Check every pair of types that can produce the same span.
- **Known false positive.** `version 1.2.3 build 4.5.6.7`: the IP pattern claims `4.5.6.7`.
  Decide whether to fix it or document it.
- **Validators.**
  - `valid_nss`: province range, and the two-case base rule.
  - `valid_cif`: letter-only and digit-only entity sets.
  - `valid_secret`: placeholder list.
  - `_SECRET_PATTERNS`: generic `key=value` false positives in normal logs.
- **Unbounded caches.** `_llm_span_cache` and `_person_confirmation_cache` are module-level
  dicts that are never evicted in a long-running gateway.
- **LLM client.**
  - One new httpx client per gateway request (`_default_llm_client_for_display`).
  - Retry-on-5xx semantics.
  - `_parse_json_object` fallback could grab the wrong `{...}`.
- **Non-PII name list.** `_NON_PII_NAMES` and `_is_known_non_pii_name`: scope and false
  negatives.
- **Encoder.**
  - Thread safety when the gateway serves concurrent requests: one shared model, `_ENCODER`
    global.
  - Chunking boundaries: duplicate or split entities at the overlap.
  - `trust_remote_code` opt-in.
  - Label maps.
- **Tokenizer.**
  - `tokenize()` parameter sprawl: `skip`, `exclude`, `use_llm`, `encoder`, `span_sink`,
    `audit_log`.
  - The already-tokenized-range guard.
  - `IncrementalDetokenizer` edge cases.
- **Comment and docstring bloat.** Many comments narrate history ("found live on ...",
  "previously this ...") instead of explaining the code. Trim to the why, but only in files
  touched anyway; it is a large diff otherwise.

### Vault and audit: `core/vault.py`, `core/audit.py`

- Key handling: env only, never logged.
- AES-GCM nonce generation.
- HMAC lookup normalisation: does the same value always yield the same token, and how are
  case and whitespace handled?
- SQLite concurrency with the gateway and both hooks writing at once.
- Append-only audit: file locking on Windows, no plaintext in any field.

### Gateway: `adapters/gateway/server.py` (about 1800 lines, one module)

- **Biggest refactor candidate.** It mixes the HTTP handler, SSE rewriting, the pipeline event
  bus, panel endpoints, metrics, cycle classification and display-text extraction. Propose a
  split, but do it **after Friday** unless a bug forces it.
- `_vault_lock` serialises every request behind LLM/encoder calls: latency during the demo.
- `_TOKENIZED_FIELDS = {"messages"}`: confirm `tool_result` content in every shape (string,
  list of blocks, nested images) is reached.
- SSE detokenization across chunk boundaries; non-SSE JSON responses; error responses
  relayed untouched.
- Panel endpoints: input validation on `/vault/reveal` and `/hook-event`; metrics comparison
  runs synchronously inside a request handler (blocks a thread for many minutes with the
  LLMs).
- `ENCODER_ENV_VAR` duplicated from `core.encoder`; module globals.

### Hooks and command: `adapters/hooks/*`, `adapters/command/sanitize.py`

- Fail-closed paths: what happens on vault errors, malformed payloads, an unset
  `VEIL_VAULT_KEY`.
- `_scope.is_demo_db_command`: how strict is the match?
- `post_tool_use` lets untokenized output through on failure. This is documented; check the
  message is clear.
- **Upcoming change (decided 2026-11-11, option B).** For the new UI the hooks will report
  their actual before/after text to the panel, including real detokenized values, accepted
  for this proof of concept. Review the current `_panel_report` design so that change slots
  in cleanly. Reporting must stay best-effort and must never affect the hook's own result.

### Demo and eval: `demo/seed.py` (about 1000 lines), `demo/eval.py`, `scripts/*.sh`

- `seed.py`: is the ground truth correct? Values are located with `str.find`, so check
  offsets for repeated substrings. Watch for accidental extra PII in templates that the
  manifest does not record, which shows up as false positives in every detector.
- `eval.py`:
  - soft-overlap matching (`_SOFT_MIN_COVERAGE`) and double counting;
  - per-detector accounting;
  - the `regex+encoder` decoy path;
  - the progress output.
- `scripts/ask.sh`: complexity; the hard-link fallback; the English system prompt.
- `run-gateway.sh`: the key file is plaintext (documented).

### Frontend: `frontend/panel.html` (about 1200 lines, React + Babel from a CDN)

- **The UI will be rewritten next**, so only note demo-blocking bugs here; do not polish.
- **Offline risk:** the CDN scripts are required at presentation time. Consider vendoring
  them.

### Tests

- Slowest tests (`--durations=10`), e.g. the 23 s audit concurrency test.
- Tests that assert implementation details rather than behaviour.
- Gaps:
  - gateway handling of `tool_result` lists;
  - encoder backends (no unit tests; only exercised by the benchmark);
  - eval soft matching.

## After the review (not part of this plan)

1. **Freeze the detector code**, then re-run the full benchmark on the new corpus: 5 encoders
   plus 3 LLMs (qwen3:4b, gpt-oss:20b effort=low, qwen3.8:27b effort=none). Run models one
   at a time and report progress to the user about every 5 minutes. Then update
   `docs/benchmarks.md`.
2. **Before the re-run,** add the planned corpus extensions:
   - about 100 synthetic **Sentry events** (user context, frame local vars, request
     headers/cookies, breadcrumbs, exception messages);
   - a sample of about 300 Spanish documents from `gretelai/synthetic_pii_finance_multilingual`
     as an external dataset. Its labels were produced by GLiNER, so flag the bias towards
     GLiNER.
3. **UI rewrite** with animations of the full flow (prompt → tokenize → API → PreToolUse →
   DB → PostToolUse → detokenize), including hook before/after text (option B). It can run
   while the benchmark runs, since the eval does not touch the hooks or the panel.
4. **Code freeze Thursday evening**, then a full demo rehearsal. Keep the current panel as a
   fallback until the new one is proven.
