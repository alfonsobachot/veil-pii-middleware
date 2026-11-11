#!/usr/bin/env bash
# Quick-start the veil gateway for manual testing.
#
# Not a production launcher: the vault key is persisted to a plaintext file
# (.veil/vault.key, git-ignored) purely for convenience across restarts, not
# read from a secure store. Fine for local dev/testing, never for real use.
#
# Usage: scripts/run-gateway.sh [port]   (default port: 8787)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${1:-8787}"
KEY_FILE=".veil/vault.key"

mkdir -p .veil
if [ ! -f "$KEY_FILE" ]; then
  .venv/Scripts/python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())" > "$KEY_FILE"
  echo "veil: generated a new vault key at $KEY_FILE"
fi
export VEIL_VAULT_KEY
VEIL_VAULT_KEY="$(cat "$KEY_FILE")"

# Optional LLM detection layer (names, addresses, ...). Export both
# VEIL_LLM_BASE_URL and VEIL_LLM_MODEL yourself before calling this script to
# enable it (e.g. an LM Studio/Ollama instance on your own LAN or machine);
# leave them unset to run regex-only -- there is no default host here, since
# any fixed default would point at somebody's personal machine.
if [ -n "${VEIL_LLM_BASE_URL:-}" ]; then
  export VEIL_LLM_BASE_URL
fi
if [ -n "${VEIL_LLM_MODEL:-}" ]; then
  export VEIL_LLM_MODEL
fi

# The 10 s default per-call timeout is too tight for a local model under
# load; a timeout falls back to regex-only, so names would go out in clear.
# 30 s was not enough either: qwen3 4b needs ~45 s for a 52-row table dump
# (about 8 KB of query output), which the demo model does produce.
export VEIL_LLM_TIMEOUT="${VEIL_LLM_TIMEOUT:-120}"

# Load the model now and pin it in memory (Ollama unloads it after 5 idle
# minutes), then check that the detector reaches it. Warns loudly on failure
# but still starts the gateway, which then runs regex-only.
.venv/Scripts/python -m veil_pii_middleware.demo.warmup || true

echo "veil: panel at http://127.0.0.1:$PORT/veil-panel"
echo "veil: point a client at it with ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT (see scripts/ask.sh)"
exec .venv/Scripts/python -m veil_pii_middleware.adapters.gateway.server "$PORT"
