---
description: Start a Blueprint mission: Grill questions, mission draft, tooling approach assessment, validation contract, and approval checkpoint.
argument-hint: "<idea or task>"
allowed-tools: Read, Edit, Write, Glob, Grep, Task, Bash(git status), Bash(git diff:*), Bash(.harness/bin/verify), Bash(.harness/bin/verify:*), Bash(.harness/internal/check-mission-contract), Bash(.harness/bin/memory:*), Bash(.harness/bin/skill:*)
---

# /start-mission - begin one harness mission

Start one Blueprint mission for: **$ARGUMENTS**

> **Context Budget:** At 60%+ context, do not start a new phase. At 75%+,
> write `.harness/session-handoff.md` (Exact Next Step required) and stop.
> See `.harness/docs/context-management.md`.

## Stage 0 - Resume Check

Read `.harness/mission.md`. If `## Status` is anything other than
`not started`, read `.harness/session-handoff.md` to find the
`## Exact Next Step` and resume from there — do not re-run the Intake
Grill on an already-approved mission.

This is the human-friendly entrypoint for the harness loop. It wraps the same
single-session workflow as `/orchestrate`, but emphasizes the first decision point:
define the mission before implementation.

Do not implement until the human approves the mission, tooling approach, risk, and
validation contract.

## Stage 1 - Mandatory Intake Grill

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
`AskUserQuestion` tool (up to 4 themed questions, 2-4 options each, recommended
option first), in sequential rounds that resolve dependent decision branches.
Record the answers in `.harness/mission.md` under `## Intake Grill`. If the user
refuses clarification, record the missing information as assumptions and keep the
change small.

## Stage 2 - Mission Draft

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

## Stage 3 - Tooling And Approach Assessment

Add this required section to `.harness/mission.md`:

```md
## Tooling And Approach Assessment
```

Answer these questions narrowly:

1. What project type is this?
2. What methods, tooling, or approaches does that imply?
3. What risks or missing decisions does it reveal?
4. What should be added to validation?
5. What is explicitly not needed now?

Recommend only tools or methods that materially affect the task. Do not create a
broad tool shopping list.

Examples:

- Terraform/AWS work should check IAM, secrets, environments, state, rollback, cost,
  and resource scope.
- Landing page work should check brand assets, design direction, accessibility,
  responsive behavior, analytics/contact flow, and deployment target.
- Jira, Confluence, GitLab, and worktree conventions should be referenced only when
  relevant to the project or Cactus workflow.

## Stage 4 - Validation Contract

Write or update `.harness/validation.md`.

The validation contract must be written before implementation and must include the
required gate:

```bash
.harness/bin/verify
```

Validation must reflect the Tooling And Approach Assessment. If the assessment
identifies Terraform, include appropriate Terraform checks. If it identifies a
landing page, include checks for build, responsive behavior, accessibility, or
manual review evidence as appropriate.

## Stage 5 - Lane Check And Human Approval Checkpoint

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
  Stage 6; the human still reviews at merge.

Stop and show the proposed mission, tooling approach, risk, and validation contract.
Ask for explicit approval before implementation.

Do not implement until the human approves the mission, acceptance criteria,
non-goals, tooling approach, risk, and validation checks.

Set `## Status` in `.harness/mission.md` to `awaiting approval` before stopping.
Update it only after the human responds (for example `approved — implementing`);
never mark the mission approved on the human's behalf.

## Stage 6 - Continue The Harness Loop

After approval, continue through the same loop as `/orchestrate`:

1. Implement the smallest coherent change. Keep the work serial.
   For the Validators stage, spawn `goal-verifier` and `code-reviewer` as
   independent subagents with read-only context — do not let validators edit
   files. See `.harness/docs/operating-model.md` delegation model.
2. Wire the executable checks from `.harness/validation.md` into the gate — as
   `.harness/bin/verify` GATES rows or a script the gate runs; checks that stay
   manual belong under the validator sections.
3. Run `.harness/bin/verify` and fix implementation issues until green or blocked.
4. Run independent validation with `goal-verifier` and `code-reviewer`
   subagents — fresh context is required; the main session must not review
   its own work. Run a security-focused code-reviewer pass when the mission
   or diff touches auth, permissions, payments, data migrations, production
   configuration, infrastructure, deployment, secrets, destructive commands,
   public APIs, or sensitive data.
5. Write `.harness/handoff.md` with evidence and human verification steps.
6. Record gotchas and create or improve active skills when evidence supports it.

Do not commit, push, deploy, mutate external systems, run background workers, consume
a queue, or auto-merge.
