# Mission

## Objective

Implement the `/veil` adapter (BRIEF §2.1, row "User prompt -> model": slash
command with `` !`script` `` — the script's stdout replaces the prompt) in
`veil_pii_middleware/adapters/command/`. Two pieces: (1) a Python sanitizer
script that reads the raw user prompt from **stdin only**, tokenizes it via
`core/tokenizer.py::tokenize()` against a `core/vault.py::Vault` instance, and
writes the (possibly unchanged) result to stdout; the adapter reuses
`detector.py`/`vault.py`/`tokenizer.py` unmodified and does not reimplement
detection or substitution; (2) `.claude/commands/veil.md`, the slash-command
definition that invokes the script via a single-line `` !`...` `` bash
execution, passing the raw prompt through a bash here-string
(`<<< "$ARGUMENTS"`) rather than as a positional argument, per BRIEF §6's
named anti-pattern ("never interpolated into a bash line").

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

## Intake Grill

| Question | Answer / Evidence |
|---|---|
| Claude Code's `!`command`` bash-execution mechanics for `$ARGUMENTS` are undocumented (confirmed via `claude-code-guide`: unclear whether it's a real shell variable or a raw text-splice into the command line before the shell parses it). Existing command files in this repo (`commit.md`) only show single-line `!`...`` usage, so a multi-line heredoc likely doesn't parse inside the inline span. How should the raw prompt reach the script? | Bash here-string, single line: `` !`python .../sanitize.py <<< "$ARGUMENTS"` ``. The script reads exclusively from `sys.stdin` — never argv, never an env var — so the only thing testable and controllable from this repo (the Python script never shells out, never trusts argv) is fully covered by tests. The markdown-file-level question of whether `$ARGUMENTS` is genuinely shell-variable-safe inside the here-string is **not** resolvable from this repo or by any subagent (no subagent can drive a live Claude Code session) — flagged in `handoff.md` as a residual platform-behavior risk needing one manual human test with an adversarial `/veil` invocation. |
| Slash commands get no session JSON on stdin (unlike `PreToolUse`/`PostToolUse` hooks), but BRIEF §3.2 requires "one mapping set per session." Where does this invocation's `session_id` come from? | `${CLAUDE_SESSION_ID}` substitution (documented for skills, unconfirmed for slash-command bash execution) passed to the script, with a fallback to a fixed demo session id (`veil-demo`) when it resolves empty. The fallback path is what the test suite actually exercises, since a real `${CLAUDE_SESSION_ID}` value can't be produced outside a live Claude Code session. |
| Where does the vault SQLite file (`db_path`) live? (Master key already has a fixed contract: the existing `VEIL_VAULT_KEY` env var from `core/vault.py`, unchanged.) | Fixed path, `.veil/vault.db` (relative to the project root). No new env var. The `.veil/` directory is git-ignored, matching `CLAUDE.md`'s "vault, generated databases ... must never be committed." |
| What happens when the detector finds nothing to tokenize? | `tokenize()` already returns the value unchanged when `merge_spans(detect_regex(text))` is empty — the script's stdout is simply the original prompt, verbatim. No new branch needed. |
| What happens when the vault can't be opened (e.g. `VEIL_VAULT_KEY` unset) or any other unexpected failure occurs? | Fail closed: nonzero exit, empty stdout, a short message on stderr. Matches `CLAUDE.md`'s fail-closed doctrine. Whether Claude Code actually surfaces a failed `!`command``'s stderr/exit code to the end user (vs. silently substituting empty output) is itself unconfirmed platform behavior — flagged as a residual risk in `handoff.md`, not something a pytest-level test can prove end-to-end. |

