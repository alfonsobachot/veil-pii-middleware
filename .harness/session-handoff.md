# Session Handoff

Mid-task continuation bridge for the NEXT agent session. Not the evidence
package — that is `.harness/handoff.md`. Write this when stopping mid-task
(context budget, phase boundary, end-of-day). Rules and thresholds:
`.harness/docs/context-management.md`.

Quality bar: a fresh session with no access to the current conversation must
be able to execute the exact next step without re-deriving it.

Never put secrets in a handoff. Reference where credentials live; do not
copy them.

## Mission

<PLACEHOLDER: one paragraph — what the overall work is for and what "done" means>

## Current State

<PLACEHOLDER: which phase/step is active; what is finished, in progress, untouched>

Execution lane: <PLACEHOLDER: agent-owned | agent-with-checkpoint | human-owned>
Risk class: <PLACEHOLDER: low | medium | high>

## Completed Files

<PLACEHOLDER: every file created or modified, one-line purpose each>

- path/to/file — purpose

## Source-Of-Truth Files

<PLACEHOLDER: files the next session MUST read before acting; keep minimal>

- path/to/file — why required

## Active Decisions

<PLACEHOLDER: decisions already made that the next session must not re-litigate>

- decision — rationale

## Blocked Questions

<PLACEHOLDER: open questions needing a human or external answer; who owns each>

- question — owner — what unblocks it

## Exact Next Step

<PLACEHOLDER: one concrete action, executable as written — include the command or file edit>

## Validation Performed

<PLACEHOLDER: commands run and their results; quote failures exactly>

- command — result

## Residual Risks

<PLACEHOLDER: what could be wrong despite the validation above; known gaps>

- risk — likelihood — mitigation

## Status

not started
