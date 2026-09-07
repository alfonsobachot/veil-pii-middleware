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

exec "$bin" --settings demo/settings.hooks.json
