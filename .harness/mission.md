# Mission

## Objective

<PLACEHOLDER: what this task must achieve, in one or two sentences>

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

Defaults above are the conservative path (today's loop). Lane rules:
`.harness/docs/operating-model.md` `## Execution Lanes`. The gate's `contract`
row checks these rows through `.harness/adapter.yaml`.

## Intake Grill

Record the 3-6 required clarification questions and answers gathered before
implementation starts. If an answer is discoverable from the repo, record the repo
evidence instead of asking the user.

| Question | Answer / Evidence |
|---|---|
| <PLACEHOLDER: question> | <PLACEHOLDER: answer or repo evidence> |

## Scope

<PLACEHOLDER: the files, behaviors, and systems this task may touch>

## Non-Goals

<PLACEHOLDER: what this task must NOT change>

## Acceptance Criteria

<PLACEHOLDER: observable behaviors that prove the task is done>

## Tooling And Approach Assessment

1. What project type is this? <PLACEHOLDER>
2. What methods, tooling, or approaches does that imply? <PLACEHOLDER>
3. What risks or missing decisions does it reveal? <PLACEHOLDER>
4. What should be added to validation? <PLACEHOLDER>
5. What is explicitly not needed now? <PLACEHOLDER>

## Risk

Risk class: <PLACEHOLDER: low | medium | high>

<PLACEHOLDER: the protected areas and why. High risk is never agent-owned.>

## Merge Policy

<PLACEHOLDER: normal human review | high-risk human verification required>

## Status

not started

<!-- Lifecycle values read by .claude/hooks/mission-state-guard.sh:
     not started -> awaiting approval -> approved — implementing
     (or, when ALL agent-owned skip conditions hold:
      agent-owned — implementing / agent-owned — proceeding without
      pre-code approval). Implementation edits are blocked until the
      status starts with `approved` or `agent-owned`. -->
