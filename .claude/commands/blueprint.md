---
description: One-shot project setup. Takes a freshly cloned blueprint repo to a fully configured, verified, ready-to-work state — then offers the optional packs. This is the single command a new developer runs.
argument-hint: "[optional: one line describing the project and its mandatory deliverables]"
allowed-tools: Read, Edit, Write, Glob, Grep, Task, Bash(.harness/bin/verify:*), Bash(chmod:*), Bash(ls:*), Bash(cat:*), Bash(cp:*), Bash(git status), Bash(git diff:*)
---

# /blueprint — set up this project

Bring this repository from "just cloned" to "configured and verified." Optional seed
describing the project and what it must deliver: **$ARGUMENTS**

Pre-fill every answer you can detect, ask only about genuine gaps, and **write nothing
until the developer approves the proposed diff.** Never request secret values — only
the environment-variable names where they live. Respect protected paths.

## Stage 0 — Preconditions
1. Confirm this is a blueprint repo (`CLAUDE.md` and `.claude/` exist). If not, stop.
2. Make scripts executable: `chmod +x .claude/hooks/*.sh .harness/bin/verify`.
3. Delegate to the `project-interviewer` subagent to detect the stack, commands, test
   runner, and CI. Use its findings to pre-fill the interview below.

## Stage 1 — Interview (ask only the gaps; confirm what was detected)
Run these short rounds. Number the questions so the developer can answer briefly.

- **Identity.** Project name; "my project is for…" (what it does, who uses it);
  type (service / app / library / CLI / monorepo); lifecycle stage.
- **Mandatory deliverables.** "It's mandatory to create…" — e.g. REST API, CLI,
  database schema, test suite, docs, Docker. These drive the scaffolded stubs.
- **Commands (exact).** install · dev · build · test (all) · test (single file) ·
  lint · typecheck · format. Pre-fill from detection; ask only for what's missing.
- **Conventions.** Conventional Commits? Which CI system? Branching model.

## Stage 2 — Propose the core configuration (one diff)
Present a single diff for approval covering ONLY the core:

- `CLAUDE.md` — every section filled from the answers; no leftover `<TODO>` in the
  overview, stack, or commands sections.
- `.claude/settings.json` — `permissions.allow` tightened to the project's real
  commands; project-specific `deny`/`ask` entries added.
- `.harness/bin/verify` — the `GATES` array filled with the project's real lint,
  typecheck, test, and build commands (the verification contract).
- Stub directories for each mandatory deliverable, plus `SETUP-CHECKLIST.md`.

Wait for explicit approval. After approval, write the files and re-run
`chmod +x .claude/hooks/*.sh .harness/bin/verify`.

## Stage 3 — Smoke-test the gate
Run `.harness/bin/verify --json` and confirm it is **configured** (must not exit 2). At
setup time it need not be green — there may be no code yet — but every configured
command must resolve. Correct any wrong `GATES` entry and re-run. Report what is wired.

## Stage 4 — Readiness report
Produce a READY / NOT READY checklist: hooks executable; `verify.sh` configured (exit
0 or 1, never 2); `CLAUDE.md` has no leftover `<TODO>` in critical sections;
`SETUP-CHECKLIST.md` lists the deliverables. State the verdict plainly, then propose
committing with `/commit`. Do not push.

## Stage 5 — Offer the optional packs (do NOT install unless asked)
Optional layers live under `optional/` in the blueprint repository — they are not
shipped by the installer. List them and ask which, if any, the developer wants to
activate now. To activate a pack, clone the blueprint repo to a temp dir, copy the
pack's files into place (e.g. `cp -r <clone>/optional/<pack>/.claude/* .claude/`
and any `commands/`, `agents/`, docs into their matching locations), delete the
clone, then re-run `/blueprint` Stage 4 if needed.

Available packs:
- **process** — spec-driven delivery loop (`/triage`, `/spec`, `/adr`, `ways-of-working.md`
  with the canonical-systems order), plus reusable skills, dual-stack CI templates, and
  MR/PR/Jira/Confluence templates. Adds discipline; adds ceremony.
- **integrations** — MCP connectors for Jira, Confluence, GitLab (`mcp.json`).
- **security** — `/security-scan`, the `security-auditor` subagent, the `security-review`
  skill, and threat-model / secrets-handling templates.
- **docs** — `/fetch-stack-docs` to distill stack documentation into `docs/stack/`.
- **multi-tool** — OpenCode / Codex config (plugins, agents, commands) for teams using
  non-Claude tools alongside Claude Code.
- **modality starter templates** — `backend`, `ux`, `api`, `qa` reusable template sets.

**Recommend, do not impose.** A good default: suggest **integrations** + **process** +
**docs** for most projects; add **security** when the work touches auth/authz, billing,
PII, a public API, or infrastructure; add the **qa**/**ux**/**backend**/**api** templates
that match the deliverables from Stage 1; offer **multi-tool** only if the team uses
OpenCode or Codex. Install only what the developer chooses.

Finish by confirming the project is ready and that day-to-day work uses the core
commands: `/review` and `/commit`. Testing and verification run automatically (the Stop hook enforces the gate after every change).
