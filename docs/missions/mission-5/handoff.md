# Handoff

## CONFIRMED: command injection via `$ARGUMENTS` text-splicing (2026-09-07)

**This supersedes the "residual risk" framing below — it is no longer an open
question, it is a proven, working remote-code-execution vector.**

The human ran both adversarial payloads this mission's own risk analysis had
predicted but left unresolved:

```
/veil hola" ; touch /tmp/veil-marker-b ; echo "fin
/veil hola $(touch /tmp/veil-marker-a) mundo
```

Both created their marker file. This proves Claude Code text-splices the raw
`$ARGUMENTS` string into the `` !`...` `` command line *before* bash parses
it, rather than substituting it as a genuine shell variable — exactly the
failure mode the here-string mitigation (`<<< "$ARGUMENTS"`) was supposed to
prevent and does not. Any text passed to `/veil` containing a bare `"`,
`` ` ``, or `$(...)` executes as shell syntax, arbitrarily. This is a second,
independent way this adapter fails, on top of (not caused by, not fixed by)
the `<command-args>` PII-exposure finding above.

**No code-level fix exists.** By the time `sanitize.py` runs, an injected
command has already executed as a separate process the script never sees —
same conclusion the earlier security-reviewer pass reached, now confirmed
empirically rather than argued from risk.

**Decision:** do not attempt to patch `veil.md`/`sanitize.py` for this. The
`command` adapter's premise for the "user prompt -> model" leg is dead on two
independent grounds now (PII exposure + code execution); the gateway is the
only planned mechanism for this leg going forward (see the finding below and
`BRIEF.md` §2.1/§8). Leaving a working RCE reachable via a checked-in slash
command in the meantime is a live risk *today*, independent of when or
whether the gateway ships — recommend neutering or removing
`.claude/commands/veil.md` now rather than deferring, since "the design is
superseded" and "the file is currently invokable and executes arbitrary shell
commands" are two different facts.

**Human decision (2026-09-07): leave `.claude/commands/veil.md` as-is, against
this recommendation.** The human was offered neutering, deletion, or leaving
it, and explicitly chose to leave it untouched, accepting the confirmed RCE
as a known, live risk while the gateway is unbuilt. Recorded here so this is
an informed, deliberate choice on the record — not an oversight — per
`BRIEF.md`'s own principle that residual risks get documented, not hidden.
Anyone resuming this project should know: `/veil` is currently invokable and
currently executes arbitrary shell commands from its argument text.

`BRIEF.md` §6's risk table entry for this ("Command injection when passing
`$ARGUMENTS`... Mitigation: pass via stdin, never interpolated into a bash
line") is corrected to reflect that the mitigation was tested and does not
hold, because the injection happens before the value ever reaches stdin.

## Critical finding from live human verification (2026-09-07)

**Read this before anything else below.** The human's live `/veil` test (a
benign email + DNI, no shell metacharacters) tokenized correctly on stdout,
but the raw argument text also reached the model verbatim via a
`<command-args>` transcript tag that Claude Code renders independently of the
command body's bash execution. This is not a bug in `sanitize.py` or
`veil.md` — both behave exactly as designed — it is BRIEF §2.1's own claim
for this interception point ("Can rewrite: Yes") turning out to be false. See
`mission.md`'s "Critical finding" section for the full analysis and the
resulting decision: finish the next mission (hooks, BRIEF step 8) as planned,
then re-prioritize the gateway auth spike (BRIEF step 12) ahead of the local
LLM detector/eval/audit log in day 2. `BRIEF.md` §2.1's table is corrected to
match. The unresolved `$ARGUMENTS`-shell-injection question below is a
separate, still-open issue — this finding fires on a fully benign prompt and
does not depend on it.

## Summary

The `/veil` adapter is implemented per BRIEF §2.1's "User prompt -> model"
row: a Python sanitizer script (`veil_pii_middleware/adapters/command/
sanitize.py`) reads the raw user prompt from stdin only, tokenizes it via the
already-implemented, unmodified `core/tokenizer.py::tokenize()` against a
`core/vault.py::Vault`, and writes the result to stdout — falling back to the
original text unchanged when no PII is detected, and failing closed (nonzero
exit, empty stdout) on any vault/tokenization error. `.claude/commands/
veil.md` invokes it via a single-line `` !`...` `` bash execution, passing
the raw argument text through a bash here-string (`<<< "$ARGUMENTS"`) rather
than as a positional CLI argument.

`.harness/bin/verify` is green (contract, lint, typecheck, test, eval, build;
140 tests total, up from 136 before this mission, no regressions elsewhere).
`tests/test_sanitize.py` has 11 tests: known PII tokenized on stdout, a
no-PII prompt passed through byte-for-byte unchanged, adversarial stdin
(quotes, backticks, `; rm -rf`) proven inert (no side effect, plus a
source-level check that the script never imports `subprocess`/`os.system`/
`os.popen`/uses `shell=True`), vault-open failure (missing `VEIL_VAULT_KEY`)
failing closed, session-id fallback behavior (warns on stderr, and two
different session ids proven via `core/vault.py::Vault` not to share token
resolution), and structural checks on `veil.md` (here-string only, no
`allowed-tools` wildcard grant). `goal-verifier` independently confirmed all
7 numbered acceptance criteria pass with file:line/command evidence. A
security-focused `code-reviewer` pass found one item that could be fixed
(session-id fallback was silent) plus one that removed a false sense of
safety (the `allowed-tools` wildcard), both fixed and re-verified green
below — and one that **cannot** be fixed from this repo; see the next
section, which is the one thing this handoff most needs you to read.

### The one item that needs your action: live human verification

**This is not solved by any amount of code review or testing run so far, and
it is a hard merge-blocker, not an FYI.**

The whole safety argument for `.claude/commands/veil.md` rests on
`<<< "$ARGUMENTS"` (a bash here-string) treating the raw, untrusted prompt as
inert data rather than shell syntax. That argument only holds **if Claude
Code substitutes `$ARGUMENTS` as a genuine shell variable at bash-execution
time.** If instead Claude Code text-splices the literal argument string into
the command line *before* handing it to bash — which is how slash-command
templating commonly works, and is exactly as plausible given what's
documented — then a prompt containing a bare `"` breaks out of the quotes
the `.md` file supplies. For example, a `/veil` invocation with the argument

