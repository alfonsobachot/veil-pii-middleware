# Mission

## Objective

Implement the `PreToolUse` / `PostToolUse` hook adapters (BRIEF work-plan step
8, `docs/private/BRIEF.md` §2.1 rows "Model -> tool (re-injection)" and "Tool
-> model (tokenization)") to close the end-to-end demo loop for the one tool
the demo actually uses: `Bash` invoking `sqlite3` against
`demo/veil_demo.db`. `PreToolUse` de-tokenizes a `PII_<TYPE>_<NNNN>` token
back into its real value inside the Bash command before it executes (so a
query built around a token the model saw earlier still hits the real row).
`PostToolUse` tokenizes PII found in the query's output before it reaches the
model. Both reuse `core/tokenizer.py::tokenize()`/`detokenize()` and
`core/vault.py::Vault` unmodified, mirroring how the (now-closed, see
mission-5) `/veil` command adapter consumed them.

**Context carried over from mission-5 (`docs/missions/mission-5/`):** the
`/veil` slash command does not close the "user prompt -> model" leg —
Claude Code renders the raw typed argument into the transcript via
`<command-args>` independently of the script's stdout, confirmed by a live
human test. That finding is specific to the slash-command mechanism (no
JSON I/O contract, no `session_id`, argument text rendered by Claude Code
itself outside the script's control). Hooks are a structurally different
mechanism — JSON on stdin, JSON (`updatedInput`/`updatedToolOutput`) on
stdout, a real per-session `session_id` on every invocation — but this
mission's own human-verification step (see Validation Contract) exists
specifically because that difference must be confirmed live, not assumed
from how the previous adapter turned out.

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

## Intake Grill

| Question | Answer / Evidence |
|---|---|
| Which tool(s) should the hooks' `matcher` target? | `Bash` only, per BRIEF §4 ("Engine: SQLite — Claude Code queries it over Bash, no server to run") and confirmed by `README.md`/`BRIEF.md` — no other tool is used by the demo. User confirmed, and asked that `Read`-tool coverage be documented as a future improvement rather than built now (see Non-goals). |
| `PostToolUse`'s official docs confirm the output field `updatedToolOutput` but do not document the exact shape of the input `tool_response` for `Bash` (only one unconfirmed example seen: `{stdout, stderr, exit_code}`) — verified directly against `https://code.claude.com/docs/en/hooks.md` this session, not assumed. How should the hook tokenize it without hardcoding a shape that might be wrong? | Generic recursive strategy: deep-copy `tool_response` as-is and run the existing `tokenize()` over it directly — `tokenize()` already recurses through dicts/lists and only touches string leaves, keys and non-string scalars pass through untouched (`core/tokenizer.py`). Whatever the real shape turns out to be, every string value in it gets scanned; no field name is hardcoded or assumed. |
| `PreToolUse`'s `updatedInput` merge semantics (full replace vs. partial patch) are undocumented. How should the hook build it? | Full-object replacement: copy the entire original `tool_input` dict and overwrite only the `command` key with the detokenized string. Safe under both a "replace" and a "merge" interpretation of `updatedInput`, since the copy is already a complete, valid `tool_input`. |
| If `detokenize()` fails in `PreToolUse` (vault won't open, unknown/foreign/malformed token) — not a PII leak (the risk is a broken command, not exposed data) — what should happen, given `CLAUDE.md`'s fail-closed doctrine? | Block: exit `2` (deny), short non-PII-bearing stderr message. Confirmed via the official docs that exit `2` blocks regardless of any JSON printed, so no conflicting JSON is emitted on this path. |
| `PostToolUse` cannot block (the tool already ran, confirmed via docs: "no blocking; shows stderr to Claude") — if this hook fails, the *unmodified, untokenized* `tool_response` reaches the model as-is. Is this gap accepted for this mission? | Yes, accepted and documented — matches BRIEF §2.1's own framing of "hooks mode: ~90% coverage... gateway: 100%, fail-closed by construction." No mitigation is added (no audit-log write, `core/audit.py` stays an unimplemented stub per its own scope) — this is out of scope, tracked as a residual risk in `handoff.md`, not solved here. |
| A `Bash`-matcher `PreToolUse`/`PostToolUse` pair, if merged into this repo's own `.claude/settings.json` (which already carries harness meta-development hooks with a `Bash` matcher), would intercept **every** Bash call in every future session on this repo — including plain `git status`/`pytest`/`ruff` calls — and, with fail-closed `PreToolUse` active, block them whenever `VEIL_VAULT_KEY` isn't set (the normal state outside an actual demo run). Where should the demo's hook config live, and should the scripts themselves be scoped? | Both layers, confirmed by the user after this trade-off was explained: (1) a separate, **not auto-loaded** settings fragment (`demo/settings.hooks.json`) activated only via `claude --settings demo/settings.hooks.json` for an actual demo run — verified this session that `--settings` merges additively per-key ("keys you omit keep their file-based values") and accepts a partial JSON object, so this file only needs a `hooks` key; `.claude/settings.json` itself is not touched. (2) The hook scripts are themselves content-aware: a shared `is_demo_db_command()` check no-ops (passthrough, no vault touched) on any Bash command that isn't a `sqlite3` invocation against `demo/veil_demo.db`, so even a future accidental merge into shared settings degrades gracefully instead of blocking unrelated Bash calls. |
| Script layout? | Two separate scripts — `adapters/hooks/pre_tool_use.py` and `adapters/hooks/post_tool_use.py`, each with its own `main()`, symmetric with `adapters/command/sanitize.py`. (No single dispatch-by-`hook_event_name` script — a settings.json hook entry has no way to pass an event-name argv flag to it without re-opening the argv-interpolation risk class BRIEF §6 already flags for a different adapter.) A small shared `adapters/hooks/_scope.py` holds only the passive `is_demo_db_command()` check used by both — not a dispatch mechanism, just avoiding two copies of a safety-relevant check drifting apart. |
| Where does `session_id` come from? | The hook's own stdin JSON `session_id` field — confirmed present and stable for the whole session on both `PreToolUse` and `PostToolUse` via the official docs, fetched and verified this session (not assumed from BRIEF or from how the `/veil` mission's env-var fallback had to work around its absence). No fallback needed; this supersedes `/veil`'s `CLAUDE_SESSION_ID`/`veil-demo` workaround for this interception point specifically. |
| Where does the vault live? | Same fixed, git-ignored path as `/veil`: `.veil/vault.db` (relative to cwd, which is the project root when Claude Code invokes a hook). No new env var; `VEIL_VAULT_KEY` unchanged. |

Evidence gathered from the repo/docs before asking: `core/tokenizer.py` and
`core/vault.py` are unchanged and already tested. `docs/private/BRIEF.md` §2.1
names `updatedInput`/`updatedToolOutput` as the exact mechanism, confirmed
verbatim against `https://code.claude.com/docs/en/hooks.md` this session
(fetched twice to pin down the Decision Control table, which is not fully
inlined on first fetch). `.claude/settings.json` already carries harness-only
hooks (`protect-paths.sh`, `mission-state-guard.sh`, `grill-guard.sh`,
`format.sh`, `verify-gate.sh`) with a `Bash` `PreToolUse` matcher for its own
purposes — informed the settings-placement question above.
`.claude/settings.local.json.example` establishes this repo's existing
convention for an opt-in, non-committed settings file, though the demo's
hooks still need a *separate* file rather than that one, since
`settings.local.json` auto-loads for every session on the machine, which
does not solve the blast-radius problem either.

## Scope

- `veil_pii_middleware/adapters/hooks/pre_tool_use.py` (new): `main()` reads
  one JSON object from `sys.stdin` (the `PreToolUse` payload; a
  non-JSON-object top-level payload is treated as a parse failure, same
  code path as malformed JSON). Passes through unchanged (exit `0`, no
  stdout) when `tool_name != "Bash"` or
  `is_demo_db_command(tool_input["command"])` is false. Otherwise opens
  `Vault(".veil/vault.db", session_id)` (`session_id` from the payload),
  calls `detokenize(command, vault)`, and on success writes
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {**tool_input, "command": detokenized}}}`
  to stdout and exits `0`. **Deliberately never emits `permissionDecision`** —
  see "Review findings and fixes" below; a code-review pass caught that
  pairing `permissionDecision: "allow"` with a bare substring scope check
  would auto-approve an entire (possibly chained/injected) command, not just
  rewrite it. Any exception (JSON parse, non-object payload, vault open,
  resolve failure) is caught, produces a short non-PII stderr message, no
  stdout, and exits `2`.
- `veil_pii_middleware/adapters/hooks/post_tool_use.py` (new): `main()` reads
  the `PostToolUse` JSON payload (same non-object-payload handling as
  above), passes through unchanged (exit `0`, no stdout) when
  `tool_name != "Bash"` or `is_demo_db_command(...)` is false on
  `tool_input["command"]`. Otherwise opens the same fixed vault, calls
  `tokenize(tool_response, vault)` directly on the whole (arbitrary-shaped)
  `tool_response` value, and on success writes
  `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": <tokenized tool_response>}}`
  to stdout and exits `0`. On any exception, prints a short non-PII stderr
  message and exits non-zero, with **no** stdout — the accepted, documented
  gap (see Intake Grill) is that the tool's original, untokenized response
  still reaches the model on this path, because `PostToolUse` cannot block.
