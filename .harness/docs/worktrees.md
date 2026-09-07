# Worktrees

Use Git worktrees to isolate agent tasks that might collide. Worktrees are useful
for parallel attempts, risky refactors, tool-specific sessions, or experiments.

Do not use worktrees as a substitute for clear task scope. One worktree still runs
one coherent task.

## When To Use A Worktree

Use a worktree when:

- two agents might edit overlapping files
- one agent is trying an experiment while another keeps the main branch clean
- a task is risky and should not disturb the primary checkout
- Claude Code, Codex, and OpenCode sessions need separate working trees
- a branch needs to be reviewed while new work continues elsewhere

For tiny docs or single-file fixes, a normal branch in the main checkout is usually
enough.

## Recommended Layout

```txt
~/.codex/worktrees/<project>/<branch>
<repo>/.claude/worktrees/<branch>
```

Use the Codex path for Codex-managed sessions. Use the project-local Claude path
for Claude Code worktrees. Keep branch names unique and descriptive.

## Preflight

Before creating a worktree:

1. Confirm the base checkout is clean.
2. Update or confirm the latest `develop`.
3. Define the task in `.harness/mission.md`.
4. Write `.harness/validation.md`.
5. Choose a branch name using `.harness/docs/git-workflow.md`.
6. Confirm the worktree path is not already in use.

Example:

```bash
git fetch origin
git switch develop
git pull --ff-only
git worktree add ~/.codex/worktrees/my-project/feat-ENG-123-search -b feat/ENG-123-search-navbar develop
```

Adjust the path and branch name for the project and tool.

## During Work

- Keep the task serial inside the worktree.
- Run `.harness/bin/verify` before handoff.
- Record evidence in `.harness/handoff.md`.
- Do not merge from a dirty or unverified worktree.
- Do not use one worktree for multiple unrelated tasks.

## Closeout

Before merging:

1. Run `.harness/bin/verify`.
2. Run validator modes or configured review agents.
3. Write `.harness/handoff.md`.
4. Ask the human to review the handoff.
5. Merge only after human approval.

After merge:

```bash
git worktree list
git status --short
git worktree remove <path>
git branch -d <branch>
```

Only remove a worktree when it is clean and the branch is merged or intentionally
abandoned.

Never delete a dirty worktree without explicit human confirmation. If a worktree has
uncommitted changes, report the path, branch, and status, then wait for direction.

## Harness State and Parallel Worktrees

`.harness/` is branch-scoped: each worktree branch has isolated harness
state after the Preflight step. `harness-memory.sh` writes to the worktree
root's `.harness/memory/events.jsonl`. When two parallel worktree branches
merge, `events.jsonl` uses the `union` merge driver (`.gitattributes`) —
both sides' events are taken. Review merged events for duplicates (same
timestamp + summary). There is no automated deduplication.

## Stale Worktrees

At mission start, if the repository has worktrees beyond the main checkout,
check for leftovers and report them — never act on them:

```bash
git worktree list
git branch -vv
```

Flag to the human any worktree whose branch is already merged into the default
branch, or whose tree is dirty with no active task. Report path, branch, and a
one-line status; then continue the mission. Removal follows the Closeout
section above — verify the branch is merged into the *updated* default branch
(pull first) before removing; clean and merged may be removed when the human
confirms, dirty is never removed without explicit human confirmation.

## Agent Rule

Agents may create or use worktrees when the task benefits from isolation. Agents
must not remove dirty worktrees, discard uncommitted work, or force-delete branches
unless the human explicitly asks for that destructive cleanup.
