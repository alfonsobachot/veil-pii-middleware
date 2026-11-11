# Code review findings

Results of running `docs/code-review-plan.md` on 2026-11-11 against commit `a966065`. Each
finding is meant to be picked up in a fresh session, so it carries its location, evidence and
proposed fix. Line numbers are from `a966065` and will drift as fixes land.

Baseline at review time: ruff, `mypy --strict`, the full pytest suite and the regex gate
(`demo.eval --mode regex`: 100% recall, 0 decoys) were all green.

## How to work through this

- One finding (or one tight group) per commit, on `main`.
- Hard rule from the plan: no behaviour change to detection, tokenization or the vault without
  a test that pins it.
- After each group run:
  - `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .`
  - `.venv/Scripts/mypy`
  - `.venv/Scripts/pytest`
  - `.venv/Scripts/python -m veil_pii_middleware.demo.eval --mode regex` (must PASS)
- Tick the box and add the commit hash when a finding is done.
- Prototypes for several fixes were written during the review in a session scratchpad that
  will not survive. The descriptions below are enough to re-create them.

Suggested order: leaks (F) → gateway (R2–R10) → hooks and scripts (R1, R11, R13) → eval (E) →
panel vendoring (R12) → environment (R14). Then the end-to-end smoke test from the plan. If
time allows before the UI rewrite, the partial `server.py` split (see "After the
presentation" → Gateway).

## Open decisions

Recommendations in bold. Settle these before working on the linked findings.

1. **Merge rule (F4).** **Regex spans take precedence; an overlapping soft span is trimmed to
   its non-overlapping remainders.** Changes the soft-type eval numbers, so it must land
   before the benchmark re-run.
2. **IP false positive on `version 1.2.3 build 4.5.6.7` (D9).** **Document it and pin the
   current behaviour with a test.** A context filter (`(?<!\bv)(?<!version )(?<!build )`)
   removes it but makes `build 10.0.0.5` leak.
3. **Decoy gate in `llm`/`union`/encoder modes (E4).** **Report only; the exit code is
   decided by `--mode regex` alone**, and fix `--help` to match.
4. **Fail-closed status code (R7).** **400 `invalid_request_error` with a "veil blocked"
   message** instead of 500, so Claude Code does not retry.

---

## Fix now: PII leaks

### F1. Wrapper-tag exclusion skips tokenization anywhere in `messages`

- [ ] Done
- **Where:** `adapters/gateway/server.py:710-723, 752-771` (`_excluded_ranges`).
- **Evidence (confirmed):** text inside `<session>`, `<system-reminder>`, `<command-*>` or
  `<local-command-*>` is never tokenized, wherever it appears, including `tool_result`
  content. The match is case-insensitive and allows attributes. A `tool_result` containing
  `<session>row: maria.lopez@example.com 12345678Z</session>` was forwarded in clear, and so
  was `<Session id=1>…</session>`, while the same email outside the tag became
  `PII_EMAIL_0001` (which also tells the API which token is which email). Claude Code also
  puts real file content inside `<system-reminder>` (changed-file notices, IDE selections,
  hook `additionalContext`).
- **Fix:** exclude only the commit-trailer email from substitution. Strip wrapper tags for
  display only, never for tokenization. If reminder false positives matter, scope any
  exclusion to `type:"text"` blocks of `role:"user"` messages, never inside `tool_result`.
- **Test:** a `tool_result` with PII inside `<session>…</session>` and inside
  `<system-reminder>` reaches upstream tokenized.

### F2. Card number next to expiry or CVV is not detected

- [ ] Done
- **Where:** `core/detector.py:429` (`_CARD`), `556-570` (validation loop).
- **Evidence (confirmed):** `_CARD` matches greedily up to 19 digits; when the whole match
  fails Luhn it is discarded and `finditer` has already consumed the characters. All of these
  return `[]`:
  - `cvv 4111 1111 1111 1111 12/28`
  - `tarjeta 4111 1111 1111 1111 123`
  - `ref 12 4111 1111 1111 1111`
- **Fix:** mirror the IBAN path. On a Luhn failure, try contiguous sub-runs aligned on
  separator groups, longest first, inside the failure branch (keep CARD in `_PATTERNS`; the
  decoy sabotage test patches it there). Match the maximal digit run (`{11,}`) so the search
  sees the whole sequence.
- **Test:** parametrize the three strings above plus `4111111111111111 123`; each yields
  `CARD 4111 1111 1111 1111` (or the compact form).

### F3. IBANs whose length is not a multiple of 4 are missed

- [ ] Done
- **Where:** `core/detector.py:416-418` (`_IBAN`), `539-546` (`_trim_iban`).
- **Evidence (confirmed):** the pattern only accepts full 4-character groups.
  `DE89 3704 0044 0532 0130 00` yields only `PHONE '0044 0532 0130 00'` (`DE89 3704` leaks).
  `DE89370400440532013000`, `GB29 NWBK 6016 1331 9268 19`,
  `FR14 2004 1010 0505 0001 3M02 606` and `NL91 ABNA 0417 1643 00` yield `[]`.
- **Fix:**
  - Pattern: `[A-Za-z]{2}[0-9]{2}(?:[ \-]?[0-9A-Za-z]{4}){2,7}(?:[ \-]?[0-9A-Za-z]{1,3})?`.
  - `_trim_iban` must drop whole trailing groups (`re.sub(r"[ \-]?[0-9A-Za-z]{1,4}$", "", c)`)
    instead of `[:-4]`; otherwise `ES91 … 1332 de Juan` swallows `de` and the ES IBAN leaks
    (regression seen in the prototype).
  - Consider requiring an uppercase country code and a word boundary: see R2, random base64
    matches the current pattern.
- **Test:** DE, GB, FR, NL grouped and compact, plus `ES91 2100 0418 4502 0005 1332 de Juan`.

### F4. `merge_spans` drops the losing span whole

- [ ] Done (needs decision 1)
- **Where:** `core/detector.py:518-536`.
- **Evidence (confirmed):** on a partial overlap the loser is dropped entirely, so its
  non-overlapping part goes out in clear:
  - LLM `PERSON 'Juan 612'` vs regex `PHONE '612 345 678'`: `Juan` leaks.
  - ORG `Ana Maria` vs PERSON `Maria Lopez Garcia`: `Ana` leaks.
  - `tel +34 612 345 678 4111 1111 1111 1111` → `PHONE '+34 612 345 678 4111'` (greedy
    `_PHONE_INTL`); CARD loses and `1111 1111 1111` leaks.
  - Plan's known case: `Courier van 5219 MDX` becomes one ENCODER ORG span that swallows the
    plate; `billing:<password>` becomes one LLM SECRET.
- **Fix:** resolve regex spans among themselves as today (longest, then `_PATTERNS` order;
  note the tie-break relies on `sorted` being stable). Then add soft spans by length. Any
  loser that overlaps is cut to its uncovered parts; strip non-alphanumerics from each part's
  edges and drop parts with no alphanumerics. Prototype results:
  - `Courier van 5219 MDX` → `ORG 'Courier van'` + `PLATE '5219 MDX'`.
  - `billing:g7bHSF33PO52FL` → `SECRET 'billing'` (LLM) + `SECRET 'g7b…'` (REGEX).
  - Soft span fully inside a regex span (name inside an email) → dropped.
  - Regex span inside a soft span (IP inside an ADDRESS) → two ADDRESS parts.
  - `(612 345 678)` under a soft span → leftover `(`/`)` dropped.
  - Idempotent.
  - The regex-vs-regex case above (PHONE eating a card group) is not solved by this alone;
    consider also trimming regex losers, or tightening `_PHONE_INTL`.
- **Tests:** rewrite `test_merge_breaks_length_ties_in_favour_of_regex`
  (`tests/test_detector.py:377`), which pins the old rule. Add one test per case above.
  Update the "longest wins" wording in the module docstring, README §4 and CLAUDE.md.

### F5. Tokenizer drops any span that touches an existing token; token pattern has no boundaries

- [ ] Done (shares the trimming helper with F4)
- **Where:** `core/tokenizer.py:54` (`_TOKEN_PATTERN`), `233-234` (`_drop_overlapping`).
- **Evidence (confirmed):**
  - A span that covers a token plus real PII is discarded whole: LLM `PERSON` over
    `PII_PERSON_0001 Garcia Lopez` → `'Cliente PII_PERSON_0001 Garcia Lopez, calle Mayor 5'`
    leaves `Garcia Lopez` in clear. The same happens with the gateway's `exclude` ranges.
  - The token pattern matches inside words, so these pass through untouched:
    `mail PII_EMAIL_0001.juan@corp.com now`, `mail juan.PII_X_1234@corp.com`,
    `PII_EMAIL_0001juan@corp.com`.
- **Fix:** trim the span to its non-overlapping parts instead of dropping it; use a bounded
  pattern for the guard: `(?<![A-Za-z0-9_.@])PII_[A-Z]+_\d{4}(?!\d)`.
- **Tests:** monkeypatch `tokenizer.detect` to return a token+surname span and assert the
  surname is absent from the output; `PII_EMAIL_0001.juan@corp.com` must not survive. There
  is no test of this guard today.

### F6. Common secret shapes are missed

- [ ] Done
- **Where:** `core/detector.py:470-473` (generic key/value in `_SECRET_PATTERNS`).
- **Evidence (confirmed):** the `\b` before the keyword fails after `_`, and there is no
  `token` or `Authorization` keyword. All return `[]`: `DB_PASSWORD=hunter2222`,
  `POSTGRES_PASSWORD=Sup3rS3cret!`, `AWS_SECRET_ACCESS_KEY=wJalr…`,
  `access_token=abcdef123456789`, `token: abcdef123456789`,
  `Authorization: Basic dXNlcjpwYXNzd29yZA==`.
- **Fix:** prefix `(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_\-.])*`, add `token`, allow only a `key`
  or `hash` suffix (so `token_count=` stays clean), and add an
  `authorization: (basic|bearer|token) <value>` pattern. Prototype kept the gate at PASS
  (SECRET 100/100, 0 decoys).
- **Test:** the six positives above, and these negatives: `retry=3`, `timeout=30s`,
  `key=value`, `password=<redacted>`, `tokens=1234567`, `token_count=123456`.

### F7. One bad LLM entry or chatty JSON loses the whole LLM layer for a block

- [ ] Done
- **Where:** `core/detector.py:1012-1025` (`detect_llm`), `763-776` (`_parse_json_object`).
- **Evidence:**
  - (read-only) A single malformed entry (`"value": 612345678` as a number, empty string,
    `entity_type: null`) raises; `detect()` then drops every LLM span for that block, so
    correct PERSON/ORG/ADDRESS spans go out in clear.
  - (confirmed) First-`{` to last-`}` slicing throws on
    `<think>return {"spans": []} if none</think>{"spans":[…]}` and on
    `Here: {"spans":[…]} Note: {x}` (`JSONDecodeError: Extra data`).
- **Fix:** skip bad entries with `continue` (raise only on a bad top-level shape). Parse with
  `json.JSONDecoder().raw_decode` from each `{` and return the first dict with the expected
  key (`spans` or `persons`).
- **Tests:** rewrite `test_non_string_entity_type_raises`,
  `test_flat_string_span_entry_raises_without_leaking_its_content` and
  `test_bad_value_field_raises_without_leaking_other_fields` as "bad entry skipped, good
  entries kept, nothing reaches stderr"; add the two chatty replies above.

### F8. Audit `value_hash` is an unkeyed SHA-256

- [ ] Done
- **Where:** `core/audit.py:130`; pinned by
  `tests/test_audit.py::test_value_absent_only_its_hash_present`.
- **Evidence (confirmed):** DNI and phone spaces are tiny; pure Python does 1M SHA-256 in
  0.6 s, so every DNI is recoverable in about 60 s. Contradicts "never records plaintext".
  Nothing reads `value_hash` except tests (the panel does not).
- **Fix:** HMAC-SHA256 with an HKDF subkey of `VEIL_VAULT_KEY` (info `b"veil-audit"`), or
  drop the field.
- **Test:** change the pinning test to assert `sha256(value)` does not appear in the file.

---

## Fix now: demo risks and bugs

### R1. `ask.sh --demo-db` queries a stale copy of the demo database

- [ ] Done
- **Where:** `scripts/ask.sh:91-94`.
- **Evidence (confirmed):** the link/copy only happens if the scratch DB does not exist yet.
  `seed.py:990` unlinks and recreates the DB, so the hard link breaks on every seed.
  `~/.veil-cli-scratch/demo-db/demo/veil_demo.db` was a different inode (98 304 bytes,
  2026-11-10) from the repo's (212 992 bytes, 2026-11-11).
- **Fix:** always `rm -f demo/veil_demo.db; ln … || cp …`.

### R2. Gateway tokenizes ids, signatures and base64 payloads

- [ ] Done
- **Where:** `adapters/gateway/server.py:795-817` (`_tokenize_request`).
- **Evidence (confirmed):** every string under `messages` is scanned, including `type`, `id`,
  `tool_use_id`, thinking `signature`, `redacted_thinking.data` and image/document
  `source.data`. The IBAN regex matched in 6 of 20 random 200 KB base64 blobs (e.g.
  `pf50hVxWO4IIUrzQljUUt37gsKD0`; the pattern accepts lowercase and mod-97 passes 1 in 97).
  A corrupted image or signature gets a 400 from the API, and a corrupted signature breaks
  every later request in the session. The LLM and encoder also run on every id, signature
  and blob, all inside `_vault_lock`: the main source of latency.
- **Fix:** walk content blocks with fixed rules. Pass through `type`, `id`, `tool_use_id`,
  `signature`, `cache_control`, `source` when `source.type == "base64"`, and
  `thinking`/`redacted_thinking` blocks. Tokenize `text`, `tool_result.content` (string or
  list, recursively), `tool_use.input` and document text.
- **Tests:** (a) `tool_result` with `content=[{"type":"text","text":"DNI 12345678Z"}]`
  reaches upstream as `PII_DNI_0001`; (b) `tool_use.input` with an email is tokenized and
  its `id` unchanged; (c) an image block whose base64 contains
  `pf50hVxWO4IIUrzQljUUt37gsKD0`, and a thinking block with a `signature`, are forwarded
  byte for byte (fails today).

### R3. Detokenized thinking can break its signature on the next turn

- [ ] Done (do with R2)
- **Where:** `adapters/gateway/server.py:971-974` (`_TEXT_BEARING_DELTA_FIELDS`).
- **Evidence (read-only, needs one live check):** `thinking_delta` is detokenized, so Claude
  Code stores thinking with real values. Next turn the gateway re-tokenizes it; if the result
  is not byte-identical (different LLM span, a name the model wrote, a SECRET hit inside the
  signature) the signature no longer matches and the API returns 400. Most likely in a
  tool-use loop.
- **Fix:** remove `thinking_delta` from `_TEXT_BEARING_DELTA_FIELDS`; pass thinking blocks
  through unchanged (R2). Thinking is generated from tokenized input, so nothing leaks.

### R4. Vault race between processes; gateway lock held across LLM calls

- [ ] Done
- **Where:** `core/vault.py:195-210, 229-258`; `adapters/gateway/server.py:916`
  (`_vault_lock`), comment at `server.py:513`.
- **Evidence:**
  - (confirmed) Lookup, counter read and inserts are not in one transaction (Python's default
    `isolation_level` opens none for `SELECT`). Forced interleaving with two connections
    gives `IntegrityError: UNIQUE constraint failed: mappings.session_id, mappings.token`
    (different values) or `… mappings.lookup_hmac` (same value). Realistic case: two parallel
    PostToolUse hooks; the losing hook exits 1.
  - (read-only) The gateway lock covers opening the vault and the whole
    `_tokenize_request`: regex, LLM, person confirmation, encoder, vault and audit writes for
    every string. Title generation, the safety check and the main turn queue behind each
    other.
- **Fix:**
  - Vault: `sqlite3.connect(db_path, isolation_level=None, timeout=10)`; in
    `get_or_create_token` run `BEGIN IMMEDIATE`, repeat the lookup inside, read the counter,
    insert, `COMMIT`, `ROLLBACK` on exception.
  - Gateway: with that in place, drop `_vault_lock` from the `with`, or narrow it to a
    `_LockedVault(Vault)` that locks only inside `get_or_create_token`. Add a separate
    `_encoder_lock` (see D13).
- **Test:** 4 threads, one `Vault` each on the same `db_path`, mixing equal and different
  values: no exceptions, unique token numbers, same value → same token.

### R5. Tool-result continuations with a system-reminder create fake turns in the panel

- [ ] Done
- **Where:** `adapters/gateway/server.py:625-648, 880` (`_last_user_content_has_text`).
- **Evidence (confirmed):** a continuation carrying a `<system-reminder>` text block (todo
  reminders, hook context) counts as real text, is classified `"conversation"` with display
  text `[tool_result]`, takes over `_open_turn_by_session` (hook events go to the wrong
  screen), and later continuations fold under it.
- **Fix:** apply `_strip_injected_wrapper_tags` to each text block and count it as real only
  if something non-empty remains.

### R6. Empty bodies produce a premature end-of-chunks marker

- [ ] Done
- **Where:** `adapters/gateway/server.py:1103-1106, 1183-1186` (`_write_chunk`).
- **Evidence (confirmed):** an empty upstream body writes `0\r\n\r\n0\r\n\r\n`, corrupting a
  keep-alive connection. 204/304 responses also get a chunked body they must not have.
- **Fix:** `_write_chunk` is a no-op for empty data; 204, 304 and HEAD send no body and no
  `Transfer-Encoding`.

### R7. Fail-closed block returns 500, which Claude Code retries

- [ ] Done (needs decision 4)
- **Where:** `adapters/gateway/server.py:1367-1371`.
- **Evidence:** blocking is correct (tokenization error → 500, nothing forwarded; invalid
  JSON → 400), but Claude Code retries 5xx with backoff and each retry re-runs the full scan.
- **Fix:** 400 `invalid_request_error` (or 422) with a "veil blocked" message.

### R8. Panel endpoints: body validation and concurrent metrics runs

- [ ] Done
- **Where:** `adapters/gateway/server.py:1599-1614` (`/vault/reveal`), `1692`
  (`/hook-event`), `1660-1673` (POST `/metrics/comparison`).
- **Evidence (confirmed for the first two bullets):**
  - A non-integer `Content-Length` raises `ValueError` outside the `try`; a non-string
    `session_id` raises `sqlite3.ProgrammingError`. Both drop the connection with a
    traceback. A negative `Content-Length` makes `rfile.read(-1)` block. No size cap.
  - `/hook-event`'s `tool_name` is free text of any length.
  - The comparison runs for minutes in the request thread with no single-run guard; a double
    click starts two runs competing with live traffic for Ollama and the shared encoder.
- **Fix:** a `_read_json_body(handler, max_bytes=64_000)` helper returning 400/413; check
  `isinstance(session_id, str)`; validate `tool_name` with `^[A-Za-z0-9_.:-]{1,128}$`; a
  module-level `Lock.acquire(blocking=False)` returning 409 while a comparison runs.
  (Moving the comparison to a background thread is after-presentation; the panel is being
  rewritten anyway. Do not click "Run comparison" during the demo.)

### R9. Bodyless requests create panel turns

- [ ] Done
- **Where:** `adapters/gateway/server.py:1334, 1444-1467`.
- **Evidence (read-only):** a GET (e.g. `/v1/models`) publishes only response stages with
  the default `kind="conversation"`, creating a turn keyed by the response text and taking
  over the session's open-turn pointer.
- **Fix:** treat an empty `raw_body` as auxiliary (`internal_continuation`) or skip
  publishing.

### R10. Encoder spans bypass the non-PII name filter

- [ ] Done
- **Where:** `core/detector.py:1206-1214` (encoder leg in `detect`).
- **Evidence (confirmed with a fake encoder):** an encoder ORG span `Claude Code` in "Ask
  Claude Code to …" is kept, so the prompt shows `PII_ORG_xxxx` on stage.
- **Fix:** apply the `_NAME_LIKE_TYPES` / `_is_known_non_pii_name` filter to encoder spans.
- **Test:** a fake encoder returning `Claude Code`/ORG; assert it is dropped.

### R11. Hooks and `/veil` read stdin as cp1252 on Windows

- [ ] Done
- **Where:** `adapters/hooks/pre_tool_use.py:46`, `adapters/hooks/post_tool_use.py:45`,
  `adapters/command/sanitize.py:53`.
- **Evidence (confirmed):** without `PYTHONUTF8`, `José Muñoz` comes out as
  `JosÃ© MuÃ±oz`, both in tokenized output and in `updatedInput` (breaks
  queries with accents). `ask.sh` and `run_demo.sh` export `PYTHONUTF8=1`; a manual
  `claude --settings demo/settings.hooks.json` (as in the README) does not.
- **Fix:** `sys.stdin.buffer.read().decode("utf-8")` (and write stdout as UTF-8 too).
- **Test:** feed UTF-8 bytes with an accented name through each entry point.

### R12. Panel depends on a CDN at presentation time

- [ ] Done
- **Where:** `frontend/panel.html:268-270`; panel served at `server.py:1498`.
- **Evidence:** three scripts, nothing else external:
  - `https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js`
  - `https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js`
  - `https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.24.7/babel.min.js`
- **Fix:** save them under `frontend/vendor/`, add GET `/veil-panel/vendor/<name>` with a
  name whitelist and `Content-Type: application/javascript` (about 10 lines), switch the
  `src` attributes. Hatchling already ships non-`.py` package files. The new UI should reuse
  the route.

### R13. Script and hook-settings robustness

- [ ] Done
- **Where and fix:**
  - `scripts/ask.sh:109,115`: calls bare `claude`, which is not on Git Bash's `PATH` on this
    machine (`~/.local/bin/claude.exe` exists). Copy the lookup from `demo/run_demo.sh`.
  - `scripts/ask.sh:43`: `VEIL_GATEWAY_URL` is assigned but not exported, so hooks report to
    8787 when the gateway runs elsewhere. `export` it.
  - `demo/settings.hooks.isolated.json:58,69`: `cd "$VEIL_REPO_ROOT" && p=…; …` only guards
    the assignment; if `cd` fails the hook exits 127 (non-blocking), so Pre runs the command
    with unresolved tokens and Post passes raw output. Use `cd "$VEIL_REPO_ROOT" || exit 2;`.
  - `adapters/hooks/post_tool_use.py` failure message: "post_tool_use: tokenization failed"
    should say the untokenized output will reach the model. In hooks-only mode
    (`run_demo.sh`) the hook also runs regex-only unless `VEIL_LLM_*` are exported, and a
    Bash call with exit ≠ 0 probably fires `PostToolUseFailure` instead of PostToolUse
    (verify live). Have `run_demo.sh` warn when `VEIL_LLM_*` are missing. The gateway covers
    all of this in `ask.sh`.

### R14. Ollama model unload and timeouts during the demo

- [ ] Done
- **Where:** environment; `core/detector.py:610-613, 852-855, 1188-1196`.
- **Evidence (read-only):** Ollama unloads an idle model after 5 minutes; the reload exceeds
  the 10 s default timeout. A 5xx gets one retry (worst case about 20 s), connect/timeout
  errors none. On failure `detect()` silently falls back to regex-only, so names go out in
  clear. The comment "Single attempt, no retry" at line 610 is stale.
- **Fix (no detector code change):** run the demo with `OLLAMA_KEEP_ALIVE=-1` and
  `VEIL_LLM_TIMEOUT=30`, and add a warm-up call to `scripts/run-gateway.sh` that logs loudly
  if Ollama is unreachable. A strict mode that blocks instead of falling back is in the
  backlog (D7).

---

## Fix now: eval (before the benchmark re-run)

The seeded ground truth itself is correct: 2350 instances in 839 cells, every
`text[start:end] == value`, no repeated value with a single annotation, no overlaps, no extra
regex-detectable PII in evaluated cells.

### E1. Soft coverage rule credits partially leaked names

- [ ] Done
- **Where:** `demo/eval.py:261, 276-291` (`_SOFT_MIN_COVERAGE`).
- **Evidence (confirmed):** in 77 of 450 PERSON values one token alone covers ≥50%; with
  3-token names (367 of 450) two tokens usually suffice, and the third leaks. Conversely a
  name covered fully by three spans each under 50% counts as a miss.
- **Fix:** coverage = union of all overlapping soft spans; require ≥0.9 (or every
  alphanumeric character covered).
- **Test:** boundary cases; a name split into two spans is a hit and not a false positive.

### E2. One predicted span can match several gold values

- [ ] Done
- **Where:** `demo/eval.py:217-228`.
- **Evidence (confirmed):** one ENCODER span over `Rosa Pérez Gil is diabetic` counted as
  PERSON hit, SENSITIVE hit, and 2 ENCODER hits. A whole-cell span would take all soft recall
  with no false positive.
- **Fix:** each predicted span consumes at most one gold value (or cap span length at 2× the
  value); count per-detector hits per span.
- **Test:** a span over two gold values.

### E3. Decoys only count on exact span match

- [ ] Done
- **Where:** `demo/eval.py:369-370`.
- **Evidence (confirmed):** ORG on `billing department` inside the decoy
  `the billing department` gave 0 hits.
- **Fix:** any span overlapping `[offset, offset+len)` is a hit.
- **Test:** partial overlap with a decoy counts.

### E4. Exit codes, messages and options

- [ ] Done (needs decision 3)
- **Where:** `demo/eval.py:410-414, 487, 530-538, 546-547, 191, 360, 198`.
- **Evidence:**
  - `--help` says `--max-false-positives` applies only to `--mode regex`, but the decoy gate
    fails every mode (GLiNER with 2 decoys exits 1).
  - `--mode llm/union` prints "FAIL — recall below…" and exits 0; on stage that reads FAIL.
  - With Ollama down, `detect()` swallows the error: union mode prints 839 stderr warnings,
    PERSON recall 0, exit 0.
  - `threshold or DEFAULT_THRESHOLD` turns `--encoder-threshold 0` into 0.4, and
    `VEIL_ENCODER_THRESHOLD` is ignored with `--encoder`, contrary to `--help`.
  - The decoy pass (194 `detect` calls, about 5 minutes with qwen3.8) prints no progress;
    the main pass first reports at cell 25.
- **Fix:** non-regex modes print "report-only" for recall; decoy gate per decision 3;
  probe the endpoint up front and exit 2 if any layer call fails; `threshold if threshold is
  not None else env or default`; progress in `evaluate_decoys` and at cell 1.
- **Test:** `main()` with a fake `detect`: regex recall failure → 1; llm low recall → 0;
  decoy hit → per decision 3; `--encoder-threshold 0`.

### E5. Server fixtures take 0.5 s to shut down per test

- [ ] Done
- **Where:** `tests/test_detector.py:493-502`, `tests/test_gateway.py:180-206`.
- **Evidence (confirmed):** 0.51 s teardown per test in about 78 detector tests; two servers
  shut down in sequence in about 79 gateway tests. Roughly 1–1.5 min of the suite.
- **Fix:** `serve_forever(poll_interval=0.02)` and `server_close()` in the `finally`.

---

## After the presentation

### Gateway

- **Module split of `server.py`.** Timing:
  - Do R2–R9 first. They all touch `server.py`, and moving code that is already fixed and
    pinned by tests is safer than fixing code that was just moved.
  - Before the UI rewrite starts (Wednesday evening or Thursday morning, only if everything is
    green and there is time to spare), extract just `panel.py` and `pipeline_bus.py`. Those are
    what the new UI and option B (H8) will touch; adding endpoints to the current module first
    would only grow what has to move later.
  - The rest of the split waits until after the presentation.
  - Risk is moderate: `tests/test_gateway.py` is mostly black-box over HTTP and only touches
    `make_handler`, `PipelineEventBus`, a few constants and
    `_skip_reserved_anthropic_metadata` (deleted by G12). Watch out for
    `monkeypatch.setattr(gateway_server, "VAULT_DB_PATH" / "AUDIT_LOG_PATH", ...)`
    (`test_gateway.py:183-184`): once a constant moves to another module that patch silently
    stops working. The `GatewayContext` below removes the need for it.
  - Target layout:
    - `server.py`: `main()`, `make_handler`, `do_*` routing, `_handle`, plus a
      `GatewayContext` dataclass (client, upstream, audit log, bus, encoder, vault path)
      replacing module globals.
    - `forwarding.py`: `_HOP_BY_HOP`, request/response header building, `_write_chunk`,
      `_write_error`, `_resolve_session_id`, `SESSION_ID_HEADER`, upstream constants.
    - `claude_code_framing.py`: wrapper-tag regex, `_strip_injected_wrapper_tags`, title and
      safety markers, commit-trailer constant.
    - `request_sanitizer.py`: `_TOKENIZED_FIELDS`, the block walker (R2), `_excluded_ranges`,
      the vault locking (R4), `_tokenize_body`.
    - `response_rewriter.py`: `_TEXT_BEARING_DELTA_FIELDS`, `_split_sse_event`,
      `_SseResponseRewriter`, `_relay_sse_response`, `_relay_buffered_response`.
    - `display.py`: `_extract_*_text`, `_find_last_user_message_content`,
      `_last_user_content_has_text`, `_extract_system_text`, `_classify_cycle_kind`,
      `_extract_sse_display_text`, `_extract_buffered_display_text`.
    - `pipeline_bus.py`: `PipelineEventBus`, `_publish_stage`, `PIPELINE_STAGES`, history
      limits, `_AUXILIARY_CYCLE_KINDS`.
    - `panel.py`: panel routes as `(handler, ctx)` functions, `_write_json`, `_write_html`,
      `_write_sse_headers`, `_query_param`, the body helper (R8).
    - `metrics.py`: `_serialize_*`, `_run_comparison_mode`, `_run_encoder_comparison`, the
      comparison cache, the single-run guard.
- **G7. Known-values pass (leak, soft types).** Assistant text returns to Claude Code with
  names resolved; the next request relies on the LLM re-detecting them in new text. One miss
  sends an already-tokenized name in clear. After detection, also replace exact matches of
  this session's existing vault values.
- **G12. Dead billing-header skip.** `_skip_reserved_anthropic_metadata`
  (`server.py:551-555, 810`): the header lives in `system`, no longer tokenized since
  `d3a0690`. Inside `messages` it only lets through a tool result that starts with
  `x-anthropic-billing-header:`. Delete the predicate and the `skip=` argument. (Cheap; can
  go in with R2.)
- **G13. Request body edge cases** (`server.py:1326`): malformed `Content-Length` on the
  proxy path returns a misleading 502; a chunked request body is sent upstream empty and
  poisons keep-alive. Parse up front, return 400 / 411.
- **G14. Buffered path detokenizes `tool_use.input`** (`server.py:1194-1195`), while SSE
  leaves `input_json_delta` tokenized for PreToolUse. Resolve only `content[*].text`.
- **G15. Duplicate `server`/`date` headers** (`server.py:1429-1432`). Add them to the
  response skip set.
- **G16. Display-only `detect()` re-runs LLM and encoder** (`server.py:923-933`) and ignores
  `_excluded_ranges`, so the panel can highlight spans never tokenized. Use `span_sink` with
  a per-string id, or regex plus cache lookup only.
- **G17. Audit tail** (`server.py:1540-1545`): `readline()` can emit a half-written line
  (panel `JSON.parse` fails); a closed tab's thread sleeps until the next line. Buffer until
  `\n`; send `: ping` every few seconds.
- **G19. Globals** (`server.py:785-792, 1762-1767`): import `ENCODER_ENV_VAR` from
  `core.encoder` (its top-level imports are light now); pass `encoder` into `make_handler`.
- **G20. Stale docs:** `adapters/gateway/__init__.py:3-4` ("response relayed untouched"),
  `server.py:4-5` ("tokenizes every string leaf"), `server.py:336-342`
  (`_open_turn_by_session` is not pruned on eviction), README §9 lock latency.
- **G21.** Inline `_default_llm_client_for_display` and reuse one httpx client
  (`server.py:658-669`); HEAD/PATCH return 501 (`1740-1748`).

### Detector and encoder

- **D7. Strict mode.** CLAUDE.md says "block rather than forward", but any LLM failure falls
  back to regex-only (README §4 documents the floor). Add a strict mode that raises so
  adapters can block, and show fallbacks on the panel.
- **D9. IP false positive** if decision 2 goes the other way.
- **D10. SECRET false positives in code** (`detector.py:470-473, 363-368`):
  `api_key = os.environ.get('API_KEY')`, `field password: required`,
  `PWD=/home/alfonso/repos`, `password=${DB_PASSWORD}`. In `valid_secret` reject values with
  `(`, starting with `$`, `os.` or `self.`; drop `pwd` or make it case-sensitive.
- **D11. Truncated PEM** (`detector.py:466-469`): a private key cut before its END line is
  not detected. Make END optional and fall back to BEGIN plus the following base64 block.
- **D12. Unbounded caches** (`detector.py:936, 1078`): `_llm_span_cache` and
  `_person_confirmation_cache` keep plaintext for the process lifetime, and `detect_llm`
  returns the cached list itself. Capped LRU (`OrderedDict`, about 4096) and return
  `list(cached)`.
- **D13. Encoder thread safety** (`encoder.py:181-222`): no init race (`_ENCODER` is set
  before `serve_forever`), but `_predict` can run in parallel (display `detect()` outside the
  lock, metrics thread); `seconds`/`calls` counters race. Put a lock around `_predict`.
- **D14. Encoder details** (`encoder.py:322, 120, 102`): `device=0 if cuda else -1` sends
  `cuda:1`/`mps` to CPU silently (pass `device=resolved`); windows start mid-word (harmless,
  align to whitespace anyway); a 1200-character window of digits may exceed 512 subwords for
  `hf:` models (smaller chunks for `hf`).
- **D15. `_NAME_CANDIDATE_RE` uses `\s`**, so `Madrid\nJuan Pérez` can become one PERSON
  (over-tokenization). Use `[ \t]`.
- **D18. Comment bloat and stale text in `detector.py`:** history narration at 625–631,
  705–716, 868–883, 1070–1077, 1086–1097; stale: 144 ("seven check-digit-provable types",
  there are 13), 91, 521 ("future LLM union stage"), 610 ("no retry"), 4–9 (LM Studio,
  "longest span wins"). README also says "these seven types". Trim when the file is touched.
- **Encoder unit tests** (none today). A fake `EncoderBase` subclass with canned `_predict`:
  chunk overlap covers texts over 1200 characters without duplicates; `_relocate` fixes
  shifted offsets and drops spans whose text is absent; edge whitespace trimming;
  `_merge_adjacent`; `_map_label` with `B-`/`I-` and unknown labels; `build_encoder("bad")`
  raises; `encoder_from_env` returns `None` without the variable and reads the threshold;
  `detect()` survives an encoder that raises.

### Tokenizer, vault and audit

- **T5. Resolve returns the first-seen format** (`vault.py:240`): `ES91 2100 …` then the
  compact form share a token that resolves to the spaced form; same for email case. A
  detokenized SQL `=` may then miss the row. Document it (or store per-occurrence originals).
- **T6. Normalisation gaps** (`vault.py:97-111`): different tokens for NSS
  `28 12345678 40` vs `28/12345678/40`, CIF `B-12345678` vs `B12345678` (no uppercasing),
  PHONE `+34 612 345 678` vs `612345678`, PERSON case. Add NSS and CIF to the
  strip-separators-and-uppercase branch (incl. `/` and `.`), strip `+34`/`0034`. Extend
  `test_equivalent_values_share_token`. Changes tokens.
- **T7. 5-digit token look-alikes** (`tokenizer.py:54`): `detokenize("ref PII_EMAIL_00012")`
  → `'ref alice@example.com2'`. Add `(?!\d)` and hold `\d{4}$` in `_PENDING_TOKEN_TAIL`
  together; test in streaming char by char.
- **T8. `tokenize()` signature** (`tokenizer.py:96-108`): `use_llm=` and `span_sink=` have
  no production caller; `use_llm=False` also disables the encoder. Proposed shape:
  `tokenize(value, vault, *, detection: DetectionConfig | None, exclude: Callable[[str],
  Sequence[tuple[int, int]] | Literal[ALL]] | None, observer: TokenizeObserver | None)`,
  where `skip` becomes `exclude` returning `ALL` and `observer.on_span(span, token)` replaces
  `span_sink` and `audit_log` + `adapter`. Also remove the one-line `_default_llm_client`
  wrapper (check for monkeypatches first).
- **T11. Lookup HMAC is session-independent** (`vault.py:189-193`): equal values across
  sessions share `lookup_hmac`. Include `session_id` in the HMAC message; requires clearing
  `.veil/vault.db`.

### Hooks, command and scripts

- **H5. `_scope.is_demo_db_command` is too loose** (`adapters/hooks/_scope.py:30`): any
  command containing `veil_demo.db` is detokenized, and
  `demo/settings.hooks.isolated.json:45-47` auto-approves `Bash(python -c*)`, so a prompt
  injection could exfiltrate a resolved value. Case-sensitive (misses `VEIL_DEMO.DB`); the
  `PowerShell` tool is not covered by the `Bash` matcher. Require a leading `sqlite3 ` and no
  `;|&$\``; remove `Bash(python -c*)` from the allow list.
- **H7. Panel reporting latency** (`adapters/hooks/_panel_report.py:44`): best-effort and
  correct, but about 0.25 s per call with the gateway down (0.42 s with `localhost`), two
  calls per hook, and httpx honours `HTTP(S)_PROXY`. One call per hook, `trust_env=False`,
  0.05 s timeout.
- **H8. Option B design (hooks report before/after text).** Add optional `before`/`after`
  kwargs to `report()`, truncated to about 8 KB; accept them server-side with a length cap
  and put them on the bus event only (never in `audit.jsonl`). Data: Pre has `command` →
  `detokenized`; Post has `tool_response` → `tokenized`. Rewrite what pins the opposite:
  docstrings in `_panel_report.py:14-21`, `server.py:203-206`, `_serve_hook_event`; README
  lines 346 and 366; tests `test_gateway.py:1934` (exact key set) and `2017-2018`
  (`"command" not in`, `"12345678A" not in`).
- **H11. Empty key file** (`scripts/run-gateway.sh:18-21`): if python fails, `>` has already
  created an empty `vault.key` and later runs fail closed until it is deleted. Use
  `[ -s "$KEY_FILE" ]` and write to a temp file then `mv`. Port argument unvalidated.
- **H13. Unescaped values in shell commands** (`pre_tool_use.py:72`): resolved values are
  inserted without shell/SQL escaping. No current DB value contains `'"$\``, so no demo
  risk. Block (exit 2) when a resolved value contains `'"$\`\\` or a newline.
- **H15. Leftovers:** seven "BRIEF §…" references in `veil_pii_middleware/` and
  `README.md:483`, but `BRIEF.md` no longer exists; `_paths.py:9` calls `/veil`
  "now-closed"; verify the comment at `demo/settings.hooks.json:4`.
- **Panel (rewrite will replace it):** `reveal` has no `.catch` (~line 821); a literal NUL
  byte in a template literal at line 461 (use `\u0000`); `cycles` state is never pruned.

### Demo and eval

- **E10. Precision only measured on PII cells** (`eval.py:182`): 753 clean cells (e.g.
  `customers.city`, "No issues.") are never scanned, so soft false positives on clean text
  go unmeasured. Evaluate unannotated text columns too.
- **E11. Leftover batch mode** (`encoder.py:169-171, 184`, `docs/benchmarks.md`):
  `detect_batch`, `.seconds`, `.calls` are documented as read by `demo/eval.py`, which calls
  `detect()` per cell. Remove the claim or use `detect_batch`.
- **E12. 52 ungrammatical corpus sentences** (`seed.py:735-736`): `f2=phrase` also uses the
  third-person phrase ("they is a devout Catholic"). Add a "they" form to `_SENSITIVE_FACTS`
  or rewrite templates 1 and 3. Do with the benchmark re-run.
- **E13. `note()` fragile on repeated values** (`seed.py:169`): `text.find(value)` from 0.
  No case today. `assert text.count(value) == 1` and add a uniqueness test to
  `tests/test_demo.py`.
- **E14. Stale text:** `eval.py:7-10` ("Matching is strict"), `seed.py:67-68` ("until the LLM
  layer lands"), `seed.py:993-996` (unused `random.seed`).

### Tests

- **Dead-endpoint tests wait for a real Windows connection refusal**
  (`tests/test_detector.py:751, 831, 873`, 1–2 s each). Use `httpx.MockTransport` raising
  `ConnectError`; keep one real-port test.
- **Audit concurrency test** (`tests/test_audit.py:144`): now about 2 s (the 23 s in the
  plan came from a loaded machine: `time.sleep(0.005)` lasts about 15.6 ms on Windows, 750
  writes, fsync per line). Optionally 3×50 writes.
- **Fixed sleeps in SSE tests** (`tests/test_gateway.py:826, 886, 910`, `idle_timeout=1.5`):
  replace `sleep(0.3)` with a `threading.Event` set on subscribe; `idle_timeout=0.5`.
- **Slowest test:** `test_gateway_keeps_serving_after_a_mid_stream_upstream_failure` (10 s).

---

## Won't fix (documented or intentional)

- **Regex-only fallback when the LLM is down** (README §4). Revisit as D7.
- **`_PATTERNS` ordering (D16).** Exact ties checked: NSS vs CARD on 12 digits (NSS wins);
  `_PHONE_INTL` vs CARD on `00…` runs (PHONE wins, cards never start with 0); SECRET value
  equal to a structured value (structured wins, EMAIL special-cased the other way at
  583–590). DNI/CIF, NIE/CIF and IPv6/MAC cannot tie.
- **Validators (D17).** `valid_nss` matches the official two-case rule; `valid_cif`'s
  digit-only set ABEH is correct; L/M leniency only adds false positives.
- **`_NON_PII_NAMES` scope (D15).** Whole-span match is narrow and correct; only a customer
  named exactly "Claude", "Opus", "Sonnet" or "Haiku" is missed. Add a README note.
- **Synchronous metrics comparison in the panel (H10).** Part of the UI rewrite; do not
  click it during the demo (R8 adds a guard).
- **`/veil` leak through `<command-args>` (H14).** Documented in README §5 and §9; the
  sanitizer code itself is correct and fails closed.
- **Tests asserting implementation details (prompt phrases in `test_detector.py:535-571`,
  `_PATTERNS` sabotage in `test_decoys.py`).** The sabotage test is valuable. The audit hash
  test changes with F8.

## Checked and fine

- Vault: key read only from env or parameter, never echoed; fresh 96-bit `os.urandom(12)`
  nonce per encryption; AAD `session:type:token` with a row-swap test; another session's
  token raises `TokenNotFoundError` (lenient → placeholder).
- `IncrementalDetokenizer`: tokens split across chunks, an incomplete trailing `PII_`
  flushed as text, unknown tokens → placeholder, long uppercase runs capped at 32.
- Structure walk reaches every `str` leaf; dict keys and non-`str` values untouched.
- Audit: no plaintext in any field besides `value_hash` (F8); the integrity error prints
  only the token id; Windows locking works.
- Gateway SSE: UTF-8 characters split across byte chunks handled; tokens split across deltas
  held back and flushed before `content_block_stop`; `Content-Encoding`/`Content-Length`
  dropped and chunking redone; 4xx/5xx relayed with their status; `tool_result` content
  reached as string and as a list of text blocks.
- PreToolUse is fail-closed on every input error (exit 2, nothing on stdout).
  `hookSpecificOutput.updatedInput` and `updatedToolOutput` match the installed Claude Code
  2.1.322.
- Demo ground truth (see the eval section).