- `veil_pii_middleware/adapters/hooks/_scope.py` (new): one function,
  `is_demo_db_command(command: str) -> bool`, checking for the demo db's
  literal filename (`veil_demo.db`) in the command string. Shared by both
  scripts above; not a dispatch mechanism. Documented in its own docstring
  as a rewrite gate only, never an approval gate.
- `veil_pii_middleware/adapters/hooks/_paths.py` (new): the shared
  `VAULT_DB_PATH` constant, so the vault location can't drift between the
  two hook scripts (added during the code-review pass; the original scope
  had each script define its own copy).
- `demo/settings.hooks.json` (new): a partial Claude Code settings JSON
  containing only a `hooks` key with `PreToolUse`/`PostToolUse` entries
  matching `Bash`, invoking the two scripts above via the same
  venv-`python.exe`-with-POSIX-fallback pattern `.claude/commands/veil.md`
  already uses. Not referenced by `.claude/settings.json` — activated only
  via `claude --settings demo/settings.hooks.json`.
- `README.md`: a short new section documenting the one command needed to run
  the demo with hooks active, and the one-line trade-off that doing so
  replaces (not merges into) this session's `hooks` key, so the harness's own
  meta-development hooks (`protect-paths.sh` etc.) do not run during a demo
  session — acceptable since a demo run doesn't edit repo files.
