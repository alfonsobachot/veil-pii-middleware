#!/usr/bin/env bash
# Launches Claude Code with the demo's PreToolUse/PostToolUse hooks active
# (see demo/settings.hooks.json and README.md's "Running the demo (hooks)").
#
# Exists purely to remove two sources of friction that have nothing to do
# with the project's own design: locating the `claude` binary (the VSCode
# extension bundles its own copy under a version-numbered path, not on
# PATH) and remembering the `--settings` invocation. Run from Git Bash.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if command -v claude >/dev/null 2>&1; then
    bin=claude
elif [ -x "$HOME/.local/bin/claude.exe" ]; then
    bin="$HOME/.local/bin/claude.exe"
elif [ -x "$HOME/.local/bin/claude" ]; then
    bin="$HOME/.local/bin/claude"
else
    bin=$(find "$HOME/.vscode/extensions" -maxdepth 1 -iname 'anthropic.claude-code-*' 2>/dev/null \
        | sort -V | tail -n1)/resources/native-binary/claude.exe
    if [ ! -x "$bin" ]; then
        echo "run_demo.sh: could not find a claude binary on PATH or under" \
             "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/" >&2
        exit 1
    fi
fi

if [ -z "${VEIL_VAULT_KEY:-}" ]; then
    echo "run_demo.sh: VEIL_VAULT_KEY is not set in this shell -- the hooks" \
         "will fail closed (blocked commands / no tokenization) until it is." >&2
fi

if [ -z "${VEIL_LLM_BASE_URL:-}" ] || [ -z "${VEIL_LLM_MODEL:-}" ]; then
    echo "run_demo.sh: VEIL_LLM_BASE_URL/VEIL_LLM_MODEL are not set -- the hooks" \
         "will run regex-only, so names, organisations and addresses are not" \
         "tokenized. Export both (see CLAUDE.md) for the full detector." >&2
fi

# Same per-call LLM timeout as scripts/run-gateway.sh: the hooks call the LLM too.
export VEIL_LLM_TIMEOUT="${VEIL_LLM_TIMEOUT:-30}"

# Windows Python defaults to cp1252; Claude Code reads Bash output as UTF-8.
export PYTHONUTF8=1
exec "$bin" --settings demo/settings.hooks.json
