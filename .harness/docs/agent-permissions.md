# Agent Permissions Policy

This document defines the permission baseline for agents working in a Blueprint
repository. It is tool-neutral and normative. Claude Code, OpenCode, Codex, and
other adapters may express the controls differently, but they must preserve the
same outcomes.

## Safety Model

Use three layers:

1. **Platform permissions and sandboxing are hard boundaries.** They decide what
   an agent can technically read, write, execute, or reach.
2. **Hooks and plugins are deterministic tripwires.** Use them only for small,
   high-confidence checks that platform rules cannot express reliably.
3. **Human approval owns high-blast-radius decisions.** Automation may prepare
   evidence, but a human decides whether the action proceeds.

Permissions are the primary boundary. A hook may add a block or force an approval,
but it must not be treated as a way to bypass a platform deny. Prompt instructions
guide behavior; they are not security controls.

## Default Policy

### Agents May

Allow reversible work inside the active repository:

| Capability | Examples |
|---|---|
| Read and search project files | source, tests, documentation, configuration without secrets |
| Edit assigned project files | source, tests, documentation, local fixtures |
| Run project verification | unit tests, lint, typecheck, local build, `.harness/bin/verify` |
| Inspect Git state | `status`, `diff`, `log`, branches |
| Create local development artifacts | tests, docs, local branches, non-secret generated output |
| Use temporary workspace storage | isolated fixtures and test installs under approved temp paths |

Allow rules should be narrow enough to exclude secrets and external paths, but broad
enough that ordinary local development does not require repeated approval.

### Agents Must Ask

Require explicit approval or platform escalation for operations that are legitimate
but can alter dependencies, external state, or a large part of the repository:

| Capability | Examples |
|---|---|
| Network access | web requests, downloads, remote APIs, package registries |
| Dependency changes | install, upgrade, remove, or regenerate lockfiles |
| Remote Git writes | push a branch, create or update a merge request |
| Broad repository changes | mass rename, large generated rewrite, deleting many files |
| Schema and data changes | create or run migrations, data backfills |
| Delivery configuration | CI/CD, Docker, Terraform, Kubernetes, deployment files |
| Sensitive product areas | auth, permissions, billing, secrets handling |
| External system writes | Jira, Confluence, cloud services, databases, MCP/app mutations |
| Out-of-workspace access | reading or writing paths outside the approved workspace |
| Long or expensive work | unusually long test suites, scans, or resource-heavy jobs |

Approval must be scoped to the concrete action. Do not request broad permanent
access when a one-command or one-domain approval is sufficient.

### Agents Must Not

Deny actions that are destructive, expose secrets, bypass review, or mutate
production:

| Capability | Examples |
|---|---|
| Destructive Git | force-push, `reset --hard`, forced `clean`, rewriting shared history |
| Broad destructive shell | recursive force-delete, bulk delete without reviewed scope |
| Secret access or disclosure | reading, printing, copying, committing, or editing credentials |
| Production or cloud mutation | deploy, infrastructure apply/destroy, real-cluster mutation |
| Destructive database operations | `DROP DATABASE`, unreviewed `TRUNCATE`, destructive production SQL |
| Unreviewed remote execution | `curl ... \| sh`, process-substitution installers |
| Permission bypass | disabling sandboxing, approval rules, hooks, or verification controls |
| Agent-driven acceptance | merge, approve, mark Done, or waive required human review |
| Unsafe broad permission changes | recursive `chmod 777`, broad ownership changes |

If a denied action is genuinely necessary, the agent prepares the exact command,
impact, rollback, and evidence. A human performs or explicitly authorizes it through
the platform's protected approval path.

## Human Review Gates

The following changes always require a human verification guide and a before-merge
review:

- authentication, authorization, permissions, secrets, or sensitive data;
- payments, billing, pricing, or financial behavior;
- database migrations, backfills, retention, or destructive data operations;
- infrastructure, deployment, production configuration, or cloud resources;
- breaking public APIs or externally consumed schemas;
- destructive commands or recovery operations;
- broad refactors whose impact cannot be established by deterministic checks.

There is no auto-merge. Passing tests proves configured checks passed; it does not
replace product, architecture, security, or operational judgment.

## What Hooks And Plugins Should Check

Use a hook or plugin only when all of these are true:

1. The unsafe action is expensive or dangerous.
2. Detection is deterministic from the tool call or changed path.
3. False positives are rare and have a clear human escape path.
4. Platform permissions or sandboxing cannot express the rule sufficiently.

Good tripwires include:

- secret and credential paths;
- recursive destructive commands;
- force-push, hard reset, and forced clean;
- production or infrastructure mutation commands;
- pipe-to-shell execution;
- protected verification files during autonomous runs;
- the required mission state and final verification gate.

Do not use hooks to judge architecture, product intent, test quality, or whether a
large change is conceptually correct. Those belong to validation and human review.

## Adapter Mapping

### Claude Code

- Express the baseline through `.claude/settings.json`
  `permissions.allow`, `permissions.ask`, and `permissions.deny`.
- Keep bypass permission modes disabled for shared repositories.
- Use `PreToolUse` hooks only as deterministic backstops. A hook block can stop an
  otherwise allowed call, but a hook allow does not override matching ask or deny
  rules.
- Use sandboxing when OS-level filesystem or network enforcement is required.

### OpenCode

- Express the baseline through `opencode.jsonc` permission rules.
- Put catch-all rules first and specific exceptions later because the last matching
  rule wins.
- Keep `external_directory` access narrow and explicitly approved.
- Use local plugins for deterministic command and path checks; do not describe them
  as merely advisory when they throw before tool execution.

### Codex

- Use the platform sandbox as the technical boundary: workspace-scoped writes and
  network disabled by default.
- Use the approval policy for out-of-workspace writes, network access, and other
  elevated actions.
- Keep repo-specific behavioral rules in `AGENTS.md`, but do not mistake those
  instructions for enforcement.
- Where repo hooks are unavailable, manually apply the protected-action deny-list
  in this document and run `.harness/bin/verify` before claiming completion.

## Adding A Rule

Before adding a permission or tripwire, answer:

1. Has the failure happened, or is the impact clearly severe?
2. Is the proposed owner correct: permission, hook, or human review?
3. Can the check be deterministic?
4. Is there a nearby benign case that must remain allowed?
5. Will the rule preserve stronger local or managed policy?

Add the smallest rule that controls the blast radius. Test both a blocked case and a
benign case. Never weaken an existing deny, sandbox boundary, test, or verification
gate to reduce friction.

## References

- [Claude Code permissions](https://code.claude.com/docs/en/permissions)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [OpenCode permissions](https://opencode.ai/docs/permissions/)
- [Codex agent approvals and security](https://developers.openai.com/codex/agent-approvals-security)
