# Claude Code Profile

The Claude profile installs `.claude/` as an adapter over the core harness.

## Surfaces

- `.claude/commands/` — `/start-mission`, `/orchestrate`, `/blueprint`,
  `/review`, and `/commit`.
- `.claude/agents/` — project interview, test execution, code review, and goal
  verification roles.
- `.claude/hooks/` — deterministic mission, path, formatting, and verification
  tripwires.
- `.claude/settings.json` — project permission and hook wiring.
- `.claude/skills/` — mirrors of the canonical selected skills under
  `.harness/skills/`.

Claude-specific files enforce or expose the shared contract; they do not replace
`AGENTS.md`, `CLAUDE.md`, or `.harness/docs/operating-model.md`.

Local credentials and personal settings belong in untracked local configuration,
never in committed adapter files.
