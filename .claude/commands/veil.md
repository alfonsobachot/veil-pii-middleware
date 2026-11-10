---
description: Sanitize the raw prompt before it reaches the model, replacing PII with vault tokens.
argument-hint: "<prompt text>"
---

<!--
No `allowed-tools` wildcard grant here on purpose. $ARGUMENTS carries the
user's raw, untrusted prompt text into this bash-execution line, and whether
Claude Code substitutes it as a genuine shell variable or splices it as raw
text before the shell parses the line is undocumented. A wildcard allow-list entry such as
`Bash(python .../sanitize.py:*)` would very likely still prefix-match a
command line an injected `$ARGUMENTS` payload had appended to, silently
auto-approving it. Leaving this ungranted means every invocation goes
through the normal ask-permission flow, so a human sees the exact rendered
command -- injected suffix included, if any -- before it runs.
-->

!`p=.venv/Scripts/python.exe; [ -x "$p" ] || p=.venv/bin/python; "$p" veil_pii_middleware/adapters/command/sanitize.py <<< "$ARGUMENTS"`
