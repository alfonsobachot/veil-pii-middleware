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

# Optional LLM detection layer (names, addresses, ...). Override by exporting
# these yourself before calling this script; unset them to run regex-only.
export VEIL_LLM_BASE_URL="${VEIL_LLM_BASE_URL:-http://192.168.31.123:1234}"
export VEIL_LLM_MODEL="${VEIL_LLM_MODEL:-qwen_qwen3-4b-instruct-2507}"

echo "veil: panel at http://127.0.0.1:$PORT/veil-panel"
echo "veil: point a client at it with ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT (see scripts/ask.sh)"
exec .venv/Scripts/python -m veil_pii_middleware.adapters.gateway.server "$PORT"
