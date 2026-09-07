# Validation Contract

Write this before implementation starts.

## Gate

The required gate is:

```bash
.harness/bin/verify
```

Exit codes:

- `0` = pass
- `1` = fail
- `2` = unconfigured

## Executable Checks

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore`, no implicit `Any` |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Build | `.venv/Scripts/python -m build --no-isolation` | exit `0` (no new dependency introduced) |
| Detector recall/decoys (unchanged) | `python -m veil_pii_middleware.demo.eval --min-recall 1.0 --max-false-positives 0` | exit `0`, unmodified — this mission does not touch `detector.py` |
| `PreToolUse` de-tokenizes a known token | pytest: subprocess run of `pre_tool_use.py`, stdin = JSON with `tool_name="Bash"`, `tool_input.command` referencing `veil_demo.db` and containing a real minted token, `session_id` matching a vault pre-seeded with that token → stdout JSON's `hookSpecificOutput.updatedInput.command` contains the real value, not the token; exit `0` | exit `0` |
| `PreToolUse` blocks on an unresolvable token | pytest: same shape, token unknown to the vault (or `VEIL_VAULT_KEY` unset) → exit `2`, empty stdout | exit `0` |
| `PreToolUse` passes through non-demo-db / non-Bash calls | pytest: (a) `tool_name="Bash"` with a command not referencing `veil_demo.db`, (b) `tool_name` something else entirely → both: exit `0`, empty stdout, no vault ever opened (assert no `.veil/vault.db` file is created in a fresh `tmp_path` cwd) | exit `0` |
| `PostToolUse` tokenizes a demo-db response, generic shape | pytest: subprocess run of `post_tool_use.py`, stdin = JSON with `tool_name="Bash"`, a demo-db `tool_input.command`, and `tool_response` as a **nested** JSON value (dict containing a list containing PII strings, not a flat `{stdout: ...}`) → stdout JSON's `hookSpecificOutput.updatedToolOutput` has every PII string replaced by a token at the same nested position, non-string/structure untouched; exit `0` | exit `0` |
| `PostToolUse` passes through non-demo-db / non-Bash calls | pytest: same shape as the `PreToolUse` passthrough check, applied to `post_tool_use.py` | exit `0` |
| `PostToolUse` fails closed on vault error without fabricating output | pytest: `VEIL_VAULT_KEY` unset on a demo-db call → non-zero exit, stdout contains no `updatedToolOutput` key (either empty stdout or non-JSON stderr-only failure) | exit `0` |
| Neither script imports shell-execution primitives | pytest or static check: `pre_tool_use.py`/`post_tool_use.py` source contains no `subprocess`, `os.system`, `os.popen`, or `shell=True` | exit `0` |
| `is_demo_db_command` is shared, not duplicated | grep/assert: both scripts import `is_demo_db_command` from `adapters/hooks/_scope`, no independent re-implementation of the check | exit `0` |
| `demo/settings.hooks.json` is not wired into committed settings | test: `.claude/settings.json` and `.claude/settings.local.json.example` do not reference `demo/settings.hooks.json` or contain a `Bash`-matched `PreToolUse`/`PostToolUse` entry pointing at `pre_tool_use.py`/`post_tool_use.py` | exit `0` |
| `.gitignore` covers the vault | grep/assert: `.gitignore` still contains a `.veil/` (or equivalent) entry | exit `0` |
| Non-object top-level payload fails closed (added after code review) | pytest, parametrized over `[1,2,3]`/`null`/a bare string/`true` as the whole stdin payload: `pre_tool_use.py` exits `2` with empty stdout; `post_tool_use.py` exits non-zero with empty stdout; neither produces an unhandled traceback | exit `0` |
| `pre_tool_use.py` never emits `permissionDecision` (added after code review) | pytest: on the known-token success path, assert `"permissionDecision" not in out["hookSpecificOutput"]`; grep: `permissionDecision` appears in `pre_tool_use.py` only inside its docstring, never in the `output` dict literal or any executable branch | exit `0` |

## Scrutiny Validator

- Confirm the diff touches only `veil_pii_middleware/adapters/hooks/`,
  `demo/settings.hooks.json`, `README.md`, `tests/test_hooks.py`, and the
  harness mission files — `detector.py`, `vault.py`, `tokenizer.py`,
  `audit.py`, `adapters/command/`, `adapters/gateway/`, and `frontend/` stay
  untouched.
- Confirm both hook scripts read their JSON payload from `sys.stdin` only —
  no argv parsing of tool input/output content.
- Confirm `PreToolUse`'s fail-closed path never writes a JSON
  `permissionDecision` alongside exit `2` that could be misread as a mixed
  signal — exit `2` alone, stderr only.
- Confirm `PreToolUse` never emits `permissionDecision` on the *success*
  path either (see "Review findings and fixes" in `mission.md`): pairing
  `permissionDecision: "allow"` with `is_demo_db_command()`'s bare substring
  check would auto-approve an entire chained/injected command, not just
  rewrite it. Only `updatedInput` may appear in `pre_tool_use.py`'s output.
- Confirm both scripts validate the top-level JSON payload is an object
  (`isinstance(payload, dict)`) inside the same `try` block that catches
  `json.loads` failures — a non-object payload (list/`null`/scalar) must
  not raise an uncaught exception past the fail-closed handler.
- Confirm `PostToolUse`'s failure path never emits a stdout JSON claiming
  `updatedToolOutput` succeeded when it didn't — no partial/fabricated
  output on the error path.
- Confirm no plaintext PII value or the master key ever appears in a stderr
  message, exception string, or log, on either script's failure path.
- Confirm `updatedInput` in the success path is a full copy of the original
  `tool_input` with only `command` replaced — not a partial object that
  drops `description`/`timeout`/`run_in_background`.
- Confirm `is_demo_db_command()`'s check is a simple, documented substring
  check (`veil_demo.db`) with no attempt at a broader/smarter classifier —
  matches the project's own "don't over-engineer a demo-scoped check"
  framing.
- Confirm `demo/settings.hooks.json` is valid JSON containing only a
  `hooks` key (no accidental full settings object that would clobber
  unrelated keys if merged via `--settings`).
- Confirm the README addition documents the `--settings` invocation and its
  "replaces, not merges, this session's `hooks` key" trade-off plainly.
- Confirm the mission's three explicitly-flagged residual risks
  (`PostToolUse`'s undocumented `Bash` `tool_response` shape; `--settings`'s
  untested-live merge semantics for the `hooks` key; whether Claude Code
  actually applies `updatedInput` when `permissionDecision` is left unset)
  are written up in `handoff.md`, not glossed over or implicitly claimed as
  resolved.

## User-Testing Validator

- Run `tests/test_hooks.py` directly and read the output — not just the
  exit code — confirming each test's stdin JSON is Python-constructed and
  piped as data, never spliced into a shell command string.
- **Manually run the actual demo in a real Claude Code session** with
  `claude --settings demo/settings.hooks.json` active: issue a prompt that
  causes the model to run a `sqlite3` query against `demo/veil_demo.db`
  containing real PII, confirm by eye that (a) `PostToolUse` produces a
  tokenized result the model actually reasons over (check the transcript,
  not just the hook's own stdout), and (b) a follow-up query built around
  one of those tokens gets de-tokenized by `PreToolUse` and returns the
  correct real row **even though `pre_tool_use.py` emits no
  `permissionDecision`** — confirm Claude Code still applies `updatedInput`
  and still shows the normal ask-permission prompt for the rewritten
  command (this is the residual risk the code-review fix introduced: the
  official docs never show `updatedInput` without an accompanying
  `permissionDecision`). This is the check nothing in this repo's test
  suite or any subagent can perform — record the result (or its absence, if
  not run) in `handoff.md`, per this mission's own charge not to assume
  hooks behave as documented just because `/veil` did not.
