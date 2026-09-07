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

List every command or acceptance check the worker and validators must run.
Executable checks must be wired into the gate — as `.harness/bin/verify` GATES
rows or a script the gate runs. Checks that stay manual belong under the
validator sections below, not here.

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| <PLACEHOLDER: check name> | <PLACEHOLDER: command> | <PLACEHOLDER: required result> |

## Scrutiny Validator

Code-level review requirements:

- Check the diff against the objective and non-goals.
- Confirm tests or checks cover the changed behavior.
- Confirm the gate was not weakened.
- <PLACEHOLDER: task-specific code-level checks>

## User-Testing Validator

Behavior-level review requirements:

- Check the user-visible workflow or documented outcome.
- Confirm acceptance criteria with observable evidence.
- Note anything a human should manually verify.
- <PLACEHOLDER: task-specific behavior-level checks>

## Stop Conditions

Stop and ask for human input if the gate exits `2`, high-risk evidence is unclear,
or the task requires changing verification policy.