Evidence gathered from the repo before asking: `core/tokenizer.py::tokenize()`
and `core/vault.py::Vault` are both fully implemented and already tested
(`tests/test_tokenizer.py`, `tests/test_vault.py`) — this mission consumes
both unmodified. `veil_pii_middleware/adapters/command/__init__.py` already
carries a one-line docstring referencing "`/ask`"; BRIEF §2.1 names the
command `/veil`, so the stale docstring gets corrected as part of this
mission. `.claude/commands/*.md` establishes the existing slash-command
convention in this repo: YAML frontmatter with a narrow `allowed-tools:
Bash(<exact command>)` allowlist, `$ARGUMENTS` for the raw argument text, and
single-line `` !`command` `` spans whose stdout is spliced into the rendered
prompt (e.g. `commit.md`'s `` !`git status --short` ``) — no example in this
repo pipes untrusted user text into an invoked script, so the here-string
pattern for this mission is new, not copied from precedent. `Vault.__init__`
takes `(db_path, session_id, *, master_key=None)` and already raises
`InvalidMasterKeyError` fail-closed when `VEIL_VAULT_KEY` is unset/malformed.

## Scope

- `veil_pii_middleware/adapters/command/sanitize.py` (new): a `main()`
  entrypoint that reads the entire raw prompt from `sys.stdin`, resolves
  `session_id` from an env var (`CLAUDE_SESSION_ID`, falling back to
  `veil-demo`), opens a `Vault` at the fixed `.veil/vault.db` path, calls
  `tokenize(prompt, vault)`, writes the result to `sys.stdout` with no
  trailing modification, and exits `0`. Any exception during vault
  construction or tokenization is caught at the top level, produces no
  stdout, prints a short non-PII-bearing message to stderr, and exits
  non-zero. Never imports `subprocess`/`os.system`/uses `shell=True` anywhere.
- `veil_pii_middleware/adapters/command/__init__.py`: docstring corrected
  from the stale "`/ask`" reference to "`/veil`".
- `.claude/commands/veil.md` (new): frontmatter restricting `allowed-tools`
  to the one `Bash(python veil_pii_middleware/adapters/command/sanitize.py:*)`
  invocation, body is a single `` !`python
  veil_pii_middleware/adapters/command/sanitize.py <<< "$ARGUMENTS"` `` line
  (no other bash execution, no positional-argument interpolation).
- `tests/test_sanitize.py` (new): invokes `sanitize.py`'s Python entrypoint
  directly (in-process or via `subprocess.run([sys.executable, ...],
  input=..., text=True)` with a controlled, trusted argv — never building a
  shell string from the test's PII/injection payloads) covering: known PII in
  the raw prompt comes out tokenized on stdout; a prompt with no PII passes
  through byte-for-byte unchanged; adversarial strings containing quotes,
  backticks, and `` ; rm -rf `` in the piped stdin never reach a shell (proven
  by asserting the process's stdout/exit code behave as plain data — the
  script has no shell-execution path at all, which the test also confirms via
  source inspection or by asserting no subprocess call occurs internally);
  missing `VEIL_VAULT_KEY` (or any vault-open failure) yields a nonzero exit
  code and empty stdout, never the unsanitized prompt.

## Non-goals

- `PreToolUse` / `PostToolUse` hooks (BRIEF work-plan step 8) — no changes
  under `adapters/hooks/`.
- `adapters/gateway/` — no changes.
- `frontend/` — no changes.
- `core/audit.py` — no audit logging wired in from this adapter.
- The detector's LLM layer — regex-only detection, per `tokenize()`'s
  existing behavior; not modified here.
- Any change to `core/detector.py`, `core/vault.py`, or `core/tokenizer.py` —
  all three are consumed as-is.
- Resolving whether Claude Code's `$ARGUMENTS`-in-`!`command`` substitution is
  shell-variable-safe or a raw text-splice — genuinely unconfirmable from
  this repo; documented as a residual risk requiring one manual human test,
  not solved by this mission.
- A general-purpose session-id or vault-path configuration system — the fixed
  fallback (`veil-demo`, `.veil/vault.db`) is intentionally minimal for this
  mission; hooks-based session lifecycle is future work.

## Acceptance criteria

1. `sanitize.py`, given a raw prompt on stdin containing known PII (e.g. an
   email, a DNI), writes that same text to stdout with the PII spans replaced
   by `PII_<TYPE>_NNNN` tokens, and exits `0`.
2. `sanitize.py`, given a raw prompt with no detectable PII, writes it to
   stdout byte-for-byte unchanged, and exits `0`.
3. `sanitize.py` never constructs or executes a shell command from the piped
   prompt text — proven by a test that pipes adversarial content (quotes,
   backticks, `` ; rm -rf `` ) via stdin and confirms it is treated as inert
   data (present in stdout only if not detected as PII, never causing a
   side effect, non-zero exit, or altered process behavior beyond normal
   tokenization).
4. `.claude/commands/veil.md` passes `$ARGUMENTS` to the script only via the
   `<<< "$ARGUMENTS"` here-string — never as a bare positional argument, never
   interpolated into any other part of the command line.
5. With `VEIL_VAULT_KEY` unset (or otherwise invalid), `sanitize.py` exits
   non-zero, writes nothing to stdout, and does not fall back to passing the
   raw, untokenized prompt through.
6. `mypy --strict` passes on the new module with no implicit `Any`.
7. `.harness/bin/verify` exits `0`.

## Risk

Risk class: high

This is the first point where the middleware touches a real outbound channel
to the model (BRIEF §2.1's "User prompt -> model" row), and BRIEF §6 names
command injection here explicitly. A mistake either lets raw PII reach the
model (tokenizer misuse, vault fail-open) or, worse, lets user-controlled text
execute as a shell command. The mitigation available from this repo is
necessarily partial: the Python script's behavior is fully within our control
and testable; the markdown command file's `` !`...` `` shell-safety rests on
undocumented Claude Code platform behavior that only a live, human-run test
can confirm.

## Merge policy

Human approval required before merge (per `CLAUDE.md`'s high-risk review
requirement — this mission touches the tokenization surface's first real
external-facing integration point). No commit, push, or merge performed by
the agent.

## Tooling And Approach Assessment

1. **What project type is this?** A thin adapter: a thirty-or-so-line Python
   CLI script plus one markdown slash-command definition, both wiring
   already-implemented, already-tested `core/` modules into a new transport
   (stdin/stdout via Claude Code's `!`command`` mechanism). No new runtime
   dependency, no new schema, no new crypto.
2. **What methods, tooling, or approaches does that imply?** `sys.stdin.read()`
   for input, `sys.stdout.write()` for output, a top-level `try/except
   Exception` for fail-closed behavior, `sys.exit(code)` for the exit
   contract. Tests via `pytest` + `subprocess.run` (argv list form, never
   `shell=True`, never a joined string) to exercise the script as a real
   subprocess without ever building a shell command from untrusted content.
   Existing `ruff`/`mypy --strict`/`pytest` gates apply unchanged.
3. **What risks or missing decisions does it reveal?** (a) The
   `$ARGUMENTS`-in-`!`command`` shell-safety question — addressed above as an
   explicit non-goal/residual risk, not silently assumed safe. (b) Session-id
   sourcing with no hook-JSON channel — resolved via `${CLAUDE_SESSION_ID}`
   env var with a fixed fallback. (c) Vault path/lifecycle — resolved via a
   fixed `.veil/vault.db`, git-ignored. (d) Whether a `.veil/` gitignore entry
   already exists needs checking during implementation.
4. **What should be added to validation?** A source-level/behavioral check
   that `sanitize.py` contains no `subprocess`, `os.system`, `os.popen`, or
   `shell=True` — since the whole point of the script is to be the safe
   endpoint that never re-interprets its input as shell syntax. A check that
   `.claude/commands/veil.md`'s body contains the here-string form and not a
   bare `$ARGUMENTS` token outside of it.
5. **What is explicitly not needed now?** Any new dependency; any change to
   `core/`; any hook wiring; any handling of the `${CLAUDE_SESSION_ID}` value
   beyond "use it if present, else fall back" (no validation of its format);
   any live/manual Claude Code session test performed by the agent itself
   (flagged for human follow-up instead, since no agent or subagent in this
   session can drive a real Claude Code CLI invocation of `/veil`).

## Post-review addendum

A security-focused `code-reviewer` pass (independent, fresh context) found:

1. **(Blocking, unresolved from this repo)** The here-string mitigation
   (`<<< "$ARGUMENTS"`) is only safe if Claude Code substitutes `$ARGUMENTS`
   as a genuine shell variable at bash-execution time. If it instead
   text-splices the raw argument string into the command line before the
   shell parses it (undocumented either way, per the Intake Grill), a prompt
   containing a bare `"` can break out of the quotes the `.md` file supplies
   and inject an arbitrary second shell command. No code-level fix exists
   from within this repo — confirmed independently by the reviewer, who also
   noted the fragility of trying to detect this after the fact inside
   `sanitize.py` (the injected command, if any, runs as a separate process
   the script never sees). This remains exactly the residual risk the
   Intake Grill already named, now independently corroborated: it requires
   one live, human-run test of `/veil` with an adversarial prompt inside a
   real Claude Code session before this adapter is trusted, and is a hard
   merge-blocker, not a "nice to verify eventually" item.
2. **(Fixed)** The reviewer additionally found that `.claude/commands/veil.md`'s
   original `allowed-tools: Bash(python .../sanitize.py:*)` wildcard would
   likely still prefix-match a command line even if an injected `$ARGUMENTS`
   payload appended a second command to it — silently auto-approving
   exactly the injection scenario in (1) instead of surfacing it to a human
   via the normal permission prompt. Fixed by removing the `allowed-tools`
   grant entirely: every `/veil` invocation now goes through Claude Code's
   standard ask-permission flow, so a human sees the exact rendered command
   line — injected suffix included, if any — before it runs. This restores
   the human-in-the-loop backstop for exactly the risk in (1) that cannot be
   closed at the code level.
3. **(Fixed)** The `CLAUDE_SESSION_ID` fallback to a fixed `veil-demo` id was
   silent — no signal that vault mappings for that invocation are not
   isolated per conversation, contradicting `core/vault.py`'s "one mapping
   set per session" guarantee whenever the fallback is actually taken (which,
   per the Intake Grill, may be every invocation if Claude Code doesn't
   populate that env var for slash-command bash execution at all).
   `sanitize.py` now prints a stderr warning naming the fallback session id
   whenever `CLAUDE_SESSION_ID` is unset, so a human running the command sees
   the trade-off instead of it being invisible. `tests/test_sanitize.py::
   TestSessionIdFallback` now also directly proves, via `core/vault.py::Vault`
   opened against the same on-disk vault the script just wrote to, that two
   different `session_id`s do not share token resolution (a token minted
   under `session-a` raises `TokenNotFoundError` when resolved under
   `session-b`) — closing the "no test exercises isolation" gap the reviewer
   flagged.

All fixes verified green: `.harness/bin/verify` (6/6 gates), `pytest`
(140 passed, up from 136), `mypy --strict` (22 source files, clean), `ruff
check`/`ruff format --check` (clean).

## Live human verification — findings so far

The human began the required live `/veil` test (see the residual risk in
item 1 above) with the sanity-check prompt (an email + a DNI, no shell
metacharacters). It surfaced a real bug, independent of the injection
question:

**Finding (fixed):** `.claude/commands/veil.md`'s `` !`...` `` line invoked
bare `python`. On the human's machine, `python` on `PATH` resolves to the
system Python 3.12 install
(`C:\Users\dev\AppData\Local\Programs\Python\Python312\python.exe`), not
`.venv/Scripts/python.exe` — and that system interpreter does not have
`veil_pii_middleware` installed, so every `/veil` invocation failed with
`ModuleNotFoundError: No module named 'veil_pii_middleware'` before
tokenization ever ran (reproduced identically from the repo root:
`python veil_pii_middleware/adapters/command/sanitize.py <<< "hola"` fails
the same way; `.venv/Scripts/python.exe` run identically succeeds). This is
also a small illustration of why `python script.py` (as opposed to `python
-m pkg.module`) is fragile here regardless of interpreter: it sets
`sys.path[0]` to the script's own directory, not the caller's cwd, so only
an actually-installed package (as the venv has, via `pip install -e`) saves
it.

**Fix:** the `!`` line now resolves the venv interpreter explicitly, mirroring
the Windows/POSIX fallback `.harness/bin/verify` already uses:

```
!`p=.venv/Scripts/python.exe; [ -x "$p" ] || p=.venv/bin/python; "$p" veil_pii_middleware/adapters/command/sanitize.py <<< "$ARGUMENTS"`
```

`$ARGUMENTS` still only ever appears in the same single, final position
(`<<< "$ARGUMENTS"`) — this fix changes nothing about the argument-passing
question in item 1 above, it only fixes interpreter resolution. Re-verified:
`.harness/bin/verify` green (6/6), `pytest tests/test_sanitize.py` (11
passed), reproduced the fix manually against the repo's real `.venv`.

The actual injection-safety tests (a `$(...)`/backtick payload, and a
quote-breakout payload) had not yet been run by the human as of this
addendum — this fix was a precondition for reaching them, since `/veil`
didn't produce any sanitized output at all before it.

## Critical finding — the adapter's core premise is false (2026-09-07)

The human then ran the planned sanity-check invocation (`/veil mi email es
test.leak@example.com y mi DNI es 12345678Z`, no shell metacharacters). It
produced correctly tokenized stdout (`PII_EMAIL_0001`, `PII_DNI_0001`) — but
inspection of the actual model-visible transcript showed the raw, untokenized
argument text (`test.leak@example.com`, `12345678Z`) present verbatim in a
`<command-args>` tag, delivered to the model **regardless of and prior to**
`sanitize.py` ever running.

Root cause: Claude Code renders a slash command's typed arguments into the
conversation transcript as part of invoking the command, independently of
whatever the command body's `` !`...` `` bash execution produces. `sanitize.py`
and `veil.md` behave exactly as designed — the bug is not in this mission's
code, it is in the premise BRIEF.md §2.1 assigned to this interception point:
"User prompt -> model | slash command with `` !`script` `` | Can rewrite:
Yes — the script's stdout becomes the prompt." That premise is false: the raw
prompt reaches the model over a channel `sanitize.py` cannot see or affect, no
matter how it's implemented. This is a distinct failure mode from the
still-unresolved `$ARGUMENTS`-shell-injection question above — it fires on a
completely benign, non-adversarial prompt, deterministically, every time.

**Consequence for the project:** the `command` adapter cannot deliver
`CLAUDE.md`/`BRIEF.md §1`'s "zero transmission of raw PII to external APIs"
requirement for the interactive-typed-prompt scenario. Of the interception
points in BRIEF §2.1, only the gateway (full HTTP proxy at
`ANTHROPIC_BASE_URL`, rewriting the outbound request body before it reaches
the real API) sits at a point structurally immune to this specific leak — a
proxy inspects the literal bytes of the request Claude Code sends to the real
external endpoint, which is necessarily downstream of however that request
got constructed. This is not yet verified end-to-end (no code exists under
`adapters/gateway/`), but the reasoning is structural rather than a platform
assumption: the model only ever sees what's already in that request body, so
a rewrite at that boundary catches it by construction. The one real
unverified precondition — whether Claude Code routes 100% of its outbound
traffic through `ANTHROPIC_BASE_URL` with no bypass channel — is exactly what
BRIEF §8 item 12 (gateway auth spike, "do this early") is meant to confirm,
and is now more urgent than originally scoped, not merely prudent.

**Decision:** proceed with the established day-1 plan — finish BRIEF work-plan
step 8 (`PreToolUse`/`PostToolUse` hooks, end-to-end demo) as the next
mission, since that mechanism modifies tool input/output via
`updatedInput`/`updatedToolOutput` directly (no analogous "render raw text to
the model before the hook runs" channel is known), giving it a different and
plausibly sounder risk profile — still to be confirmed by its own live human
test, not assumed. For day 2, re-prioritize: run the gateway auth spike
(BRIEF §8 item 12) first, ahead of the local LLM detector, `eval.py`, and the
audit log/frontend (items 9-11), since detection-recall work is valuable
under any enforcement mechanism while the gateway is now the only mechanism
that can close the gap this finding exposed.

`BRIEF.md` §2.1's table has been corrected to reflect this (see that file).
This does not roll back anything already merged in this mission — `sanitize.py`
and `veil.md` remain correctly implemented for what they can actually do; the
finding is about the ceiling of what this interception point can guarantee,
not a defect in the code shipped here.

## CONFIRMED: command injection via `$ARGUMENTS` text-splicing (2026-09-07)

The human ran the two adversarial payloads flagged as unresolved in the
"Live human verification" section above:

```
/veil hola" ; touch /tmp/veil-marker-b ; echo "fin
/veil hola $(touch /tmp/veil-marker-a) mundo
```

Both executed — both marker files were created. This confirms Claude Code
text-splices `$ARGUMENTS` into the `` !`...` `` command line before bash
parses it; the here-string (`<<< "$ARGUMENTS"`) never functioned as a
mitigation. See `handoff.md` for the full writeup and decision. Net effect:
the `command` adapter for the "user prompt -> model" leg is now confirmed
broken on two independent axes (raw PII exposure via `<command-args>`, and
arbitrary shell command execution via `$ARGUMENTS`), neither fixable at the
script/markdown level. `BRIEF.md` §6's mitigation entry for this risk is
corrected to note it does not hold.

## Status

closed — do not use `/veil` as implemented; recommend neutering or removing
`.claude/commands/veil.md` now (see `handoff.md`) rather than leaving a
confirmed RCE reachable while the gateway (its replacement for this leg) is
unbuilt. `sanitize.py`/`core/tokenizer.py`/`core/vault.py` remain correct and
reusable — nothing about them needs to change; only the slash-command
transport around them is unsafe.
