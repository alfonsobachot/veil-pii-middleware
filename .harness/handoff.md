# Handoff

Mission 6: `PreToolUse`/`PostToolUse` hooks (BRIEF work-plan step 8).

## Summary

Implemented two Claude Code hook scripts that close the end-to-end demo loop
for the one tool the demo uses (`Bash` invoking `sqlite3` against
`demo/veil_demo.db`):

- `pre_tool_use.py` de-tokenizes a `PII_<TYPE>_<NNNN>` token inside a
  demo-db `Bash` command back into its real value before the command runs,
  via `core/tokenizer.py::detokenize()` against a per-session
  `core/vault.py::Vault`. Blocks (exit `2`) on any failure — bad/foreign
  token, vault won't open, malformed hook payload.
- `post_tool_use.py` tokenizes the whole `tool_response` value of a
  demo-db `Bash` call generically (via `tokenize()`, which recurses through
  arbitrary dicts/lists and only touches string leaves) since Claude Code's
  official hooks reference doesn't fully document `tool_response`'s shape
  for `Bash`. Cannot block (the tool already ran, per the hook contract);
  on failure it prints a stderr diagnostic and exits non-zero, but never
  fabricates or partially emits `updatedToolOutput`.
- Both scripts no-op (no vault touched, exit `0`, no stdout) for any tool
  call that isn't a demo-db `Bash` invocation, via a shared
  `is_demo_db_command()` check in `_scope.py`.
- The hooks are wired up in `demo/settings.hooks.json`, a partial settings
  file **not** referenced by `.claude/settings.json` or
  `.claude/settings.local.json.example` — activated only via
  `claude --settings demo/settings.hooks.json` for an actual demo run, so
  every other Bash call on this repo (including this harness's own
  meta-development sessions) is unaffected.
- `README.md` documents that activation command and the trade-off it
  carries (see below).

`.harness/bin/verify` passes (contract, lint, typecheck, test, eval, build —
all 6 green). `tests/test_hooks.py` has 23 passing tests, all real
subprocess invocations with a Python-constructed JSON payload piped via
stdin, matching the convention `tests/test_sanitize.py` already established
for the `/veil` adapter.

## Review cycle (important — read before treating this as done)

An independent `code-reviewer` pass (fresh context, read-only) on the first
implementation found two **blocking** issues before anything was committed.
Both are fixed and a second, targeted `code-reviewer` re-check confirmed the
fixes by direct reproduction (not just by reading the diff). Full detail is
in `.harness/mission.md`'s "Review findings and fixes" section; summary:

1. **Fail-closed gap:** a non-object top-level JSON payload (`[1,2,3]`,
   `null`, a bare string, `true`) crashed both scripts with an uncaught
   `AttributeError` (exit code `1`, not the documented fail-closed
   contract). Fixed by validating `isinstance(payload, dict)` inside the
   same `try` block. Verified by direct reproduction: all four payload
   variants now produce the documented exit code and stderr, no traceback,
   for both scripts. Locked in by `tests/test_hooks.py`'s
   `test_non_object_top_level_payload_*` cases.
2. **Permission-model gap (the more serious one):** `pre_tool_use.py`
   originally emitted `"permissionDecision": "allow"` on every match of
   `is_demo_db_command()` — a bare substring check on `"veil_demo.db"`
   anywhere in the command, not proof the command *only* invokes `sqlite3`
   against that db. A compound/chained command (e.g. `rm -rf ~; sqlite3
   demo/veil_demo.db "select 1"`) would also match, and
   `permissionDecision: "allow"` would have auto-approved that *entire*
   command, skipping Claude Code's normal ask-permission prompt — the same
   failure class `docs/missions/mission-5/handoff.md` already documents
   fixing once for the `/veil` adapter (removing an `allowed-tools`
   wildcard grant for the identical reason: an auto-approve mechanism
   riding on a pattern match that doesn't actually constrain the whole
   command line). Fixed by removing `permissionDecision` entirely from
   `pre_tool_use.py`'s output — it now only ever returns `updatedInput`,
   leaving the (rewritten) command subject to the normal ask-permission
   flow. Verified: `permissionDecision` no longer appears in any executable
   path of `pre_tool_use.py` (grep confirms it exists only in
   docstring/comment prose and in the test asserting its absence).

