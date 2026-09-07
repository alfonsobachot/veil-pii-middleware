#!/usr/bin/env bash
#
# protect-paths.sh — PreToolUse hook (Edit/Write AND Bash).
#
# Runs BEFORE Claude edits/writes a file or runs a shell command, and blocks
# (exit 2 + reason on stderr) two classes of action:
#
#   1. Path guard (Edit/Write/MultiEdit/NotebookEdit): writes to secrets,
#      lockfiles, CI config, or anything under a secrets/ directory.
#   2. Command guard (Bash): reading a secret file THROUGH THE SHELL,
#      destructive commands — recursive-force rm, find -delete, force-push,
#      git reset --hard / clean -f, kubectl delete, terraform apply|destroy,
#      curl|sh pipe-to-shell — plus WRITING to a protected destination via
#      cp/mv/tee and in-place rewrites of locked verification code via
#      sed/awk/perl -i (CLAUDE_VERIFY_LOCK=1). This mirrors the OpenCode
#      block-dangerous-commands plugin and closes the gap that per-tool `deny`
#      rules cannot: Read(./.env) does NOT stop Bash(cat .env), and
#      Edit-deny does NOT stop Bash(tee .gitlab-ci.yml).
#
# Exit 2 blocks the call even when permissions.allow matches it, so safe
# commands stay allow-listed for UX while this hook owns the dangerous cases.
# Defense-in-depth on top of the settings.json deny rules. Patterns are
# intentionally broad — when in doubt, block and let a human re-run by hand.
# Tune them for your repo. Regexes avoid \b for BSD/macOS grep portability.

set -uo pipefail

payload="$(cat)"

# --- structured-field extraction (jq -> python3) ---------------------------
extract() { # $1 = jq filter, $2 = python expression
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$payload" | jq -r "$1 // empty" 2>/dev/null
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' "$payload" | python3 -c "$2" 2>/dev/null
  else
    return 1
  fi
}

tool="$(extract '.tool_name' 'import sys,json;print(json.load(sys.stdin).get("tool_name","") or "")' || true)"
file="$(extract '.tool_input.file_path // .tool_input.notebook_path // .tool_input.path // .tool_input.filePath // .tool_input.notebookPath' 'import sys,json
d=json.load(sys.stdin); t=d.get("tool_input") or {}
for k in ("file_path","notebook_path","path","filePath","notebookPath"):
    if t.get(k):
        print(t.get(k)); break
else:
    print("")' || true)"
cmd="$(extract '.tool_input.command' 'import sys,json;print((json.load(sys.stdin).get("tool_input") or {}).get("command","") or "")' || true)"

# Fallback: no parser available AND nothing extracted. Scan the raw payload for
# dangerous substrings and fail TOWARD blocking on a match (the old grep path
# failed open).
if [ -z "${tool:-}${file:-}${cmd:-}" ]; then
  if printf '%s' "$payload" | grep -Eiq 'rm[[:space:]]+-[a-z]*r[a-z]*f|rm[[:space:]]+-[a-z]*f[a-z]*r|find[[:space:]].*-delete|git[[:space:]]+push[^"]*(--force|[[:space:]]-f|[[:space:]]\+)|reset[[:space:]]+--hard|clean[[:space:]]+-[a-z]*f|kubectl[[:space:]]+delete|terraform[[:space:]]+(apply|destroy)|(curl|wget)[^"]*\|[[:space:]]*(sh|bash|zsh)|\.env([^.a-zA-Z]|$)|\.pem|\.key|secrets/|id_rsa|credentials'; then
    echo "BLOCKED: could not parse the tool payload (no jq/python3) and it matched a dangerous pattern." >&2
    echo "Install jq or python3 so the guard can inspect commands precisely, or run this by hand after review." >&2
    exit 2
  fi
  exit 0
fi

block() { # $1 = path-or-command, $2 = reason
  echo "BLOCKED: $1 — $2." >&2
  echo "Do not bypass this guard. Secrets: use the approved store, never print them. Lockfiles: regenerate via the package manager. CI: open an MR. Destructive ops: a human runs them after review." >&2
  exit 2
}