```
foo" ; touch pwned #
```

would render the executed line as:

```
python veil_pii_middleware/adapters/command/sanitize.py <<< "foo" ; touch pwned #"
```

— a working command injection, with the here-string never coming into play
as a mitigation at all.

**Neither this agent nor any subagent used during this mission can resolve
this.** It requires driving a real Claude Code CLI session, which no tool
available in this harness run can do. `claude-code-guide` was consulted
during intake and confirmed the substitution mechanics are genuinely
undocumented; an independent `code-reviewer` pass re-derived the same
conclusion from the code and flagged it as blocking without being told about
the earlier research.

**What to do:** in a real Claude Code session, run `/veil` with an argument
containing at least a bare `"` and a shell metacharacter (e.g. the example
above, or your own variant), and observe:

1. Does the exact text you typed reach the model as a prompt (correctly
   sanitized if it contained PII, unchanged otherwise) — or does something
   else happen (a stray file gets created, a command visibly runs, the
   session behaves unexpectedly)?
2. Does Claude Code show you a permission prompt for the rendered `!`...``
   command before running it? (It should now, since `allowed-tools` was
   deliberately left ungranted — see the addendum in `mission.md` for why.)
   If it auto-runs without asking, that's worth knowing regardless of (1).

Record the result here (or wherever you track it) before merging. If the
text-splice scenario is confirmed, this adapter needs a design change before
it's safe to use — not a patch to `sanitize.py`, since by the time that
script runs, an injected command would already be a separate, already-
executed process it never sees.

