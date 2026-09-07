#!/usr/bin/env bash
#
# mission-state-guard.sh — PreToolUse hook (Edit/Write/MultiEdit/NotebookEdit).
#
# Mechanically enforces the before-code approval checkpoint: implementation
# files may only be edited while `.harness/mission.md` `## Status` records an
# approved (or legitimately agent-owned) state. Until then the loop's stages
# 1-4 — grill, mission, assessment, validation — are paperwork, and paperwork
# lives under `.harness/`, which this guard exempts.
#
# Design approved by human review 2026-06-07 (remediation-plan-2026-06 MR-E2,
# eval EV-17). Graceful degradations:
#   - no `.harness/mission.md`           -> exit 0 (plain repo, no harness state)
#   - target file outside the guard set  -> exit 0
#   - target under `.harness/`           -> exit 0 (the agent fills its own paperwork)
#
# Status values that allow implementation (prefix match on the first
# non-empty line under `## Status`):
#   approved …      e.g. `approved — implementing`
#   agent-owned …   e.g. `agent-owned — implementing` or
#                   `agent-owned — proceeding without pre-code approval`
#   (the agent-owned skip itself is legitimate ONLY when the three conditions
#   in .harness/docs/operating-model.md `## Execution Lanes` hold; the contract
#   gate enforces the risk side)
#
# MISSION_GUARD_PATHS overrides the guarded glob set (space-separated globs,
# matched against the full path and the basename). The override is a
# trusted-operator surface: anything that can set the hook's environment can
# already edit settings.json, so env-settability adds no new exposure — but
# the glob handling below is load-bearing, see the set -f note.

set -uo pipefail

payload="$(cat)"

extract() {
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$payload" | jq -r "$1 // empty" 2>/dev/null
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' "$payload" | python3 -c "$2" 2>/dev/null
  else
    return 1
  fi
}

file="$(extract '.tool_input.file_path' 'import sys,json;print((json.load(sys.stdin).get("tool_input") or {}).get("file_path","") or "")' || true)"
[ -z "${file:-}" ] && exit 0

ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
MISSION="$ROOT/.harness/mission.md"
[ -f "$MISSION" ] || exit 0

# The agent must be able to write its own paperwork.
case "$file" in
  .harness/*|*/.harness/*) exit 0 ;;
esac

# Guarded implementation surface (override with MISSION_GUARD_PATHS).
GUARD_PATHS="${MISSION_GUARD_PATHS:-src/* */src/* tests/* */tests/* *.ts *.js *.py *.go *.rs}"
base="$(basename "$file")"
guarded=0
# SECURITY BOUNDARY, not a style choice: without noglob, patterns like `*.ts`
# expand against whatever the hook's cwd happens to contain before reaching
# [[ == ]], silently SHRINKING the guarded set (a cwd with no .ts files would
# unguard every .ts edit). Any edit to this loop must keep the set -f/+f
# wrapping intact — the reconstruction gate pins it.
set -f
for pat in $GUARD_PATHS; do
  # shellcheck disable=SC2053
  if [[ "$file" == $pat || "$base" == $pat ]]; then
    guarded=1
    break
  fi
done
set +f
[ "$guarded" = "1" ] || exit 0

# First non-empty line after the `## Status` heading.
status="$(awk '/^## Status/{f=1;next} f && NF {print; exit}' "$MISSION")"

case "$status" in
  approved*|agent-owned*) exit 0 ;;
esac

echo "BLOCKED: mission status is '${status:-<empty>}' — not approved for implementation." >&2
echo "Set ## Status to 'approved — implementing' after human approval, or confirm this is an agent-owned task with all three skip conditions met (check-mission-contract.sh exits 0, verify.sh exits != 2, risk != high) and record the agent-owned status." >&2
exit 2