# Basename globs that must never be modified by Claude. Shared by the PATH
# GUARD (Edit/Write file_path) and the COMMAND GUARD destination check
# (cp/mv/tee through Bash) — entries are bare basenames because both checks
# compare against `basename`.
PROTECTED=(
  ".env" ".env.*"
  "*.pem" "*.key" "id_rsa*"
  "credentials*" "*.p12" "*.keystore"
  ".gitlab-ci.yml"
  "verify.sh"
  "pnpm-lock.yaml" "package-lock.json" "yarn.lock"
  "poetry.lock" "Cargo.lock" "go.sum"
)

# Verification surface globs, shared by the VERIFY_LOCK branches of both
# guards (Edit/Write below, and sed/awk/perl -i through Bash).
VERIFY_LOCKED=(
  ".harness/bin/verify" "*/.harness/bin/verify"
  ".harness/acceptance/*" "*/.harness/acceptance/*"
  "tests/*" "*/tests/*" "*/__tests__/*"
  "*.test.*" "*.spec.*" "test_*.py" "*_test.go"
)

check_dest() { # echo the last non-flag argument of a command string
  # Tokens arrive via unquoted word-splitting of the raw command string, so a
  # quoted path keeps its literal quote chars and a path with spaces splits
  # into several tokens: `cp --backup=t "/my path/.env"` ends in `path/.env"`.
  # Strip surrounding quotes per token so the trailing component still matches
  # the basename checks (the full spaces path is unrecoverable from split
  # tokens, but every guard below compares basenames or suffix globs).
  local last="" a
  for a in "$@"; do
    a="${a#\"}"; a="${a%\"}"; a="${a#\'}"; a="${a%\'}"
    [ -z "$a" ] && continue
    [[ "$a" == -* ]] || last="$a"
  done
  printf '%s' "$last"
}

