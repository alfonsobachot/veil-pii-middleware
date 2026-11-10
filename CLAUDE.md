# CLAUDE.md

## Project

PII sanitization middleware for Claude Code, built around a tokenization /
de-tokenization cycle. Raw personal data never leaves the machine: values are
replaced with stable `PII_<TYPE>_<NNNN>` tokens before any payload reaches an
external API, and the substitution is reversed for local tools so database
queries and file reads keep operating on the real values.

Users: developers who debug production incidents with Claude Code against
systems that hold customer PII.

Lifecycle stage: feature-complete demo/portfolio project. Every core component
(detector, vault, tokenizer, audit) and all three adapters (`/veil` command,
hooks, gateway) are implemented and tested, along with the demo database, the
eval harness and the gateway's observability panel. Remaining work is
incremental polish.

## Stack

Python, `pyproject.toml` with the Hatchling build backend, `pip` + `venv` for
dependencies, `ruff` for lint and formatting, `pytest` for tests. Runtime
dependencies: `cryptography` (vault AES-GCM) and `httpx` (gateway forwarding
client). SQLite backs the vault and demo database; the LLM detection layer
talks to any OpenAI-compatible chat-completions endpoint (LM Studio, Ollama,
...) via `VEIL_LLM_BASE_URL`/`VEIL_LLM_MODEL`, never a hardcoded host.

Runtime versions: `requires-python = ">=3.11"`.

Layout:

```
veil_pii_middleware/
  core/       detector, vault, tokenizer, audit
  adapters/   command (/veil), hooks, gateway
  frontend/   live observability panel
  demo/       seed (database + manifest), eval (recall / precision)
tests/        pytest suite
scripts/      run-gateway.sh, ask.sh
```

## Commands

| Task | Command |
|---|---|
| Install | `python -m venv .venv && .venv/Scripts/activate && pip install -e ".[dev]"` (POSIX: `source .venv/bin/activate`) |
| Seed demo DB | `.venv/Scripts/python -m veil_pii_middleware.demo.seed` |
| Run gateway | `scripts/run-gateway.sh [port]` (panel at `http://127.0.0.1:8787/veil-panel`) |
| Full demo turn | `scripts/ask.sh --demo-db "<message>"` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` |
| Typecheck | `.venv/Scripts/mypy` (strict; settings in `pyproject.toml`) |
| Test | `.venv/Scripts/pytest` (~4 min) |
| Eval | `.venv/Scripts/python -m veil_pii_middleware.demo.eval --mode regex\|llm\|union` |
| Build | `.venv/Scripts/python -m build --no-isolation` |

Run the shell scripts from Git Bash on Windows.

Local LLM used for the demo: Ollama on this machine,
`VEIL_LLM_BASE_URL=http://127.0.0.1:11434`,
`VEIL_LLM_MODEL=qwen3:4b-instruct-2507-q4_K_M`.

## Project Conventions

**Architecture.** One shared `core/` (detector, vault, tokenizer, audit) applied
by three adapters at three interception points (command, hooks, gateway).
Adapters never re-implement detection or substitution; they only wire `core/`
into a transport.

**Gateway scope.** The gateway tokenizes only the request's `messages` (what
the user typed plus tool results). `system`, `tools` and `metadata` are Claude
Code boilerplate and are forwarded untouched.

**Detection.** Hybrid, both layers unioned. The regex layer requires
check-digit validation — DNI/NIE letter, IBAN mod-97, card Luhn. The local LLM
layer returns spans as JSON, never rewritten text; substitution happens in
code. It covers `PERSON`, `ORG` and `ADDRESS`, which no regex can prove. On
overlapping spans the longest wins; ties go to regex.

**Tokens.** Format `PII_<TYPE>_<NNNN>`. The same normalized value must always
map to the same token within a session — otherwise the model cannot correlate
rows and gateway-mode prompt caching breaks.

**Vault.** AES-GCM ciphertext plus an HMAC-SHA256 lookup, never plaintext; the
master key comes from the environment (`scripts/run-gateway.sh` keeps one in
`.veil/vault.key`). The audit log is append-only and never records plaintext.

**Fail-closed.** When detection cannot run, block rather than forward. The
`UserPromptSubmit` hook can only block (exit 2), never rewrite.

**Data.** The demo database is fully synthetic (Faker, fixed seed). Any "PII"
seen during development is fake. The vault, generated databases and audit logs
are git-ignored.

**Testing.** `pytest` under `tests/`. Ruff enforces `E,F,I,UP,B,SIM` at line
length 100 with double quotes. `mypy` runs in `strict` mode over
`veil_pii_middleware` and `tests`. Detector changes are measured with
`demo/eval.py` (recall / precision) against the seeded ground-truth manifest.
