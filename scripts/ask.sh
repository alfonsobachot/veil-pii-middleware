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
#
# Pass --demo-db before the message to test the demo-database scenario
# instead: activates demo/settings.hooks.isolated.json's PreToolUse/
# PostToolUse hooks (de-tokenize before the Bash call runs, re-tokenize its
# output) while still running from the neutral scratch dir, same as the
# default path above -- NOT from the repo root. Confirmed live: Claude Code
# walks up parent directories looking for CLAUDE.md, so even starting from
# a subdirectory of this repo (e.g. demo/) still loads it; the only way to
# keep this repo's own CLAUDE.md (which describes this very PII-blocking
# design) out of the subprocess's context is a directory with no repo
# ancestry at all. That used to mean choosing between isolation and the
# demo-db hooks; now VEIL_REPO_ROOT plus the isolated settings file's hook
# commands (which cd into it themselves) gives both at once. The message is
# given the repo's absolute path automatically, since the demo db is no
# longer reachable via a relative path from the session's own cwd:
#   scripts/ask.sh --demo-db "consulta demo/veil_demo.db para el email..."
#
# Set VEIL_APPEND_SYSTEM_PROMPT to pass --append-system-prompt through to
# claude (only meaningful with --demo-db) for extra reinforcement if needed.
set -euo pipefail

demo_db=0
if [ "${1:-}" = "--demo-db" ]; then
  demo_db=1
  shift
fi

if [ $# -eq 0 ]; then
  echo "usage: $(basename "$0") [--demo-db] <message>" >&2
  exit 1
fi

: "${VEIL_GATEWAY_URL:=http://127.0.0.1:8787}"

if [ "$demo_db" -eq 1 ]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  # PreToolUse/PostToolUse open the vault in their own subprocess, spawned
  # by Claude Code and inheriting *this shell's* env -- not the gateway
  # process's env from scripts/run-gateway.sh. Always (re)load from the key
  # file rather than only when unset: a stale VEIL_VAULT_KEY left over in
  # this shell from an earlier export (this terminal's history, not any
  # shell profile) silently wins over an unconditional guard, which is
  # exactly what happened live (2026-09-08) -- confirmed by hashing the key
  # the hook actually saw against the key file's current content and
  # finding they differed, in a shell where VEIL_VAULT_KEY was already set
  # before this script ever ran. Whatever the currently-running gateway
  # loaded at its own startup is the only key that can ever decrypt what it
  # mints, so the file (which run-gateway.sh both writes and loads from) is
  # the one source of truth here -- never trust an inherited value.
  KEY_FILE="$ROOT/.veil/vault.key"
  if [ ! -f "$KEY_FILE" ]; then
    echo "ask.sh --demo-db: $KEY_FILE not found -- start the gateway first" \
         "(scripts/run-gateway.sh), which generates it, then retry." >&2
    exit 1
  fi
  export VEIL_VAULT_KEY
  VEIL_VAULT_KEY="$(cat "$KEY_FILE")"
  # Read by demo/settings.hooks.isolated.json's own hook commands, which cd
  # into this before invoking pre_tool_use.py/post_tool_use.py -- see that
  # file's own comment for why (relative paths inside the hook scripts
  # resolve the shared vault against *this*, not the scratch dir below).
  export VEIL_REPO_ROOT="$ROOT"
  # The model's `python -c` queries run on Windows with a cp1252 stdout by default;
  # Claude Code reads Bash output as UTF-8, so accents came back as U+FFFD
  # ("Consultorï¿½a"). Force UTF-8 for every child process.
  export PYTHONUTF8=1
  SCRATCH="$HOME/.veil-cli-scratch/demo-db"
  mkdir -p "$SCRATCH/demo"
  cd "$SCRATCH"
  # A relative "demo/veil_demo.db" only resolves here if the file actually
  # exists at that relative path too -- hard link it in rather than telling
  # the model an absolute path in the message text: an absolute path here
  # would include this machine's real Windows username (part of $HOME), and
  # that text is exactly what the gateway tokenizes before forwarding, so
  # the LLM detection layer flagging it (username strings can look like a
  # person's name) would corrupt the very path the model needs to use --
  # observed live (2026-09-08): the path got partially tokenized and the
  # session looped trying to make sense of the mangled result. A hard link
  # needs no elevated privilege on NTFS (unlike a symlink) and keeps the
  # message itself free of any machine-specific or personal text.
  if [ ! -e "demo/veil_demo.db" ]; then
    ln "$ROOT/demo/veil_demo.db" "demo/veil_demo.db" 2>/dev/null \
      || cp "$ROOT/demo/veil_demo.db" "demo/veil_demo.db"
  fi
  # Reassures the model that seeing PII_<TYPE>_<NNNN> tokens instead of real
  # values is expected and correct, not something to flag or hedge about --
  # observed live (2026-09-08): without this, the model added disclaimers
  # ("I can't confirm this row is correct, the token I used doesn't match
  # the token in the result") that were themselves unreliable self-narration
  # (the vault's own mapping table showed the tokens were in fact
  # consistent), not a real defect to surface. VEIL_APPEND_SYSTEM_PROMPT, if
  # set, is appended after this baseline rather than replacing it.
  BASE_SYSTEM_PROMPT="Vas a ver identificadores con forma PII_<TIPO>_<NNNN> (por ejemplo PII_EMAIL_0001) en lugar de valores reales de email, nombre, DNI, dirección, etc. Esto es intencional: una capa de middleware externa a ti sustituye los valores reales por estos tokens antes de que lleguen a tu contexto, y otra capa distinta los revierte a sus valores reales cuando hace falta ejecutar una herramienta local (por ejemplo una consulta SQL) o mostrarlos en pantalla al usuario. Tú no tienes ni necesitas tener acceso a los valores reales, y no puedes verificar por ti mismo la correspondencia entre un token y el valor que representa -- eso ya está garantizado por el sistema. Trata estos tokens como si fueran los valores reales: úsalos con normalidad en tus consultas y respuestas, sin añadir advertencias, notas de fiabilidad ni dudas sobre si corresponden al dato correcto."
  if [ -n "${VEIL_APPEND_SYSTEM_PROMPT:-}" ]; then
    SYSTEM_PROMPT="$BASE_SYSTEM_PROMPT"$'\n\n'"$VEIL_APPEND_SYSTEM_PROMPT"
  else
    SYSTEM_PROMPT="$BASE_SYSTEM_PROMPT"
  fi
  ANTHROPIC_BASE_URL="$VEIL_GATEWAY_URL" claude -p --settings "$ROOT/demo/settings.hooks.isolated.json" \
    --append-system-prompt "$SYSTEM_PROMPT" "$*"
else
  SCRATCH="$HOME/.veil-cli-scratch"
  mkdir -p "$SCRATCH"
  cd "$SCRATCH"
  ANTHROPIC_BASE_URL="$VEIL_GATEWAY_URL" claude -p "$*"
fi
