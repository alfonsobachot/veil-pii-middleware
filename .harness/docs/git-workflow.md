# Git Workflow

This is the default Blueprint Harness Git workflow.
Keep it simple: short-lived branches, verified handoffs, human-reviewed merges.

## Branch Model

Default model:

```txt
develop -> feature/fix/docs branch -> merge request -> develop
main = release branch
```

- Branch from `develop`.
- Merge back to `develop` through a merge request.
- Keep `main` for releases.
- Treat `develop` and `main` as protected branches.
- Never commit directly to `develop` or `main`.
- Never force-push a shared branch.

If a project uses a different branching model, document that override in the
project's `CLAUDE.md` and keep the harness verification and handoff rules.

## Branch Names

Use:

```txt
<type>/<jira-key-or-topic>-<short-slug>
```

Examples:

```txt
feat/ENG-123-search-navbar
fix/ENG-456-login-timeout
docs/confluence-index
refactor/ENG-789-auth-boundaries
```

Types:

- `feat`
- `fix`
- `docs`
- `test`
- `refactor`
- `chore`
- `security`
- `hotfix`

Use a Jira key when the work has one. For docs-only or internal harness work, use a
clear topic slug.

## Commits

Use Conventional Commits:

```txt
type(scope): short imperative summary
```

Examples:

```txt
feat(search): add navbar query input
fix(auth): handle expired session
docs(harness): document worktree cleanup
```

When a Jira issue exists, include it in the body or footer:

```txt
Refs ENG-123
```

Do not hide unrelated work in the same commit. Prefer a small coherent history.

## Merge Readiness

A branch is ready for human review when:

- `.harness/mission.md` and `.harness/validation.md` match the work.
- `.harness/bin/verify` passes.
- `.harness/handoff.md` records commands, evidence, files changed, gotchas, risk,
  and verdict.
- Verifier evidence exists for the acceptance criteria.
- High-risk work has a completed human verification guide.
- Jira links, Confluence links, and merge request links are recorded when relevant.

The human decides merge. There is no auto-merge in v1.

## CI Verification

`.harness/docs/gitlab-ci-verify.yml` is the minimal GitLab CI job for the
gate. A **human** copies it into `.gitlab-ci.yml` (or `include:`s it) and
adapts the image — agents must not create or edit CI configuration.

At the merge boundary the gate's exit semantics tighten: locally
`.harness/bin/verify` may exit `2` (unconfigured) while a repo is being set
up; in CI any non-zero exit — including `2` — fails the job. An
unconfigured repo cannot pass review.

## Post-Merge Closeout

Branch hygiene is part of the workflow, not an afterthought. Create merge
requests with the platform's delete-source-branch-on-merge option enabled
(GitLab: `--remove-source-branch` or the project's "Delete source branch"
default; other hosts have an equivalent setting). After the human merges, the
agent runs the closeout:

1. `git checkout develop` (or the project's default branch) and
   `git pull --ff-only`. If the pull fails, the local default branch has
   diverged — stop and ask the human before continuing.
2. `git branch -d <branch>` — delete the merged local branch (`-d`, never `-D`).
   If Git refuses, the branch tip is not reachable from the default branch:
   either the branch is not fully merged, or the project squash/rebase-merges
   (which rewrites the tip). Stop and ask the human.
3. `git fetch --prune` — drop stale remote-tracking refs (not local branches —
   step 2 handles those).
4. Re-run `.harness/bin/verify` on the merged default branch and report the exit
   code.

The result: the default branch carries the work, no merged branch lingers
locally, and the remote branch is deleted when the platform's auto-delete is
enabled. Long-lived local branches should be the exception and deliberate.

## Protected Work

Require human review before merging changes that touch:

- auth, permissions, secrets, payments, or sensitive data
- migrations or destructive data changes
- production config, infrastructure, CI, deployment
- public APIs or breaking contracts
- lockfiles and dependency policy
- harness verification policy

If the work is high risk, keep Jira in `In Review` until the human verification
guide is completed and recorded.