**Update:** the human started this test and hit a real (now-fixed) bug
first: the `!`` line called bare `python`, which resolved to the system
Python install rather than `.venv`, so `/veil` failed with
`ModuleNotFoundError` before tokenizing anything. Fixed in
`.claude/commands/veil.md` by resolving the venv interpreter explicitly
(same Windows/POSIX fallback pattern `.harness/bin/verify` already uses);
see `mission.md`'s "Live human verification" section for the full
before/after. `$ARGUMENTS` still lands in the exact same single position as
before, so this fix doesn't touch the open injection question — the
actual `$(...)`/backtick and quote-breakout tests below still need to be run.

### Other findings from the security review (fixed)

- **Silent session-id fallback.** `CLAUDE_SESSION_ID` (used to key the
  vault's `session_id`, since slash commands get no session JSON on stdin
  unlike hooks) falling back to a fixed `veil-demo` string was silent —
  meaning if Claude Code never actually populates that env var for
  slash-command bash execution (unconfirmed, same category of uncertainty as
  above), *every* `/veil` invocation from every concurrent conversation on
  this machine silently shares one vault's token namespace, contradicting
  `core/vault.py`'s "one mapping set per session" guarantee, with zero
  signal to the user. Fixed: `sanitize.py` now prints a stderr warning
  naming the fallback whenever `CLAUDE_SESSION_ID` is unset. Covered by
  `TestSessionIdFallback` in `tests/test_sanitize.py`, including a test that
  opens the same on-disk vault directly via `core/vault.py::Vault` under two
  different session ids and confirms a token minted under one raises
  `TokenNotFoundError` when resolved under the other.
- **`allowed-tools` wildcard removed a human backstop.** The original
  `.claude/commands/veil.md` granted `allowed-tools: Bash(python .../
  sanitize.py:*)`. The reviewer pointed out this wildcard would very likely
  still prefix-match a command line even after an injected `$ARGUMENTS`
  payload appended a second shell command to it — auto-approving exactly the
  injection scenario above instead of surfacing it to a human via the normal
  permission prompt. Fixed by removing the `allowed-tools` grant entirely,
  so every invocation now goes through Claude Code's standard ask-permission
  flow. This is the mitigating control available for the item above; it
  does not resolve the underlying platform-behavior question, it only
  ensures a human sees the rendered command before it runs.

## Files changed

- `veil_pii_middleware/adapters/command/sanitize.py` — new: `main()` entry
  point, stdin-only prompt reading, fixed `.veil/vault.db` path,
  `CLAUDE_SESSION_ID`-with-fallback session id (now with a stderr warning on
  fallback), fail-closed exception handling.
- `veil_pii_middleware/adapters/command/__init__.py` — docstring corrected
  from the stale `/ask` reference to `/veil`.
- `.claude/commands/veil.md` — new: slash-command definition, here-string
  argument passing, no `allowed-tools` grant (see above), an inline HTML
  comment explaining why.
- `tests/test_sanitize.py` — new: 11 tests (see Summary).
- `.gitignore` — added `.veil/`.
- `.harness/mission.md`, `.harness/validation.md` — rewritten for this
  mission (previously held the prior tokenizer mission's content, now
  archived to `docs/missions/mission-4/`).

## Human verification steps

1. Run `bash .harness/bin/verify` from Git Bash at the repo root — expect
   `RESULT: PASS — all 6 gate(s) green.`
2. **Do the live `/veil` injection test described above, in a real Claude
   Code session.** This is the step that actually gates merge for a
   high-risk, first-real-external-channel adapter — nothing else in this
   handoff substitutes for it.
3. Read `veil_pii_middleware/adapters/command/sanitize.py` (60 lines) and
   `.claude/commands/veil.md` (small) end to end — both are small enough to
   review in full given the high-risk classification.
4. Decide whether the `CLAUDE_SESSION_ID`/`veil-demo` fallback design is
   acceptable to carry forward as-is, given it's explicitly a demo-scope
   simplification (real session lifecycle is planned for the hooks work,
   BRIEF work-plan step 8, out of scope here).
5. Approve or request changes before merge (required per `CLAUDE.md`'s
   high-risk review policy — this is the first adapter touching a real
   outbound channel to the model). No commit, push, or merge has been
   performed.

## Follow-ups (not done, for a future mission if wanted)

- Resolve the `$ARGUMENTS`-in-`!`command`` shell-safety question definitively
  (may need an upstream Claude Code documentation request/issue, not just a
  local test, if the live test above is inconclusive).
- `PreToolUse`/`PostToolUse` hooks (BRIEF work-plan step 8) — real per-session
  lifecycle management would let `/veil` (and a future de-tokenizing hook)
  share a session id that's actually guaranteed correct, replacing today's
  fallback.
- `core/audit.py` — still an unimplemented stub; this adapter logs nothing.
