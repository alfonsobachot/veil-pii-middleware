---
description: Review the current changes (or a given diff range) for quality, security, and convention violations.
argument-hint: "[optional: a git ref range like main..HEAD]"
allowed-tools: Read, Grep, Glob, Bash(git diff:*), Bash(git log:*), Bash(git status), Task
---

Review code changes for this project.

Scope of the diff to review (default to unstaged + staged vs. `main` if no arg):
- Range: ${ARGUMENTS:-"working tree vs. main"}

Steps:
1. Gather the diff. If an argument was given, use `git diff $ARGUMENTS`. Otherwise
   show `git status` and `git diff` (staged and unstaged).
2. Delegate the review to the `code-reviewer` subagent so it runs in its own
   context. Pass it the diff and the relevant file contents.
3. Summarize its findings here, grouped as **Blocking**, **Should-fix**, and
   **Nits**. For each, give file:line and a concrete suggested change.
4. Do NOT edit code in this command — just report. The developer decides what to fix.
