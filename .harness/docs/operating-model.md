# Harness Contract

The full normative reference for the Blueprint Harness contract in this
repository. `CLAUDE.md` carries the short `## Harness Contract` summary and
points here; this file carries the detail. These rules apply to every agent
session — Claude Code, OpenCode, Codex, or any other tool. Project facts live in
`CLAUDE.md`; current task state lives in `.harness/*`.

Permission ownership is defined in
`.harness/docs/agent-permissions.md`: platform permissions and sandboxing are hard
boundaries, hooks/plugins are deterministic tripwires, and high-blast-radius
actions remain human-owned.

## Operating Loop

1. Intake Grill asks 3-6 clarification questions before any new task or feature work.
2. Orchestrator mode writes `.harness/mission.md`.
3. Orchestrator mode adds `## Tooling And Approach Assessment` to identify implied
   methods, tools, risks, missing decisions, validation impact, and what is not
   needed now.
4. Orchestrator mode writes `.harness/validation.md`.
5. The human approves the mission, approach assessment, risk, and validation
   (lane-aware: see `## Execution Lanes` — `agent-owned` may skip this stop
   only under the three conditions there; the merge decision stays human).
6. Worker mode implements one coherent change and loops on `.harness/bin/verify`.
7. Scrutiny Validator mode checks code-level correctness.
8. User-Testing Validator mode checks behavior-level correctness.
9. The worker writes `.harness/handoff.md`.
10. The worker records durable memory for gotchas, decisions, repeated procedures,
   and skill lifecycle events.
11. If the same procedure or gotcha is likely to repeat, the worker creates or
   improves an active skill with evidence.
12. The human reviews the handoff, memory/skill diffs, and decides whether to merge
   or roll back the self-evolved skill.

Scrutiny Validator and User-Testing Validator are role modes. The shared
`code-reviewer` and `goal-verifier` subagents carry their checks under the same
names in both adapters.

Use `/start-mission <task>` as the recommended entrypoint for new work.
Claude Code can also run the loop with `/orchestrate <task>`. OpenCode can run the
same loop with `/start-mission <task>` or `/orchestrate-task <task>`. These commands
automate the sequence inside one explicit session, but they pause for human approval
after mission, approach assessment, and validation are written. They are not
background runners, not queues, not daemons, and not auto-merge paths. When the
human starts new work in plain chat, tell them `/start-mission <task>` is the
entrypoint, then run that loop — do not assume they have read any documentation.

## Execution Lanes

Every mission declares routing rows in `.harness/mission.md`; the
`.harness/bin/verify` `contract` row checks them through `.harness/adapter.yaml`
(vendored agent-practices checker):

```text
Execution lane: agent-owned | agent-with-checkpoint | human-owned
Human review trigger: comma-separated (before-code, before-merge, ...)
Delegation mechanism: main-session | subagent | worktree-agent | ...
Validation plan: .harness/validation.md
Risk class: low | medium | high   (a `Risk class:` line in ## Risk)
```

Lane semantics — this section is the single normative home; commands and
templates point here:

- **`agent-with-checkpoint` (default).** Today's loop unchanged: hard human
  approval after mission + validation, human merge decision at the end.
- **`agent-owned`.** The pre-code approval stop may be skipped ONLY when all
  three hold:
  1. `Risk class` is `low`;
  2. the routing contract passes — `.harness/internal/check-mission-contract`
     exits 0;
  3. `.harness/bin/verify` has at least one configured project gate (its exit
     code is not `2`).
  If any condition fails, fall back to the checkpoint stop and say why.
  Record the skip in `## Status` (`agent-owned — implementing` or
  `agent-owned — proceeding without pre-code approval` — the agent-owned
  equivalents of `approved — implementing`, accepted by
  `.claude/hooks/mission-state-guard.sh`); the human still reviews at merge.
- **`human-owned`.** The agent prepares mission and validation, then hands
  over; the human implements.

Invariant, machine-checked by the contract gate: **high risk is never
agent-owned**. `before-merge` stays on every lane — there is no auto-merge.

### CLAUDE_VERIFY_LOCK

