# Agent Instructions

Read `CLAUDE.md` first. It contains the project-specific operating agreement.

## Harness Map

- `.harness/docs/README.md` — installed user guide and documentation map.
- `.harness/docs/operating-model.md` — task loop, approvals, risk, and review.
- `.harness/docs/agent-permissions.md` — allow, ask, deny, and human gates.
- `.harness/mission.md` / `.harness/validation.md` / `.harness/handoff.md` —
  current task state and evidence.
- `.harness/bin/verify` — the single gate: `0` pass, `1` fail, `2` unconfigured.
- `.harness/bin/memory` and `.harness/bin/skill` — persistent learning tools.
- `.harness/skills/` — canonical repo-native skills.

## Non-Negotiables

- Start new work with `/start-mission <task>` or follow the same Intake Grill
  and approval loop manually when the active tool has no command adapter.
- Write validation before implementation and pause for approval when required.
- Fix failures; never weaken checks to make the gate green.
- Never commit secrets. Ask before external writes, destructive actions,
  production changes, deploys, pushes, or merges.
- Run `.harness/bin/verify` before claiming completion.

Tool-specific adapters may enforce this contract, but they do not redefine it.
