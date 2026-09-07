---
description: Run the Blueprint harness loop for one task: intake, mission, tooling approach assessment, validation, approved implementation, verification, validators, and handoff.
argument-hint: "<task objective>"
allowed-tools: Read, Edit, Write, Glob, Grep, Task, Bash(git status), Bash(git diff:*), Bash(.harness/bin/verify), Bash(.harness/bin/verify:*), Bash(.harness/internal/check-mission-contract), Bash(.harness/bin/memory:*), Bash(.harness/bin/skill:*)
---

# /orchestrate - one-task harness loop

Run exactly one Blueprint harness task for: **$ARGUMENTS**

> **Context Budget:** At 60%+ context, do not start a new phase. At 75%+,
> write `.harness/session-handoff.md` (Exact Next Step required) and stop.
> See `.harness/docs/context-management.md`.

This command is not a background runner, queue, daemon, or auto-merge path. It is a
single-session orchestrator. The human must approve the mission, tooling approach,
risk, and validation contract before implementation starts.

## Stage 0 - Resume Check

Read `.harness/mission.md`. If `## Status` is anything other than
`not started`, read `.harness/session-handoff.md` to find the
`## Exact Next Step` and resume from there — do not re-run the Intake
Grill on an already-approved mission.

## Stage 1 - Intake Grill

Before editing implementation files, ask 3-6 clarification questions. Follow
`CLAUDE.md` and the Mandatory Intake Grill:

- user problem and desired outcome
- target users or affected workflow
- scope, non-goals, and protected areas
- acceptance criteria and observable behavior
- edge cases, data, integrations, and dependencies
- risk level and human verification needs
- execution lane and review triggers (default `agent-with-checkpoint`;
  `agent-owned` only per `.harness/docs/operating-model.md` `## Execution Lanes`)

Explore the repo first for discoverable facts. Ask only for gaps that change the
task definition. Use the `intake-grill` skill: ask each round through the
`AskUserQuestion` tool (up to 4 themed questions, recommended option first), in
sequential rounds that resolve dependent decision branches. If the user refuses
clarification, record the missing information as assumptions and keep the change
small.

## Stage 2 - Mission And Tooling Assessment

Write or update `.harness/mission.md` with:

- objective
- `## Routing` rows (execution lane, human review trigger, delegation
  mechanism, validation plan). Conservative defaults are
  `agent-with-checkpoint`, `before-code, before-merge`, `main-session`, and
  `.harness/validation.md`.
- Intake Grill answers or repo evidence
- scope
- non-goals
- acceptance criteria
- risk (with a `Risk class:` line)
- merge policy

Then add the required section:

```md
## Tooling And Approach Assessment
```

Answer narrowly:

1. What project type is this?
2. What methods, tooling, or approaches does that imply?
3. What risks or missing decisions does it reveal?
4. What should be added to validation?
5. What is explicitly not needed now?

Recommend only tools or methods that materially affect the task. Do not create a
broad tool shopping list.

## Stage 3 - Validation Contract

Write or update `.harness/validation.md`.

The validation contract must be written before implementation, must reflect the
Tooling And Approach Assessment, and must include the required gate:

```bash
.harness/bin/verify
```

Also record whether the task is low risk or high risk. High-risk work needs a
human verification guide and cannot be treated as merge-ready without human review.

## Stage 4 - Lane Check And Human Approval Checkpoint

Read `Execution lane` from `.harness/mission.md` `## Routing` (treat a
missing row as `agent-with-checkpoint`). Lane rules:
`.harness/docs/operating-model.md` `## Execution Lanes`.

- `human-owned`: present the mission and validation contract, then stop —
  the human implements.
- `agent-with-checkpoint` (default): hard pre-code stop below.
- `agent-owned`: skip the pre-code stop ONLY when all three hold —
  `Risk class` is `low`; `.harness/internal/check-mission-contract` exits 0; and
  `.harness/bin/verify` exits with a code other than `2` (a project gate is
  configured). If any condition fails, fall back to the stop below and say
  why. When skipping, set `## Status` to
  `agent-owned — proceeding without pre-code approval` and continue to
  Stage 5; the human still reviews at merge.

Stop and show the proposed mission, tooling approach, risk, and validation contract.
Ask for explicit approval before implementation.

Do not implement until the human approves the mission, acceptance criteria,
non-goals, tooling approach, risk, and validation checks.

Set `## Status` in `.harness/mission.md` to `awaiting approval` before stopping.
Update it only after the human responds (for example `approved — implementing`);
never mark the mission approved on the human's behalf.

## Stage 5 - Worker Execution

After approval, implement the smallest coherent change. Keep the work serial.

For the Validators stage (Stage 6), spawn `goal-verifier` and `code-reviewer`
as independent subagents with read-only context — do not let validators edit
files. See `.harness/docs/operating-model.md` delegation model.

Loop on:

```bash
.harness/bin/verify
```

Rules:

- Wire the executable checks from `.harness/validation.md` into the gate — as
  GATES rows or a script the gate runs; checks that stay manual belong under
  the validator sections, not `## Executable Checks`.
- If the gate exits `0`, continue to validation.
- If the gate exits `1`, fix the implementation and rerun.
- If the gate exits `2`, stop and ask for human input; the gate is unconfigured.
- Do not weaken tests, hooks, gates, or validation to pass.
- Do not commit, push, deploy, or mutate external systems.

## Stage 6 - Validators

Delegate independent checks with fresh context:

1. Use `goal-verifier` to verify `.harness/mission.md`,
   `.harness/validation.md`, the diff, and `.harness/bin/verify` evidence.
2. Use `code-reviewer` for scrutiny review of correctness, scope, risk, and
   conventions.
3. Use `test-runner` only when additional isolated test execution or failure
   summarization is useful.

Run a security-focused code-reviewer pass when the mission or diff touches
auth, permissions, payments, data migrations, production configuration,
infrastructure, deployment, secrets, destructive commands, public APIs, or
sensitive data.

For high-risk changes, explicitly call out that human review is required.

## Stage 7 - Handoff

Write `.harness/handoff.md` with:

- summary
- implemented
- left undone
- commands run with exit codes
- files changed
- issues found
- gotchas learned
- procedure compliance
- risk review
- human verification guide
- verdict: `done`, `needs-fix`, `needs-human`, or `blocked`

## Stage 8 - Persistent Learning

If gates, validators, user correction, or review found a gotcha, record it:

```bash
.harness/bin/memory record --type gotcha --summary "<what happened>" --source .harness/handoff.md --prevention "<future check>"
```

If a repeated procedure or gotcha should become reusable, create or improve an active
skill:

```bash
.harness/bin/skill create --name <kebab-slug> --trigger "<when to use>" --summary "<procedure>" --evidence .harness/handoff.md
.harness/bin/skill improve --name <kebab-slug> --evidence .harness/memory/events.jsonl --change "<what changed>"
```

Do not store secrets or private user memory. Do not mutate gates or external systems
automatically. Skills are active when written into the repo; git review is the audit
and rollback path.

Finish by reporting the verdict and the shortest useful human review path.