- `tests/test_hooks.py` (new): both scripts run as real subprocesses (argv
  list form, JSON payload built in Python and passed via stdin — never a
  constructed shell string), covering: a demo-db `Bash` command carrying a
  known token gets de-tokenized in `updatedInput.command` and exit `0`; the
  same command with a foreign/unknown token exits `2` with empty stdout; a
  non-`Bash` tool or a non-demo-db `Bash` command passes through with no
  stdout and exit `0` for both hooks; a demo-db tool response containing
  known PII (in a nested dict/list shape, to prove the generic recursive
  strategy, not a hardcoded `stdout` key) comes back tokenized under
  `updatedToolOutput` with exit `0`; a vault-open failure (`VEIL_VAULT_KEY`
  unset) on a demo-db command yields exit `2`/empty stdout for `PreToolUse`
  and a non-zero exit with no `updatedToolOutput` in stdout for
  `PostToolUse`; `.gitignore` still covers `.veil/`.

## Non-goals

- `Read`-tool coverage — documented as a future improvement, not built now
  (user's explicit choice).
- `adapters/gateway/` — untouched.
- `frontend/` — untouched.
- `core/audit.py` — stays an unimplemented stub; neither hook writes to it.
- Merging the demo's hooks into `.claude/settings.json` or
  `.claude/settings.local.json.example` — the whole point of this mission's
  settings-placement decision is that they do *not* go there.
- `UserPromptSubmit` / `MessageDisplay` hooks — separate BRIEF rows (day-2
  items), not this mission.
- Any change to `core/detector.py`, `core/vault.py`, or `core/tokenizer.py` —
  consumed as-is.
- Reconciling the `/veil` command's `CLAUDE_SESSION_ID`/`veil-demo` fallback
  session id with the hooks' real `session_id` — the two adapters are not
  exercised together in this mission's scope (`/veil` is closed per
  mission-5); no cross-adapter vault-session consistency work is done here.
