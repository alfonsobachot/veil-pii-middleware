# Repo-Native Skills

This directory stores persistent, active skills created from repeated harness
evidence.

Skill lifecycle:

1. Memory event or handoff identifies a repeated procedure or gotcha.
2. `.harness/bin/skill create` writes `.harness/skills/<name>/SKILL.md`.
3. The script mirrors the skill to `.claude/skills/<name>/SKILL.md` and
   `.opencode/skills/<name>/SKILL.md`, and writes the wrapper
   `.opencode/commands/<name>.md`.
4. Agents may use the skill immediately and record evidence.
5. `.harness/bin/skill improve` appends evidence-backed improvements.
6. Humans review the diff as the audit and rollback path.

Rules:

- Skills must cite evidence.
- Skills must stay narrow and procedural.
- Skills must not contain secrets, credentials, or private user memory.
- Skills must not mutate gates, tests, Jira, Confluence, or external systems without
  explicit human approval.