For autonomous runs — any session expected to loop on the gate without a
human reading each turn (`agent-owned` lanes, batch runs, CI-adjacent
experiments) — export the lock before starting the agent:

```bash
export CLAUDE_VERIFY_LOCK=1
claude
```

What it protects: the path guard (`.claude/hooks/protect-paths.sh`) blocks
Edit/Write to `.harness/bin/verify`, `.harness/acceptance/*`, and the test globs
(`tests/*`, `*.test.*`, `*.spec.*`, `test_*.py`, `*_test.go`), and blocks
`sed/awk/perl -i` rewrites of the same files through Bash.

Why it matters: the dominant autonomous failure mode is "loop until tests
pass" resolving into *weakening the tests*. With the lock on, the agent can
only change the implementation, never the gate that judges it. OFF by
default (`0`) so human-driven test writing is never blocked. The installed
permission model and protected-action policy are documented in
`.harness/docs/agent-permissions.md`.

## Mandatory Intake Grill

Every new task or feature starts with clarification. Do not implement from vague
prompts like "fix the issue" or "add search" without first gathering the functional
requirements.

Ask 3-6 questions that cover:

- user problem and desired outcome
- target users or affected workflow
- scope, non-goals, and protected areas
- acceptance criteria and observable behavior
- edge cases, data, integrations, and dependencies
- risk level and human verification needs
- execution lane and review triggers (default `agent-with-checkpoint`;
  `agent-owned` only per `## Execution Lanes`)

Run this through the `.harness/skills/intake-grill/SKILL.md` skill. Explore the
repo instead of asking when the answer is discoverable, and recommend a default
answer for each question. Gather answers with the platform's structured question
tool — Claude Code's `AskUserQuestion` or OpenCode's `question` tool — in rounds
of up to 4 themed questions, recommended option first; fall back to plain
one-question-at-a-time chat only when no question tool exists. Write the
answers into `.harness/mission.md` before writing `.harness/validation.md` or
changing code. If the user refuses clarification, record the missing information
as assumptions in `.harness/mission.md` and keep the change small.

## Tooling And Approach Assessment

Every new task must include this required mission section before validation:

```md
## Tooling And Approach Assessment
```

The assessment answers:

1. What project type is this?
2. What methods, tooling, or approaches does that imply?
3. What risks or missing decisions does it reveal?
4. What should be added to validation?
5. What is explicitly not needed now?

Keep the assessment constrained. It is a guard against missing domain decisions, not
a broad tool-recommendation engine.

## Visual-Surface Gate and DESIGN.md

This section is active only when the `design` installation profile is selected
and `.harness/skills/design-direction/SKILL.md` is present. Without that profile,
record visual requirements through the normal Intake Grill and project
validation; do not assume design-generation tooling exists.

When the Tooling And Approach Assessment classifies the project as having a
**visual surface** — frontend, landing page, web app, design system, or docs
site — the following rules apply. Backend, CLI, and infrastructure projects
skip this section entirely.

### Visual-surface trigger

A visual-surface project is one where at least one of these is true:
- The deliverable includes HTML/CSS/JS rendered in a browser.
- The task description names a landing page, web app, design system, or docs site.
- The existing codebase has a `src/` or `app/` directory containing `.tsx`,
  `.jsx`, `.vue`, `.svelte`, or `.html` files with user-facing content.

If uncertain, default to non-visual (skip the design round).

### Read-before-UI rule

**Every worker or validator session that touches UI code must read `.harness/design.md`
before writing or reviewing any component, page, or stylesheet.**

- If `.harness/design.md` exists at the project root, read it first. It is the single
  source of truth for color tokens, typography, spacing, radius, and component
  decisions. Do not invent values already specified there.
- If `.harness/design.md` is absent, note the absence in the mission Assessment and
  either (a) trigger the design round via the intake-grill skill to generate it,
  or (b) record the missing design intent as a risk in `.harness/handoff.md`.

### Design round (intake-grill integration)

For new visual-surface projects or major UI reworks, the intake-grill skill fires
a gated design round (≤4 questions) after the Tooling And Approach Assessment
confirms a visual surface:

1. **Target & domain** — who is this for and what is the product domain?
2. **Vibe preset** — clean-SaaS / warm-editorial / bold-brutalist / playful /
   minimal-corporate (or describe your own; unsure? see Linear, Stripe, Apple).
3. **Reference sites** — any sites whose look you want to match or avoid?
4. **Brand basics** — existing logo, colors, or fonts? (skip if none yet)

Optional: tooling question for opt-in open-design MCP / design skill.

Answers are recorded in the `## Intake Grill` table in `.harness/mission.md`.
The design-direction skill then renders 2–3 self-contained HTML design directions;
the user picks one; `.harness/design.md` is generated directly from the chosen direction
in the eight-section Google `design.md` format. The throwaway HTML is discarded
after the pick.

### Soft conformance

Design conformance is **guidance, not a hard gate**. No new row is added to
`.harness/bin/verify` for DESIGN.md. Instead:

- Scrutiny Validator (code-reviewer) reads `.harness/design.md` when present and flags
  token / aesthetic drift as a finding in `.harness/handoff.md`.
- User-Testing Validator (goal-verifier) notes whether the delivered UI matches
  the chosen direction and records it in the handoff verdict.

The human approver reviews drift findings before merging — the gate never
breaks on design issues, but drift is always surfaced.

## Context Management

Multi-session work must survive context exhaustion. The full doctrine —
budget thresholds (0–40 / 40–60 / 60–75 / 75%+), the four responses
(continue, checkpoint, compact, hand off), and the quality bar — lives in
`.harness/docs/context-management.md`. The normative rule:

- Before stopping mid-task, write `.harness/session-handoff.md` (the
  continuation bridge). `.harness/handoff.md` stays the end-of-task evidence
  package; never merge the two.
- A resuming session reads `.harness/mission.md` first, then
  `.harness/session-handoff.md`, then only the source-of-truth files the
  handoff names.
- Above 60% context, do not start a new implementation phase. At 75%+,
  produce the session handoff and stop.
- Never put secrets in a handoff.

## High-Risk Review

High-risk changes require a human verification guide in `.harness/handoff.md`.
High-risk triggers include auth, permissions, payments, data migrations, production
configuration, infrastructure, deployment, secrets, destructive commands, and
breaking public APIs.

There is no auto-merge.

## Persistent Memory And Self-Improving Skills

Every detected error can become persistent project memory, and repeated procedures
can become active self-evolving skills.

Memory files:

- `.harness/memory/events.jsonl` — append-only event log.
- `.harness/memory/lessons.md` — distilled human-readable lessons.

Skill files:

- `.harness/skills/<name>/SKILL.md` — canonical repo-native skill.
- `.opencode/skills/<name>/SKILL.md` — OpenCode-native skill mirror.
- `.claude/skills/<name>/SKILL.md` — Claude-compatible skill mirror.
- `.opencode/commands/<name>.md` — OpenCode slash-command wrapper, not the skill.

When a gate, validator, user correction, or review finds an error, record:

- trigger: how the error was detected
- symptom: what failed or was wrong
- root cause: why it happened
- fix: what changed
- prevention: what future agents should check
- evidence: command, test, validator note, or file reference

Use:

```bash
.harness/bin/memory record --type gotcha --summary "..." --source .harness/handoff.md --prevention "..."
.harness/bin/memory distill
```

Autonomous skill creation is active when backed by evidence:

```bash
.harness/bin/skill create --name <kebab-slug> --trigger "When to use it" --summary "Procedure summary" --evidence ".harness/handoff.md"
.harness/bin/skill improve --name <kebab-slug> --evidence ".harness/memory/events.jsonl" --change "What changed and why"
```

Rules:

- Do not store secrets, credentials, or private user memory.
- Do not mutate gates, tests, Jira, Confluence, or external systems automatically.
- Human review is the audit and rollback path, not the activation step.
- If one weak signal creates a skill, keep its procedure narrow and record evidence
  for the next improvement pass before relying on it for critical work.

## No Platform Creep

Do not add these to the default core:

- No runtime services or background runners.
- No private user memory, external databases, or messaging integrations.
- No dashboard infrastructure.

Keep queue-style execution and advanced adapters future or optional.