- Resolving `PostToolUse`'s inherent inability to block on failure — this is
  a documented platform limitation, not a bug to fix; accepted per the Intake
  Grill answer above.

## Acceptance criteria

1. A `Bash` command referencing `demo/veil_demo.db` and containing a token
   this session's vault knows resolves to the real value in `updatedInput`
   before the (real or test-simulated) tool executes.
2. A `Bash` command's demo-db query result gets PII-bearing strings replaced
   with `PII_<TYPE>_<NNNN>` tokens in `updatedToolOutput`, for a nested JSON
   shape, not just a flat `stdout` string.
3. Any `Bash` command that is not a demo-db query, and any non-`Bash` tool
   call, passes through both hooks with no modification and exit `0`.
4. A `PreToolUse` failure (bad/foreign token, vault won't open) blocks the
   command (exit `2`, empty stdout) rather than letting a literal
   `PII_..._NNNN` string execute against the real database.
5. `demo/settings.hooks.json` is not referenced anywhere in
   `.claude/settings.json`/`.claude/settings.local.json.example` and is only
   activated by an explicit `--settings` invocation, documented in
   `README.md`.
6. `.harness/bin/verify` exits `0`.
7. A human runs the actual demo with real hooks active in a live Claude Code
   session (matching this mission's own charge to verify hooks' behavior
   live rather than assume it from the `/veil` mission's outcome) and
   confirms `updatedInput`/`updatedToolOutput` behave as this mission's
   scripts assume — including that Claude Code still applies `updatedInput`
   when `permissionDecision` is left unset (see "Review findings and fixes")
   — recorded in `handoff.md` regardless of outcome.
8. `pre_tool_use.py` never emits `permissionDecision` under any code path —
   a match on `is_demo_db_command()` only ever causes a rewrite, never an
   auto-approval of the command.

## Risk

Risk class: high

Both hooks sit directly in a live tool-execution path: `PreToolUse` rewrites
the actual Bash command Claude Code is about to run against a real (if
synthetic) database, and a bug here could execute something other than what
the model intended, or silently fail to de-tokenize (breaking the query,
not leaking data). `PostToolUse` is the layer meant to keep PII out of the
model's context — a bug here has a direct PII-leak blast radius, mitigated
only by the demo database being fully synthetic (per `CLAUDE.md`/BRIEF §4,
no real customer data ever flows through this code path). Per `CLAUDE.md`'s
own review policy, this touches the high-risk surface even though `core/`
itself isn't modified, because it's the first adapter that can rewrite a
real tool call's input and a real tool call's output, not just a prompt
string.

## Merge policy

No commit, push, or merge without explicit human approval. This mission
does not commit on its own (per repo convention/memory: the user handles
their own commits).

## Review findings and fixes

An independent `code-reviewer` pass (fresh context, read-only) on the first
implementation found two blocking issues before any commit happened:

1. **Fail-closed gap:** both scripts only wrapped `json.loads(...)` in
   `try`/`except`, not the shape check — a syntactically valid but
   non-object top-level payload (`[1,2,3]`, `null`, a bare string, `true`)
   raised an uncaught `AttributeError` on the first `.get()` call, exiting
   with Python's default code `1` instead of the documented fail-closed
   contract. **Fixed:** an `isinstance(payload, dict)` check now runs inside
   the same `try` block, so it's caught by the existing handler. Locked in
   by `tests/test_hooks.py`'s `test_non_object_top_level_payload_*` cases
   (parametrized over `[1,2,3]`/`None`/a string/`True`) for both scripts.
2. **Permission-model gap (the more serious one):** `pre_tool_use.py`
   originally emitted `"permissionDecision": "allow"` whenever
   `is_demo_db_command()` matched — but that check is a bare substring test,
   not proof the command *only* invokes `sqlite3` against the demo db. A
   compound/chained command (e.g. `rm -rf ~/important; sqlite3
   demo/veil_demo.db "select 1"`) would also match, and
   `permissionDecision: "allow"` would auto-approve that *entire* command,
   skipping Claude Code's normal ask-permission prompt — the same failure
   class `docs/missions/mission-5/handoff.md` already documents fixing once
   for the `/veil` adapter (removing an `allowed-tools` wildcard grant for
   the identical reason). **Fixed:** `permissionDecision` is no longer
   emitted at all; `pre_tool_use.py` only ever returns `updatedInput`, so a
   match still leaves the (rewritten) command subject to the normal
   ask-permission flow rather than skipping it.

This introduces one more residual risk on top of the two already flagged in
the Tooling Assessment: **whether Claude Code actually applies
`updatedInput` when `permissionDecision` is left unset is itself
undocumented** (the official hooks reference shows `updatedInput` only in
examples that also set `permissionDecision: "allow"`). If it turns out
`updatedInput` is silently ignored without an accompanying
`permissionDecision`, this mission's `PreToolUse` de-tokenization would be a
no-op in practice even though every unit test here passes (the tests only
exercise the script's own stdout contract, not Claude Code's interpretation
of it) — this is exactly the class of assumption the live human test in
acceptance criterion 7 exists to catch before this is considered done.

A second, targeted `code-reviewer` re-check (fresh context) confirmed both
fixes by direct reproduction, not just by reading the fix: all four
non-object payload variants (`[1,2,3]`, `null`, `"5"`, `true`) piped via
stdin to each script produced the documented exit code and stderr with no
unhandled traceback, and `permissionDecision` no longer appears in any
executable path of `pre_tool_use.py` (only in docstring/comment prose
explaining its deliberate absence, and in the test asserting that absence).
See `handoff.md` for the full verdict.

## Tooling And Approach Assessment

1. **Project type:** Python middleware adding two new Claude Code hook
   scripts (stdin/stdout JSON contract) plus a settings fragment and README
   doc update — no new runtime dependency, no build-system change.
2. **What that implies:** the scripts must be tested the same way
   `sanitize.py` already is — real subprocess invocation with a
   Python-constructed JSON payload piped via stdin, argv fixed, never a
   shell string built from untrusted content — because these are the second
   real external-facing transport this project ships (after the now-closed
   `/veil`), and the same command-injection risk class BRIEF §6 already
   flags applies to *how the settings fragment invokes them*, not just to
   the scripts' own internals.
3. **Risks or missing decisions this reveals:** (a) `PostToolUse`'s
   input/output shape for `Bash` is not fully documented upstream — mitigated
   by the generic recursive `tokenize()`-over-arbitrary-JSON strategy already
   decided above, but still needs the live human test to confirm Claude Code
   actually honors `updatedToolOutput` the way the docs describe (same class
   of risk mission-5 got burned by with `$ARGUMENTS`). (b) `--settings`'s
   additive-merge semantics were fetched and confirmed this session
   ("keys you omit keep their file-based values") but not live-tested against
   a real `hooks` key specifically — flagged as a secondary residual risk in
   `handoff.md`, since a full top-level-key replace vs. a deep merge of the
   `hooks` object's nested arrays would change whether harness meta-hooks
   silently keep running during a demo session or not. (c) Windows: hook
   commands run via Git Bash per the docs; the venv-python resolution needs
   the same `.venv/Scripts/python.exe` / `.venv/bin/python` fallback
   `veil.md` already uses.
4. **What should be added to validation:** subprocess-level tests for both
   scripts covering pass-through, success, and every documented failure
   mode; a static check that neither script imports
   `subprocess`/`os.system`/uses `shell=True`; a check that
   `demo/settings.hooks.json` is not referenced from the two committed
   settings files; the mandatory human live-hook-test, recorded regardless
   of outcome.
5. **What is explicitly not needed now:** no new pyproject dependency, no
   mypy config change (existing `strict` settings already cover
   `veil_pii_middleware`/`tests`), no gateway/frontend/audit work, no CI
   pipeline change.

## Status

closed — implementation complete, `.harness/bin/verify` green, code review
findings fixed and re-verified. Acceptance criterion 7's live test is
closed for its security-relevant half (confirmed live, both by the user's
own manual run and by a real headless `claude -p` session: `PostToolUse`
tokenizes real query output in a real session, and `PreToolUse` omitting
`permissionDecision` does not cause auto-approval). One narrow, low-severity
piece stays open by the user's explicit choice not to chase it further: 
whether `updatedInput`'s rewritten command is what actually *executes* once
a call is approved (a broken-query risk, not a leak risk, since a wrong
command would simply fail against the real database rather than silently
run). See `handoff.md` for full detail. The user is moving on to a new
mission for `adapters/gateway/` rather than continuing to harden this one.