This second fix introduces a **new residual risk**, not present before: it
is undocumented whether Claude Code actually applies `updatedInput` when
`permissionDecision` is left unset — the official hooks reference only
shows `updatedInput` in examples that also set `permissionDecision:
"allow"`. If Claude Code silently ignores `updatedInput` without an
accompanying `permissionDecision`, this mission's `PreToolUse`
de-tokenization would be a no-op in a real session even though every unit
test here passes (the tests only exercise the script's own stdout contract,
never Claude Code's interpretation of it). This is now the single most
important thing the live human test below needs to check.

## Residual risks (all require the live human test to close)

1. **`PostToolUse`'s `tool_response` shape for `Bash` is not fully
   documented upstream.** Only one example was seen (`{stdout, stderr,
   exit_code}`), unconfirmed as exhaustive. Mitigated by design:
   `post_tool_use.py` never assumes a field name, it hands the whole value
   to `tokenize()`. **Confirmed live (2026-09-07):** a real query against
   `demo/veil_demo.db` (email lookup, `users` table) came back with the
   email tokenized as `PII_EMAIL_0001` in what the model actually saw —
   `updatedToolOutput` is honored by Claude Code in practice, not just in
   this repo's own tests. One side observation from that same run, not a
   bug in this mission: the customer's full name (`Rosendo Cifuentes
   Rojas`) reached the model in plain text, because full names are only
   caught by the probabilistic LLM layer (BRIEF SS3.1), which is a
   day-2/not-yet-built item — `detect_regex()` was never going to catch a
   bare name, and nothing in this mission's scope claims otherwise.
2. **`--settings`'s merge semantics for the `hooks` key specifically are
   untested live.** Confirmed via the official CLI reference that
   `--settings` merges additively per top-level key ("keys you omit keep
   their file-based values"), but not confirmed whether `hooks` is replaced
   wholesale or deep-merged with `.claude/settings.json`'s own `hooks`
   entries. If it's a deep merge instead of a replace, the harness's own
   meta-development hooks would keep running during a demo session (not
   harmful, just not what the README currently documents).
3. **(New, from the review cycle) whether `updatedInput` is honored without
   `permissionDecision`.** See above — this is the one most likely to
   silently break the demo if wrong, since a no-op de-tokenization would
   look identical to a working one unless someone checks the actual
   executed command. **Confirmed live (2026-09-07), the auto-approval half
   of this risk:** ran a real headless `claude -p` session (not a subagent,
   not this repo's own test suite) against this repo with
   `--settings demo/settings.hooks.json` and `--permission-prompts none`
   (denies anything needing a prompt — no `--allowedTools`/bypass grant, so
   nothing masks the result). A `Bash` command referencing
   `demo/veil_demo.db` was denied with "this session has no approval
   surface" — i.e. `pre_tool_use.py` emitting no `permissionDecision` did
   **not** cause Claude Code to silently auto-approve the call. This closes
   the security-relevant half of this risk. **Still open:** whether
   `updatedInput`'s rewritten `command` is what actually *executes* once a
   call *is* approved (mechanical correctness, not a leak risk) — blocked
   on needing either an interactive human approver or `bypassPermissions`,
   which this session's own auto-mode classifier correctly refused to let
   me use recursively on myself (spawning a permission-bypassed nested
   session is a real privilege-escalation pattern, not something to route
   around). The user's own manual test (see below) already exercised
   `PostToolUse` live; a token-reuse follow-up query in an interactive
   session would close this last piece.

**Status as of this handoff:** risk 1 (`PostToolUse`/`updatedToolOutput`)
confirmed live by the user, see above. Risks 2 (`--settings` merge
semantics for `hooks`) and 3 (`updatedInput` honored without
`permissionDecision`, i.e. whether `PreToolUse`'s de-tokenization actually
works and still goes through the normal ask-permission prompt) are **still
unconfirmed** — the live test so far only exercised a first query (tool
output tokenized), not a follow-up query reusing the returned token (which
is what would exercise `PreToolUse`). `.harness/mission.md`'s acceptance
criterion 7 and `.harness/validation.md`'s User-Testing Validator are
therefore partially, not fully, satisfied. This is not being chased further
in this session at the user's request — pick it back up whenever there's
appetite for it, using `demo/run_demo.sh` (see below) to start.

## Files changed

- `veil_pii_middleware/adapters/hooks/pre_tool_use.py` — new: `PreToolUse`
  hook, de-tokenizes demo-db Bash commands, fail-closed, never emits
  `permissionDecision`.
- `veil_pii_middleware/adapters/hooks/post_tool_use.py` — new: `PostToolUse`
  hook, tokenizes demo-db Bash responses generically, cannot block on
  failure (documented gap).
- `veil_pii_middleware/adapters/hooks/_scope.py` — new: shared
  `is_demo_db_command()` substring check, documented as a rewrite gate
  only, never an approval gate.
- `veil_pii_middleware/adapters/hooks/_paths.py` — new: shared
  `VAULT_DB_PATH` constant (added during the review cycle to stop the path
  being duplicated across the two scripts).
- `demo/settings.hooks.json` — new: partial, non-auto-loaded settings
  fragment wiring both hooks to the `Bash` matcher.
- `demo/run_demo.sh` — new: added after the first live-test attempt showed
  the activation command was more friction than estimated when the mission
  was approved — locates the `claude` binary (the VSCode extension bundles
  its own copy under a version-numbered path, not on `PATH`) and runs it
  with `--settings demo/settings.hooks.json`. Must be run from a real
  terminal (Git Bash): the VSCode extension's own panel has no setting
  that exposes `--settings`, confirmed by reading its
  `contributes.configuration` in `package.json` — a session already
  running inside the panel cannot switch the demo hooks on.
- `README.md` — new "Running the demo (hooks)" section documenting
  `demo/run_demo.sh`, the `--settings` merge trade-off, and the
  terminal-only limitation above.
- `tests/test_hooks.py` — new: 23 tests, all real subprocess invocations.
- `.harness/mission.md`, `.harness/validation.md` — written for this
  mission (previously held mission-5's `/veil` content, now archived to
  `docs/missions/mission-5/`).

## Closure decision (2026-09-07)

Closed by the user's explicit choice, without chasing the last narrow item
further. State at closure:

- `bash .harness/bin/verify`: green (6/6 gates).
- Code review cycle: 2 blocking findings fixed, re-verified by direct
  reproduction (see "Review cycle" above).
- Residual risk 1 (`PostToolUse`/`tool_response` shape): confirmed live —
  by the user's own manual run and by a real headless `claude -p` session.
- Residual risk 3 (`updatedInput` without `permissionDecision` — the
  security-relevant question, does this silently auto-approve anything):
  confirmed **closed** live, via a real headless `claude -p` session with
  `--permission-prompts none` and no tool allowlist — a demo-db `Bash` call
  was denied ("no approval surface"), proving nothing is being
  auto-approved.
- Residual risk 3's narrower mechanical half (does the rewritten `command`
  in `updatedInput` actually *execute*, once a call is approved by a human)
  stays **unconfirmed** — a broken-query risk, not a leak risk, if it turns
  out wrong. Not chased further because the user is moving to the gateway
  mission instead; carried forward as a known gap, not a resolved one.
- Residual risk 2 (`--settings`'s merge semantics for the `hooks` key
  specifically): still unconfirmed, not blocking, low-stakes (worst case:
  the harness's own meta-development hooks keep running during a demo
  session, which is harmless, just undocumented-as-such).

**Open question raised at closure, not yet decided:** once
`adapters/gateway/` exists, do these two hook scripts stay in the
architecture as a low-latency, defense-in-depth layer for the `Bash` leg
specifically (per BRIEF SS2.1's own framing: hooks mode is a legitimate,
lower-friction interception point alongside the gateway, not merely a
demo scaffold), or does the gateway supersede them entirely, making
`adapters/hooks/`, `demo/settings.hooks.json`, and `demo/run_demo.sh` dead
weight to remove later? Not decided in this session — flagged for whoever
picks up the gateway mission to revisit once that mechanism exists and its
own coverage is known.

## Follow-ups (not done, for a future mission if wanted)

- **Next up, per the user's explicit direction: `adapters/gateway/`**
  (BRIEF work-plan day-2 items #9-10). This is the mechanism that actually
  closes the "raw user prompt -> model" leg (which nothing built so far
  does — `/veil` is closed/broken per mission-5, hooks only ever saw tool
  calls, never the typed prompt) and the one that would work
  friction-free from every Claude Code surface including the VSCode
  extension panel, since it's an `ANTHROPIC_BASE_URL` environment variable
  rather than a CLI startup flag. Two risks the BRIEF itself already flags
  as unresolved and worth spiking first: (1) whether `ANTHROPIC_BASE_URL`
  works with a subscription login or requires a separate console API key;
  (2) SSE streaming means a token like `PII_EMAIL_0001` can arrive split
  across chunks, needing a buffering strategy before substitution.
- `Read`-tool coverage for the hooks (explicitly deferred by the user
  during intake — BRIEF's demo only uses `Bash`, so this wasn't built).
- The mechanical `updatedInput`-execution question above, if the hooks are
  kept once the gateway exists.
- Deduplicating `VAULT_DB_PATH` across all three adapters, including the
  (now-closed) `/veil` command adapter's `sanitize.py` — this mission only
  deduplicated it between the two hook scripts, since `adapters/command/`
  was out of scope here.
- `UserPromptSubmit`/`MessageDisplay` hooks (separate BRIEF rows, day-2
  items) — not touched.
- `core/audit.py` — still an unimplemented stub; neither hook logs to it.
