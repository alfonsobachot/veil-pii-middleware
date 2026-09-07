#!/usr/bin/env bash
#
# format.sh — PostToolUse hook
# Runs after Claude edits/writes a file. Formats that single file with whatever
# formatter your project uses, so Claude's output always matches house style.
#
# Claude Code passes hook input as JSON on stdin. We pull out the file path and
# format only that file. We never fail the tool call just because a formatter is
# missing — formatting is best-effort.
#
# Customize the per-language commands below for your stack.

set -euo pipefail

# --- Read the file path from the hook payload on stdin ---------------------
payload="$(cat)"

extract_path() {
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$payload" | jq -r '.tool_input.file_path // .tool_input.notebook_path // .tool_input.path // .tool_input.filePath // .tool_input.notebookPath // empty'
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' "$payload" | python3 -c 'import sys,json
t=json.load(sys.stdin).get("tool_input",{})
for k in ("file_path","notebook_path","path","filePath","notebookPath"):
    if t.get(k):
        print(t.get(k)); break
else:
    print("")'
  else
    # crude fallback
    printf '%s' "$payload" | grep -o '"file_path"[^,]*' | head -1 | sed 's/.*: *"//; s/"$//'
  fi
}

file="$(extract_path || true)"
[ -z "${file:-}" ] && exit 0           # nothing to format
[ -f "$file" ] || exit 0               # file may have been deleted/moved

have() { command -v "$1" >/dev/null 2>&1; }

case "$file" in
  *.ts|*.tsx|*.js|*.jsx|*.mjs|*.cjs|*.json|*.css|*.scss|*.md|*.html|*.yaml|*.yml)
    if have pnpm && [ -f package.json ];   then pnpm exec prettier --write "$file" 2>/dev/null || true
    elif have npx;                          then npx --no-install prettier --write "$file" 2>/dev/null || true
    fi
    ;;
  *.py)
    if have ruff;   then ruff format "$file" 2>/dev/null || true; ruff check --fix "$file" 2>/dev/null || true
    elif have black; then black -q "$file" 2>/dev/null || true
    fi
    ;;
  *.go)
    have gofmt && gofmt -w "$file" 2>/dev/null || true
    ;;
  *.rs)
    have rustfmt && rustfmt "$file" 2>/dev/null || true
    ;;
  *.java|*.kt)
    : # plug in google-java-format / ktlint here
    ;;
  *)
    : # no formatter configured for this type
    ;;
esac

exit 0
