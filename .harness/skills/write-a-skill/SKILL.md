---
name: write-a-skill
description: Create active repo-native agent skills with trigger-rich descriptions, concise instructions, progressive disclosure, and optional bundled scripts. Use when creating, improving, or reviewing harness skills, Claude skills, OpenCode command skills, or reusable agent procedures.
status: active
source_evidence: https://github.com/mattpocock/skills/blob/main/skills/productivity/write-a-skill/SKILL.md
---

# Write A Skill

Create small, active skills that agents can trigger reliably and improve over time.

## Quick Start

1. Gather the skill requirements:
   - task or domain
   - specific use cases and trigger words
   - whether the work needs deterministic scripts
   - references or examples the agent may need
2. Write `SKILL.md` with a clear frontmatter `description`.
3. Keep the body short and procedural.
4. Split large or rarely used detail into one-level reference files.
5. Add scripts only for deterministic repeated work.
6. Verify the checklist below before considering the skill active.

## Skill Shape

```txt
skill-name/
├── SKILL.md
├── REFERENCE.md
├── EXAMPLES.md
└── scripts/
    └── helper.sh
```

Only `SKILL.md` is required. Add other files when they reduce repeated context or
make a fragile operation deterministic.

## SKILL.md Template

```md
---
name: skill-name
description: Brief capability sentence. Use when specific trigger words, contexts, or file types appear.
status: active
source_evidence: .harness/memory/events.jsonl
---

# Skill Name

One-sentence purpose.

## Quick Start

Minimal procedure.

## Workflow

1. Step one.
2. Step two.
3. Verify evidence.

## Advanced

Read `REFERENCE.md` only when the task needs deeper detail.
```

## Description Rules

- Max 1024 characters.
- Write in third person.
- First sentence says what capability the skill provides.
- Second sentence starts with `Use when` and names specific triggers.
- Avoid vague descriptions like `Helps with docs`.

## Review Checklist

- [ ] Description includes specific `Use when` triggers.
- [ ] `SKILL.md` is concise and procedural.
- [ ] No secrets, credentials, private user memory, or time-sensitive claims.
- [ ] References are one level deep.
- [ ] Scripts are used only for deterministic repeated work.
- [ ] Verification evidence is named.