# ===========================================================================
# COMMAND GUARD (Bash)
# ===========================================================================
if [ -n "${cmd:-}" ]; then
  # 1) Reading a secret file through the shell (mirror of the Read deny rules).
  #    Excludes .env.example/.sample/.template/.dist (matched as plain .env<X>).
  readers='cat|less|more|head|tail|nl|tac|bat|grep|egrep|fgrep|rg|ag|strings|xxd|od|hexdump|base64|cp|mv|scp|rsync'
  secret_tgt='(\.env([^.a-zA-Z]|$)|\.env\.(local|dev|development|prod|production|staging|test)|[^[:space:]]*\.pem|[^[:space:]]*\.key|id_rsa|credentials|(^|[[:space:]=/])secrets/)'
  # Path-sensitive backstop: block any static shell reference to a protected
  # secret path, not only a small command-name allow list. This catches
  # interpreters, dd if=, input redirects, process substitution tokens, and
  # reader commands such as awk/sed that can print a file without being named in
  # the legacy reader list.
  if printf '%s' "$cmd" | grep -Eiq "$secret_tgt"; then
    block "$cmd" "references a secret file through the shell (.env / *.pem / *.key / secrets/)"
  fi
  if printf '%s' "$cmd" | grep -Eiq '(^|[^[:alnum:]_./-])('"$readers"')([[:space:]]|$)' \
     && printf '%s' "$cmd" | grep -Eiq "$secret_tgt"; then
    block "$cmd" "reads a secret file through the shell (.env / *.pem / *.key / secrets/)"
  fi

  # 2) Recursive-force rm in ANY flag spelling/order (policy denies rm -rf wholesale).
  if printf '%s' "$cmd" | grep -Eiq '(^|[^[:alnum:]_])rm([[:space:]]|$)'; then
    if printf '%s' "$cmd" | grep -Eiq '(-[a-zA-Z]*r|--recursive)' \
       && printf '%s' "$cmd" | grep -Eiq '(-[a-zA-Z]*f|--force)'; then
      block "$cmd" "recursive-force delete (rm with -r and -f, any spelling)"
    fi
  fi

  # 3) Other destructive commands (mirror of the OpenCode plugin) + find delete.
  DANGER=(
    '(^|[^[:alnum:]])find[[:space:]].*-delete::find -delete (bulk delete)'
    '(^|[^[:alnum:]])find[[:space:]].*-exec[[:space:]]+rm::find -exec rm (bulk delete)'
    '(^|[^[:alnum:]])git([[:space:]]+(-C|--git-dir|--work-tree)[[:space:]]+[^[:space:]]+)*[[:space:]]+push([[:space:]][^|;&]*)?([[:space:]](--force|--force-with-lease|-f)([[:space:]]|$)|[[:space:]]\+[[:alnum:]])::force-push with git global options (rewrites/clobbers remote)'
    '(^|[^[:alnum:]])git[[:space:]]+push([[:space:]][^|;&]*)?([[:space:]](--force|--force-with-lease|-f)([[:space:]]|$)|[[:space:]]\+[[:alnum:]])::force-push (rewrites/clobbers remote)'
    '(^|[^[:alnum:]])git([[:space:]]+(-C|--git-dir|--work-tree)[[:space:]]+[^[:space:]]+)*[[:space:]]+reset[[:space:]]+--hard::git reset --hard with git global options (discards working changes)'
    '(^|[^[:alnum:]])git[[:space:]]+reset[[:space:]]+--hard::git reset --hard (discards working changes)'
    '(^|[^[:alnum:]])git([[:space:]]+(-C|--git-dir|--work-tree)[[:space:]]+[^[:space:]]+)*[[:space:]]+clean[[:space:]]+(-[a-zA-Z]*[[:space:]]*)*-[a-zA-Z]*f::git clean -f with git global options (deletes untracked files)'
    '(^|[^[:alnum:]])git[[:space:]]+clean[[:space:]]+(-[a-zA-Z]*[[:space:]]*)*-[a-zA-Z]*f::git clean -f (deletes untracked files)'
    '(^|[^[:alnum:]])kubectl[[:space:]]+delete::kubectl delete (destroys cluster resources)'
    '(^|[^[:alnum:]])terraform[[:space:]]+(apply|destroy)::terraform apply/destroy (mutates infrastructure)'
    '(^|[^[:alnum:]])(curl|wget)[^|]*\|[[:space:]]*(sudo[[:space:]]+)?(sh|bash|zsh)([^[:alnum:]]|$)::pipe-to-shell install (curl|sh)'
    '(^|[^[:alnum:]])(sh|bash|zsh)[^|;&]*<[[:space:]]*(<)?[[:space:]]*\([[:space:]]*(curl|wget)([^[:alnum:]]|$)::process-substitution installer (bash <(curl ...))'
  )
  for entry in "${DANGER[@]}"; do
    re="${entry%%::*}"; reason="${entry##*::}"
    if printf '%s' "$cmd" | grep -Eiq "$re"; then
      block "$cmd" "$reason"
    fi
  done

  # 4) Destination check for cp/mv/tee: per-tool deny rules and the PATH
  #    GUARD below only see Edit/Write file_path — `tee .gitlab-ci.yml` or
  #    `cp evil .env` would sail through. Extract the destination (last
  #    non-flag argument) and run it through the same protections.
  for trigger_cmd in cp mv tee; do
    if printf '%s' "$cmd" | grep -Eiq "(^|[^[:alnum:]_])$trigger_cmd([[:space:]]|\$)"; then
      # shellcheck disable=SC2086
      dest=$(check_dest $cmd)
      dest_base=$(basename "$dest")
      for pat in "${PROTECTED[@]}"; do
        # shellcheck disable=SC2053
        [[ "$dest_base" == $pat ]] && block "$cmd" "destination '$dest' is a protected file (matches '$pat')"
      done
      case "$dest" in
        .github/workflows/*|*/.github/workflows/*|.circleci/*|*/.circleci/*|Jenkinsfile|*/Jenkinsfile|azure-pipelines.yml|*/azure-pipelines.yml|.gitlab-ci.yml|*/.gitlab-ci.yml)
          block "$cmd" "destination '$dest' is CI configuration (change it via a reviewed MR)" ;;
      esac
      if [[ "$dest" == secrets/* || "$dest" == */secrets/* ]]; then
        block "$cmd" "destination '$dest' lives under a secrets/ directory"
      fi
    fi
  done

  # 4b) Shell redirects can write protected files without cp/mv/tee. Catch
  # visible redirect targets for CI and secret paths.
  if printf '%s' "$cmd" | grep -Eiq '(^|[^0-9])>{1,2}[[:space:]]*([^[:space:]]*/)?(\.gitlab-ci\.yml|\.env([^[:alnum:].]|$)|\.env\.(local|dev|development|prod|production|staging|test)|[^[:space:]]+\.(pem|key))'; then
    block "$cmd" "redirects output to a protected file"
  fi

  # 5) VERIFY_LOCK for the shell: during an autonomous run the agent must not
  #    rewrite verification code in place either — sed/awk/perl -i are the
  #    Bash siblings of the Edit/Write lock below. Case-SENSITIVE grep: the
  #    in-place flags are lowercase i (avoid matching perl -Ilib).
  if [ "${CLAUDE_VERIFY_LOCK:-0}" = "1" ]; then
    if printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_])(sed|awk|perl)([[:space:]][^|;&]*)?[[:space:]](-[a-zA-Z]*i|--in-?place)'; then
      # shellcheck disable=SC2086
      tgt=$(check_dest $cmd)
      tgt_base=$(basename "$tgt")
      for pat in "${VERIFY_LOCKED[@]}"; do
        # shellcheck disable=SC2053
        if [[ "$tgt" == $pat || "$tgt_base" == $pat ]]; then
          block "$cmd" "rewrites verification code in place while it is locked (CLAUDE_VERIFY_LOCK=1) — change the implementation, not the test"
        fi
      done
    fi
  fi

  exit 0
