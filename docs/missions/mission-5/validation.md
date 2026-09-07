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
| Known PII tokenized on stdout | pytest: run `sanitize.py` as a subprocess (argv list, stdin=raw prompt containing an email/DNI), assert stdout contains `PII_EMAIL_NNNN`/`PII_DNI_NNNN` and not the original value, exit `0` | exit `0` |
| No-PII prompt passes through unchanged | pytest: stdin with no detectable PII → stdout is byte-for-byte identical to stdin, exit `0` | exit `0` |
| Adversarial stdin never reaches a shell | pytest: stdin containing `"` , `` ` ``, and `; rm -rf /tmp/marker` (a benign marker file path, not a real destructive path) → assert no such file is created, process exit code and stdout are consistent with plain-data tokenization, and no unexpected side effect occurred | exit `0` |
| Script contains no shell-execution primitives | pytest or static check: `sanitize.py`'s source contains no `subprocess`, `os.system`, `os.popen`, or `shell=True` | exit `0` |
| Vault-open failure fails closed | pytest: run with `VEIL_VAULT_KEY` unset (`monkeypatch.delenv`), assert non-zero exit code and empty stdout — never the raw prompt | exit `0` |
| Command file passes `$ARGUMENTS` only via here-string | test (can be a plain Python string-based assertion, not pytest-parametrized): read `.claude/commands/veil.md`, assert it contains `<<< "$ARGUMENTS"` and does not contain `$ARGUMENTS` anywhere outside that here-string construct (e.g. no bare `sanitize.py $ARGUMENTS` or `sanitize.py "$ARGUMENTS"` positional form) | exit `0` |
| `__init__.py` docstring corrected | grep/assert: `veil_pii_middleware/adapters/command/__init__.py` references `/veil`, not the stale `/ask` | exit `0` |
| Missing `CLAUDE_SESSION_ID` warns visibly | pytest: run `sanitize.py` with `CLAUDE_SESSION_ID` unset, assert stderr names the env var and the fallback id (`veil-demo`), exit `0` regardless | exit `0` |
| Different session ids do not share vault mappings | pytest: run `sanitize.py` with `CLAUDE_SESSION_ID=session-a` on a PII prompt, extract the minted token from stdout, then open the same on-disk vault directly via `core/vault.py::Vault` under `session-a` (resolves) and `session-b` (raises `TokenNotFoundError`) | exit `0` |
| No `allowed-tools` wildcard grant in the command file | test: read `.claude/commands/veil.md`'s frontmatter, assert no `allowed-tools` key — every invocation must go through the standard ask-permission flow given the unresolved injection risk | exit `0` |

## Scrutiny Validator

- Confirm the diff touches only `veil_pii_middleware/adapters/command/`,
  `.claude/commands/veil.md`, `tests/test_sanitize.py`, and the harness
  mission files — `detector.py`, `vault.py`, `tokenizer.py`, `audit.py`, other
  adapters, and `frontend/` stay untouched.
- Confirm `sanitize.py` reads the prompt from `sys.stdin` only — no argv
  parsing of the prompt content, no environment variable ever carries prompt
  text.
- Confirm the top-level exception handling is genuinely fail-closed: on any
  exception (vault construction, tokenization, encoding), stdout has already
  received nothing (no partial write before the failure point), and the exit
  code is non-zero.
- Confirm no plaintext PII value or the master key ever appears in a stderr
  message, exception string, or log.
- Confirm `.claude/commands/veil.md`'s `allowed-tools` frontmatter is scoped
  narrowly (the one `sanitize.py` invocation), not a broad `Bash(*)` grant.
- Confirm the `session_id` fallback logic (`CLAUDE_SESSION_ID` env var, else
  `veil-demo`) does not silently swallow a malformed/empty env var in a way
  that could cross-contaminate two different real sessions' vault mappings.
- Confirm `.veil/` (or wherever the vault db path resolves) is covered by
  `.gitignore` — add the entry if missing.
- Confirm the mission's explicitly-flagged residual risk (undocumented
  Claude Code `$ARGUMENTS`-in-`!`command`` shell-safety) is written up
  plainly in `handoff.md` as a manual-human-verification item, not glossed
  over or implicitly claimed as "tested."

## User-Testing Validator

- Run `tests/test_sanitize.py` directly and read the output — not just the
  exit code — to confirm the injection test actually pipes the adversarial
  string via stdin (not embedded in a constructed shell command by the test
  itself) and that the assertions check real stdout content/exit codes, not
  just "no exception raised."
- Manually invoke `/veil` inside a real Claude Code session with a prompt
  containing an email and a backtick/semicolon, and confirm by eye that (a)
  the model receives a tokenized prompt, and (b) no unintended shell
  side-effect occurred. This is the one check nothing in this repo's test
  suite or any subagent can perform — record the result (or its absence) in
  `handoff.md`.
