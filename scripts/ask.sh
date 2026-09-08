#!/usr/bin/env bash
# Send one message through the veil gateway to a real `claude -p` process.
#
# Runs from a fixed, neutral scratch directory (~/.veil-cli-scratch)
# regardless of where this script itself is invoked from, so no project's
# CLAUDE.md (this repo's included) ever loads into the request.
#
# Usage: scripts/ask.sh "your message here"
# Override the gateway URL (default http://127.0.0.1:8787) with:
#   VEIL_GATEWAY_URL=http://127.0.0.1:8788 scripts/ask.sh "..."
set -euo pipefail

if [ $# -eq 0 ]; then
  echo "usage: $(basename "$0") <message>" >&2
  exit 1
fi

: "${VEIL_GATEWAY_URL:=http://127.0.0.1:8787}"

SCRATCH="$HOME/.veil-cli-scratch"
mkdir -p "$SCRATCH"
cd "$SCRATCH"

ANTHROPIC_BASE_URL="$VEIL_GATEWAY_URL" claude -p "$*"
