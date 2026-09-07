# Persistent Memory And Self-Improving Skills

Blueprint implements a repo-native version of the Hermes learning loop. It persists
project lessons, creates active skills from repeated evidence, and improves those
skills from later runs. It does not add a daemon, private user memory, external
database, or messaging gateway.

## Memory Store

Persistent project memory lives in:

```txt
.harness/memory/events.jsonl
.harness/memory/lessons.md
```

Use memory for:

- gotchas found by gates, validators, users, or review
- repeated procedures agents should reuse
- decisions that explain why the harness behaves a certain way
- skill creation and skill improvement evidence

Do not store:

- secrets, tokens, or credentials
- private user memory
- personal data
- unreviewed external content copied from Jira, Confluence, chat, or logs

Commands:

```bash
.harness/bin/memory record --type gotcha --summary "..." --source .harness/handoff.md --prevention "..."
.harness/bin/memory list
.harness/bin/memory distill
```

## Skill Lifecycle

All generated skills use the default `write-a-skill` structure, based on Matt
Pocock's skill-writing guidance:

```txt
.harness/skills/write-a-skill/SKILL.md
.claude/skills/write-a-skill/SKILL.md
.opencode/skills/write-a-skill/SKILL.md
.opencode/commands/write-a-skill.md
```

The other default skills — `jira-triage`, `confluence-docs`, `intake-grill`, and
`design-direction` — ship in the same layout: canonical under `.harness/skills/`,
mirrored into both adapters, and wrapped as OpenCode commands.

The key defaults are:

- trigger-rich frontmatter description
- description includes `Use when`
- concise `SKILL.md`
- progressive disclosure through one-level reference files
- scripts only for deterministic repeated work
- evidence named in the skill

Repo-native skills live in:

```txt
.harness/skills/<skill-name>/SKILL.md
```

When a skill is created, the harness also mirrors it into tool adapters:

```txt
.claude/skills/<skill-name>/SKILL.md
.opencode/skills/<skill-name>/SKILL.md
.opencode/commands/<skill-name>.md
```

The `.opencode/skills/` file is the native OpenCode skill discovered by the `skill`
tool. The `.opencode/commands/` file is only a slash-command wrapper.

Creation command:

```bash
.harness/bin/skill create \
  --name repeated-login-debug \
  --trigger "Debug repeated login failures. Use when auth-related tasks fail around login, sessions, cookies, or redirects." \
  --summary "Inspect auth config, session storage, logs, and regression tests in order." \
  --evidence ".harness/handoff.md"
```

Improvement command:

```bash
.harness/bin/skill improve \
  --name repeated-login-debug \
  --evidence ".harness/memory/events.jsonl" \
  --change "Add a session-cookie expiry check before changing auth code."
```

## Self-Evolving Rule

Autonomous creation and self-improvement are active operations. When
`.harness/bin/skill create` writes `.harness/skills/<name>/SKILL.md` and its
Claude/OpenCode mirrors, agents may use that skill immediately in the current repo.
When `.harness/bin/skill improve` appends evidence-backed changes, the updated
skill is immediately active.

Human review remains the audit and rollback path, not the activation step. If a
self-evolved skill causes bad behavior, revert or improve the skill through normal
git review and record the lesson in memory.

Every skill must:

- have a narrow trigger-rich description containing `Use when`
- cite evidence
- include a repeatable procedure
- preserve the harness gate
- avoid secrets and private memory
- avoid external mutation without explicit human confirmation

## Relationship To Hermes

Hermes has persistent memory, skill creation, and skill self-improvement inside an
agent runtime. Blueprint keeps those ideas repo-native and self-evolving:

```txt
Hermes runtime memory  -> .harness/memory/events.jsonl
Hermes learned skills  -> .harness/skills/<name>/SKILL.md
Hermes self-improvement -> .harness/bin/skill improve
Audit / rollback       -> git diff + .harness/bin/verify
```
