#!/usr/bin/env bash
#
# verify-gate.sh — Stop hook. Makes the verification loop autonomous.
#
# When the agent tries to finish its turn, this runs the verification gate
# (.harness/bin/verify, which includes tests). If the gate is RED, it blocks the
# stop (exit 2) and sends the failures back so the agent keeps fixing — without
# the developer ever typing /verify or /test. The loop is bounded so it can
# never run forever or trap the developer.
#
# Safeguards:
#   - runs the gate at Stop time unless explicitly disabled
#   - skips when no gate is configured
#   - capped at CLAUDE_VERIFY_MAX forced iterations (default 3), then lets the
#     stop through with a clear RED warning
#   - honours stop_hook_active as an additional infinite-loop guard
#   - opt out entirely with CLAUDE_VERIFY_GATE=0

set -uo pipefail

[ "${CLAUDE_VERIFY_GATE:-1}" = "0" ] && exit 0

payload="$(cat)"
field() {
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$payload" | jq -r --arg k "$1" '.[$k] // empty' 2>/dev/null
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' "$payload" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print(''); sys.exit(0)
print(d.get('$1',''))" 2>/dev/null
  else
    printf '%s' "$payload" | grep -Eo "\"$1\"[[:space:]]*:[[:space:]]*(\"[^\"]*\"|[A-Za-z0-9_.-]+)" | head -1 | sed -E 's/.*:[[:space:]]*//; s/^"//; s/"$//'
  fi
}

ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
VERIFY="$ROOT/.harness/bin/verify"
# A missing gate is a fail-CLOSED condition: deleting .harness/bin/verify must
# not silently disarm the Stop hook (eval EV-03).
if [ ! -f "$VERIFY" ]; then
  echo "BLOCKED: .harness/bin/verify is missing — gate cannot run. Restore the file before stopping." >&2
  exit 2
fi

# Counter identity first: the green path must be able to reset it. The
# fallback sid hashes the project dir (no $$ — the PID changes on every hook
# subprocess and would defeat the counter) so parallel worktree sessions
# without session ids do not share one counter (eval EV-28).
MAX="${CLAUDE_VERIFY_MAX:-3}"
sid="$(field session_id)"
[ -z "$sid" ] && sid="default-$(printf '%s' "${CLAUDE_PROJECT_DIR:-$(pwd)}" | cksum | cut -d' ' -f1)"
# Counter lives under a per-user state dir, NOT /tmp: a predictable
# world-writable /tmp path can be pre-seeded by another user on a shared
# machine to skew the retry budget (hardening review 2026-06). If the dir
# cannot be created the counter writes fail silently and n stays 0 — the
# loop is still bounded by the stop_hook_active release below.
STATE_DIR="${XDG_RUNTIME_DIR:-${HOME:-/tmp}/.cache}/harness"
mkdir -p "$STATE_DIR" 2>/dev/null && chmod 700 "$STATE_DIR" 2>/dev/null
ctr="$STATE_DIR/verify-gate.${sid}"

out="$(bash "$VERIFY" 2>&1)"; rc=$?
if [ $rc -eq 0 ]; then rm -f "$ctr"; exit 0; fi   # green -> allow stop, reset retry budget (eval EV-01)
[ $rc -eq 2 ] && exit 0   # gate not configured -> nothing to enforce

# rc == 1: the gate is failing. Bound the forced-fix loop.
n=0; [ -f "$ctr" ] && n="$(cat "$ctr" 2>/dev/null || echo 0)"
[[ "$n" =~ ^[0-9]+$ ]] || n=0   # a corrupt counter must not break the arithmetic guard
stop_active="$(field stop_hook_active)"

if [ "$n" -ge "$MAX" ] || [ "$stop_active" = "True" ] || [ "$stop_active" = "true" ]; then
  rm -f "$ctr"
  # A failing CONTRACT row is paperwork, not code — the cap release must not
  # read as permission to keep implementing against an invalid mission (EV-14).
  if printf '%s' "$out" | grep -q '✗.*contract'; then
    echo "CONTRACT GATE FAILED: mission routing fields are invalid. Do not proceed to implementation — fix .harness/mission.md first." >&2
  fi
  echo "Verification still RED after ${n} attempt(s). Allowing stop — but .harness/bin/verify is failing and must be fixed before this is merged." >&2
  exit 0
fi

# Atomic write (mktemp + rename): parallel Stop hooks sharing a fallback sid
# (e.g. two worktree sessions without session ids in the same project dir)
# must never interleave a partial counter read.
if tmpctr="$(mktemp "${ctr}.XXXXXX" 2>/dev/null)"; then
  echo $((n + 1)) > "$tmpctr" && mv -f "$tmpctr" "$ctr"
fi
echo "Verification gate FAILED (.harness/bin/verify). Do not finish yet: read the failures below, fix the implementation (never weaken or delete tests), and continue until the gate is green. Attempt $((n + 1)) of ${MAX}." >&2
echo "----- gate output (tail) -----" >&2
printf '%s\n' "$out" | tail -n 20 >&2
exit 2