fi

# ===========================================================================
# PATH GUARD (Edit/Write/MultiEdit/NotebookEdit)
# ===========================================================================
[ -z "${file:-}" ] && exit 0
base="$(basename "$file")"

# --- Verification immutability (Phase 1) -----------------------------------
# During an autonomous run, the runner sets CLAUDE_VERIFY_LOCK=1 so the agent
# cannot edit the gate that judges it (the dominant "loop until tests pass"
# failure mode is the agent weakening its own checks). OFF by default, so
# human-driven test writing is never blocked.
if [ "${CLAUDE_VERIFY_LOCK:-0}" = "1" ]; then
  for pat in "${VERIFY_LOCKED[@]}"; do
    # shellcheck disable=SC2053
    if [[ "$file" == $pat || "$base" == $pat ]]; then
      echo "BLOCKED: '$file' is verification code and is locked during an autonomous run (CLAUDE_VERIFY_LOCK=1)." >&2
      echo "The agent may not modify the gate that verifies it. Change the implementation, not the test." >&2
      exit 2
    fi
  done
fi

# Basename globs that must never be modified by Claude (PROTECTED is defined
# at the top of the file, shared with the COMMAND GUARD destination check).
for pat in "${PROTECTED[@]}"; do
  # shellcheck disable=SC2053
  if [[ "$base" == $pat ]]; then
    block "$file" "protected file (matches '$pat') — secret, lockfile, or CI config"
  fi
done

# Path-based protections (basename matching is too coarse for these).
if [[ "$file" == secrets/* || "$file" == */secrets/* ]]; then
  block "$file" "lives under a secrets/ directory"
fi
case "$file" in
  .github/workflows/*|*/.github/workflows/*|.circleci/*|*/.circleci/*|Jenkinsfile|*/Jenkinsfile|azure-pipelines.yml|*/azure-pipelines.yml)
    block "$file" "is CI configuration (change it via a reviewed MR)" ;;
esac

exit 0
