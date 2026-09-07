#!/usr/bin/env bash
#
# grill-guard.sh — PreToolUse hook (Edit/Write/MultiEdit/NotebookEdit).
#
# Mechanically enforces the Mandatory Intake Grill: implementation files may
# not be edited while `.harness/mission.md`'s `## Intake Grill` section is
# absent or still all-placeholder. Layer 3 of 3 — the vendored contract
# checker and the check-mission-contract.sh fast path catch the same state at
# gate time; this hook catches it at edit time.
#
# Design approved by human review 2026-06-07 (remediation-plan-2026-06 MR-G1,
# eval EV-36). Graceful degradations:
#   - no `.harness/mission.md`  -> exit 0 (plain repo, no harness state)
#   - target under `.harness/`  -> exit 0 (the agent fills the grill itself)
#   - prose-only grill (no table rows) -> exit 0 (only tables are machine-judged)
#
# Sentinel: created ONLY when the check PASSES — never on first invocation —
# so an agent cannot defeat the guard by triggering it once and riding
# sentinel skips while the grill stays unfilled. It only saves re-parsing the
# mission on every subsequent Edit in a session. It lives under a per-user
# state dir (${XDG_RUNTIME_DIR:-$HOME/.cache}/harness), NOT /tmp: a
# predictable world-writable /tmp path could be pre-created by another user
# on a shared machine to bypass the guard (hardening review 2026-06). If the
# state dir cannot be created the sentinel is simply skipped — the check then
# runs on every edit (fail closed, slower, never weaker).

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

file="$(extract '.tool_input.file_path // .tool_input.notebook_path // .tool_input.path // .tool_input.filePath // .tool_input.notebookPath' 'import sys,json
d=json.load(sys.stdin); t=d.get("tool_input") or {}
for k in ("file_path","notebook_path","path","filePath","notebookPath"):
    if t.get(k):
        print(t.get(k)); break
else:
    print("")' || true)"
[ -z "${file:-}" ] && exit 0

ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
MISSION="$ROOT/.harness/mission.md"
[ -f "$MISSION" ] || exit 0

case "$file" in
  .harness/*|*/.harness/*) exit 0 ;;
  # Windows: the host normalizes file_path to backslashes, so the
  # forward-slash patterns above never match there. Without this line the
  # exemption is dead on that platform and the guard deadlocks — the agent
  # cannot fill the grill it is being blocked for not having filled.
  '.harness\'*|*'\.harness\'*) exit 0 ;;
esac

sid="$(extract '.session_id' 'import sys,json;print(json.load(sys.stdin).get("session_id","") or "")' || true)"
[ -z "$sid" ] && sid="default-$(printf '%s' "${CLAUDE_PROJECT_DIR:-$(pwd)}" | cksum | cut -d' ' -f1)"
# Per-user state dir — see the sentinel note in the header.
STATE_DIR="${XDG_RUNTIME_DIR:-${HOME:-/tmp}/.cache}/harness"
mkdir -p "$STATE_DIR" 2>/dev/null && chmod 700 "$STATE_DIR" 2>/dev/null
SENTINEL="$STATE_DIR/grill-checked.${sid}"
[ -f "$SENTINEL" ] && exit 0

block() {
  cat >&2 <<'MSG'
BLOCKED: Intake Grill not completed. Run /start-mission or /orchestrate
to interview the user first; record real answers in .harness/mission.md
## Intake Grill before editing implementation files.
MSG
  exit 2
}

grep -q '^## Intake Grill' "$MISSION" || block

# Non-header, non-separator table rows inside the grill section.
rows="$(awk '/^## Intake Grill/{f=1;next} /^#/{f=0} f && /^\|/ && $0 !~ /^[| :-]+$/ && $0 !~ /^\|[[:space:]]*Question/' "$MISSION")"
if [ -n "$rows" ] && ! printf '%s\n' "$rows" | grep -qv '<PLACEHOLDER'; then
  block
fi

# Check passed — drop the sentinel so later edits skip the re-parse. A
# failed write (unwritable state dir) just means re-checking next time.
: > "$SENTINEL" 2>/dev/null || true
exit 0
