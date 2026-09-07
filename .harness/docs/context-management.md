# Context Management

Context exhaustion is one of the main agent failure modes. An agent that runs
out of context mid-task produces truncated work, loses decisions, and forces a
human to reconstruct state. Manage the context budget as deliberately as the
task itself.

Adopted from the agent-practices bundle (decision record:
`.harness/adapter.yaml` pins `contract-version: 1`). This page is
tool-neutral; tool differences are named at the end.

## Two Handoff Files, Two Audiences

| File | Audience | When |
|---|---|---|
| `.harness/handoff.md` | The human reviewing the finished task | End of task — evidence package, verdict, human verification guide |
| `.harness/session-handoff.md` | The next agent session continuing the same task | Mid-task — context budget, phase boundary, or end-of-day stop |

Never merge them: evidence answers "can this be merged?", continuation
answers "what is the exact next step?".

## Context Budget Thresholds

Measure context as the fraction of the agent's window already consumed
(conversation, file reads, tool output). Apply this operating rule:

| Context Used | Rule |
| --- | --- |
| 0–40% | Continue normal work. |
| 40–60% | Finish the current small step only. Prepare a session handoff before starting a new phase. |
| 60–75% | Do not start a new implementation phase. Checkpoint, summarize, and compact. |
| 75%+ | Stop expanding context. Produce a session handoff only. |

When the exact percentage is unavailable, estimate from signals: number of
large file reads, repeated tool output, or the tool's own context warnings.
When in doubt, treat the budget as one band worse than estimated.

## The Four Responses

- **Continue** — keep working normally. Allowed only in the 0–40% band, or to
  finish an already-started small step in the 40–60% band.
- **Checkpoint** — commit or write the current state to disk so that no
  in-context-only knowledge remains. A checkpoint is durable: files, commits,
  or an updated mission/validation document — never just a chat summary.
- **Compact** — reduce context occupancy without losing decisions: summarize
  completed work, drop stale file contents, and re-read only what the next
  step needs. Compact after a checkpoint, not instead of one.
- **Hand off** — write `.harness/session-handoff.md` so a fresh session can
  continue without the current conversation. Mandatory at 75%+; recommended
  at any phase boundary in the 40–60% band.

## Rules That Prevent Exhaustion

- Read only the files the current step needs. Missions and validation
  contracts should name their required reading explicitly so a fresh session
  does not re-explore.
- Delegate large reads and searches to subagents (`project-interviewer`,
  read-only research) that return conclusions, not file dumps.
- Prefer short factual docs over large pasted doctrine; link instead of
  embedding.
- Never start a new phase of multi-phase work above 60% context. Phase
  boundaries are the natural handoff points.
- Write durable state (mission updates, decision records, completed-file
  lists) as you go, so a forced handoff loses nothing.

## Handoff Quality Bar

A session handoff is complete when a fresh session, with no access to the
current conversation, can:

1. State the mission and current phase.
2. Name every completed file and every source-of-truth file.
3. List active decisions and blocked questions.
4. Execute the exact next step without re-deriving it.
5. Know what validation already ran and what risks remain.

If any of those require the old conversation, the handoff is incomplete.

The resuming session reads `.harness/mission.md` first, then
`.harness/session-handoff.md`, then only the source-of-truth files the
handoff names.

## Tool Notes

- **Claude Code** — the status line and `/context` show window occupancy;
  auto-compaction may summarize older context, but do not rely on it to
  preserve decisions — checkpoint to disk first. Use subagents for broad
  searches; their context is disposable and only the conclusion returns.
  `/compact` is the manual trigger; run it after a checkpoint.
- **OpenCode** — the orchestrator agent runs one serial task; treat session
  restarts like any fresh session (mission first, then session handoff).
- **Codex** — no compaction equivalent; long sessions degrade silently.
  Apply the thresholds more conservatively (treat 60% as the hard phase
  boundary). Session state is not recoverable across runs, so
  `.harness/session-handoff.md` is the only bridge — full paths and exact
  commands, no references to "as discussed".

Never put secrets in a handoff. Reference where credentials live; do not
copy them.
