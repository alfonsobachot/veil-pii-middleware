---
description: Stage and commit the current changes with a well-formed Conventional Commit message.
argument-hint: "[optional: a hint about what changed]"
allowed-tools: Bash(git status), Bash(git diff:*), Bash(git add:*), Bash(git commit:*), Bash(git log:*)
---

Create a commit for the current changes.

Context hint from the user: $ARGUMENTS

Current state:
- Status: !`git status --short`
- Staged diff: !`git diff --cached --stat`
- Unstaged diff: !`git diff --stat`

Steps:
1. Review what changed. If nothing is staged, stage the relevant files (do NOT
   blindly `git add -A` — exclude anything that looks generated or unrelated).
2. Write a **Conventional Commits** message:
   - `type(scope): summary` where type ∈ feat, fix, chore, docs, refactor, test,
     perf, build, ci. Summary ≤ 72 chars, imperative mood.
   - Add a body explaining the *why* if the change isn't trivial.
   - Reference the issue/MR if known (e.g. `Refs JIRA-123`).
3. Show me the proposed message and the file list, then commit on approval.
4. Do not push. Pushing requires explicit confirmation (it's an `ask` permission).
