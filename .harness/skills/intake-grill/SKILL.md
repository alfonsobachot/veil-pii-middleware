---
name: intake-grill
description: Interview the user with structured clarification questions until shared understanding is reached before any new task or feature work. Use when starting a mission, running /start-mission or /orchestrate, or whenever a prompt is vague (e.g. "fix the issue", "add search") and the functional requirements, scope, acceptance criteria, edge cases, or risk are not yet clear.
status: active
source_evidence: https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md
---

# Intake Grill

Interview the user relentlessly about every aspect of the task until shared
understanding is reached, then record the answers before writing validation or
code. Adapted from Matt Pocock's `grill-me` skill.

## Quick Start

Three steps:

1. Explore the repo for discoverable answers: use Read, Glob, Grep on
   CLAUDE.md, existing tests, and src/ structure.
2. Ask 3-6 questions using AskUserQuestion (Claude Code) or the question
   tool (OpenCode). Recommended option first, up to 4 questions per round.
3. Record every answer in the `## Intake Grill` table in
   `.harness/mission.md` before writing `.harness/validation.md` or
   changing code.

## When It Runs

At the start of every new task or feature, before editing implementation files.
This is the Mandatory Intake Grill from `CLAUDE.md`.

## Explore First

Walk each branch of the design tree and resolve dependencies between decisions
one by one. If a question can be answered by exploring the repository, explore
the repo instead of asking. Ask only about gaps that change the task definition.

## Ask With Structured Question Tools

Gather answers with the platform's structured question tool, not free chat:

- **Claude Code — `AskUserQuestion`.** Send up to 4 questions per call, each with
  2-4 options, a header of 12 characters or fewer, and the recommended option
  first, labeled `(Recommended)`. An automatic "Other" free-text choice is always
  available to the user.
- **OpenCode — `question` tool.** Provide a header, the question text, and an
  options list with the recommended default first; the user may type a custom
  answer and navigate multiple questions before submitting.
- **No question tool (e.g. Codex):** fall back to plain chat, one question at a
  time, still recommending a default answer for each.

Ask in rounds of up to 4 themed questions per tool call. Run rounds sequentially
so each round resolves the dependent decision branches the previous answers
opened.

## Cover 3-6 Questions

Across the rounds, cover:

- user problem and desired outcome
- target users or affected workflow
- scope, non-goals, and protected areas
- acceptance criteria and observable behavior
- edge cases, data, integrations, and dependencies
- risk level and human verification needs
- execution lane and review triggers (default `agent-with-checkpoint`;
  `agent-owned` only per `.harness/docs/operating-model.md`
  `## Execution Lanes` — low risk plus a configured executable gate; high
  risk is never agent-owned)

## Done When

Grill is complete when all five conditions hold; see [completion.md](completion.md).
Waived conditions must be recorded as assumptions in the `## Intake Grill` table.

## Design Round

This extension is available only when the `design` installation profile is
selected and `design-round.md` exists beside this file. It fires when the
Tooling And Approach Assessment classifies a visual surface. Skip it when the
profile or reference file is absent, and for backend/CLI/infra projects.

Ask ≤4 pick-from-options questions; record all answers in the `## Intake Grill`
table of `.harness/mission.md`. When installed, `design-round.md` contains the
full question set, vibe presets, and recording format.

## Record The Answers

Write every answer into the `## Intake Grill` question/answer table in
`.harness/mission.md` before writing `.harness/validation.md` or changing code.
Record the lane decision in the mission's `## Routing` rows and the risk
answer as its `Risk class:` line; the gate's `contract` row checks both.

If the user refuses clarification, record the missing information as assumptions
in `.harness/mission.md` and keep the change small.

## Attribution

Adapted from Matt Pocock's `grill-me` skill:
https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md
