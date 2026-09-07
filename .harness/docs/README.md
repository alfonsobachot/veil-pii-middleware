# Installed Harness Guide

Cactus Blueprint adds a small repository-native contract around agentic work:
clarify the task, define validation before implementation, run one gate, and
hand evidence to a human reviewer.

## Start Here

1. Read `AGENTS.md` and the project-adapted `CLAUDE.md`.
2. Start work with `/start-mission <task>`, or follow
   [operating-model.md](operating-model.md) manually when the active tool has no
   command adapter.
3. Configure and run `.harness/bin/verify`.
4. Use the map below only when the current task needs more detail.

## Core Files

```text
AGENTS.md
CLAUDE.md
.harness/
  mission.md
  validation.md
  handoff.md
  session-handoff.md
  bin/verify
  bin/memory
  bin/skill
  docs/
  memory/
  skills/
```

The manifest at `.harness/manifest.json` records the installed version,
selected profiles, owned paths, and source hashes.

## Documentation Map

| Need | Read |
|---|---|
| Task loop, lanes, Intake Grill, approval, and risk | [operating-model.md](operating-model.md) |
| Agent permissions and human gates | [agent-permissions.md](agent-permissions.md) |
| Context limits and session continuation | [context-management.md](context-management.md) |
| Persistent memory and repo-native skills | [memory-and-skills.md](memory-and-skills.md) |
| Branches, commits, and merge readiness | [git-workflow.md](git-workflow.md) |
| Worktree use and cleanup | [worktrees.md](worktrees.md) |
| Upgrade and collision recovery | [upgrade.md](upgrade.md), [collision-recovery.md](collision-recovery.md) |

Selected profiles may add adapter, GitLab, Atlassian, design, or planning
documents to this directory. Their absence means the profile is not installed.

## Configure The Project

After installation:

1. Fill every `<PLACEHOLDER>` in `CLAUDE.md`.
2. Add the project’s real lint, typecheck, test, build, and acceptance commands
   to the `GATES` array in `.harness/bin/verify`.
3. Run `.harness/bin/verify`.

Exit codes:

```text
0 = all configured checks pass
1 = at least one check fails
2 = no project checks are configured
```

## Daily Loop

```text
Intake Grill
  -> mission and risk
  -> validation contract
  -> human approval when required
  -> implementation
  -> .harness/bin/verify
  -> independent validation
  -> handoff
  -> human merge decision
```

Never weaken a check to get green. Never store secrets or private user data in
harness memory. Ask before external writes, destructive actions, production
changes, deploys, pushes, or merges.

## Maintenance

The installer is not copied into this project. Install, verify, upgrade, and V1
migration commands run from a fresh blueprint clone. See [upgrade.md](upgrade.md)
and [collision-recovery.md](collision-recovery.md).
