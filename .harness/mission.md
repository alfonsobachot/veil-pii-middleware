# Mission

## Objective

Implement the gateway adapter (`veil_pii_middleware/adapters/gateway/`, BRIEF
work-plan day-2 items 9-10, `docs/private/BRIEF.md` §2.1 row "Whole payload |
gateway at `ANTHROPIC_BASE_URL` | Yes, full coverage"). This is the only
mechanism that closes the "user prompt -> model" leg — confirmed in mission-5
that the (now-closed) `/veil` slash command cannot, because Claude Code
renders the raw typed argument into the transcript independently of the
script's stdout. It is also the only interception point that requires no
per-session activation: any Claude Code surface (CLI, VS Code extension,
desktop app) that honors `ANTHROPIC_BASE_URL` gets covered transparently,
unlike hooks (mission-6, closed) which need a settings fragment loaded.

Two of the BRIEF's own "known technical risks" (§6) are unresolved and must
be spiked live, against the user's real Claude Code session, before any
proxy logic is written:

1. **Auth.** `ANTHROPIC_BASE_URL` may require a console API key rather than
   the subscription login already configured on this machine. If it does,
   the rest of this mission has no viable target and the finding is a
   go/no-go gate, not a detail.
2. **SSE streaming.** A token like `PII_EMAIL_0001` can arrive split across
   two response deltas. **Scope reduction agreed with the user this
   session:** rather than build a chunk-boundary-safe buffer now, this
   mission tokenizes only the outbound request leg (the one the
   architectural requirement — "zero raw PII to an external API" — actually
   depends on) and relays the response byte-for-byte untouched, streaming or
   not. This sidesteps the split-token risk entirely for v1 instead of
   solving it. A follow-up mission may add response-side rewriting with a
   real streaming buffer; it is optional and not scheduled.

Reuses `core/detector.py`, `core/vault.py`, `core/tokenizer.py` unmodified,
same as the `/veil` command and the hooks adapters.

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-external-write, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

Note: `before-external-write` here covers the live auth spike specifically
(spike 1 sends real traffic through the user's authenticated Claude Code
session against a live external endpoint) — approval for the mission as a
whole does not by itself authorize running that spike; see Validation
Contract's Live Spikes table.

## Intake Grill

| Question | Answer / Evidence |
|---|---|
| Spike 1 (auth) sends live traffic through a throwaway proxy using the user's real Claude Code subscription session. How should it be run safely, without disrupting the live conversation making this very request? | Spawn a **separate** process: `claude -p "<probe prompt>"` (non-interactive, print mode) with `ANTHROPIC_BASE_URL` pointed at a throwaway local pass-through proxy, run from a Bash tool call. This session's own connection to Anthropic is never touched — only the spawned process's environment is redirected. Confirmed available: `claude --version` -> `2.1.263 (Claude Code)` on PATH. Uses a small amount of the user's real usage/quota; user approved this explicitly. |
| No HTTP client/server library is installed (`pyproject.toml` has zero runtime deps besides `cryptography`). What should the gateway's HTTP stack be? | `httpx` as the forwarding client (mature streaming/SSE support) + stdlib `http.server` as the listening side. One focused new dependency, same pattern as how `cryptography` was added for the vault (pinned major line, reviewed). Confirmed not installed: `import httpx`/`aiohttp`/`starlette`/`uvicorn` all fail in `.venv`. |
| Should this mission build both the auth spike and a working (non-streaming-aware) gateway, or stop after the spike? | Both, gated on the spike succeeding — build a minimal gateway that tokenizes the outbound request body and relays the response untouched. If the auth spike fails, stop and report; no gateway code gets built on a broken precondition. User explicitly asked to keep this mission simple by deferring response-side SSE rewriting to an optional follow-up, rather than attempting it now. |
| Gateway mode has no `session_id` field the way hook JSON payloads do — the gateway is a standalone long-running process, not a per-invocation script. What keys the per-session `Vault`? | One `Vault` session per running gateway process instance: a fixed `session_id` generated once at process startup (e.g. a UUID), shared by every request the process handles for its lifetime. Matches "one mapping set per session" if "session" = "one running gateway instance" — correct for the target user (single developer, single gateway process). Documented as a known limitation: two concurrent gateway processes, or a restart mid-conversation, do not share a token vocabulary. Multi-window session correlation is out of scope. |
| Where does the vault live, and what env var carries the master key? | Same as `/veil` and the hooks: `.veil/vault.db` (relative to cwd), `VEIL_VAULT_KEY` unchanged. No new env var introduced for the vault itself; `ANTHROPIC_BASE_URL` (existing Claude Code / Anthropic SDK convention) is the only new env var this mission relies on, and it is not read by the gateway code — it is set by whoever launches Claude Code, pointing it at the gateway's listening address. |

Evidence gathered before asking: `veil_pii_middleware/adapters/gateway/__init__.py`
is currently a one-line docstring stub — no prior code to preserve or
conflict with. `core/tokenizer.py`/`core/vault.py` are unchanged and already
tested (`tests/test_tokenizer.py`, `tests/test_vault.py`). `docs/private/BRIEF.md`
§2.1 and §6 are the design of record for this mission; §6's auth and SSE rows
are the two spikes above. `claude --version` confirms a real `claude` CLI is
on PATH for the auth spike's separate-process approach.

## Scope

- `veil_pii_middleware/adapters/gateway/` — a reverse-HTTP-proxy adapter:
  listens locally, tokenizes every outbound request body's string content
  via `core/tokenizer.py::tokenize()` against a per-process `Vault`, forwards
  the tokenized request (headers, including auth, untouched) to the real
  `https://api.anthropic.com`, and relays the response back to the client
  unmodified — streaming (SSE) or not.
- A throwaway (not committed, or committed only as a clearly-labeled spike
  script under the gateway package if it has lasting value) auth-spike proxy
  used to answer question 1 live, before the real adapter is written.
- `pyproject.toml`: add `httpx` as a pinned-major runtime dependency.
- Tests: gateway request-body tokenization, pass-through of non-JSON/non-text
  content, and fail-closed behavior when the vault/detector can't run —
  mirroring `tests/test_hooks.py`'s subprocess-free, direct-call style where
  practical, or an in-process `httpx`/ASGI test client if that fits `httpx`'s
  own testing story better.
- `.harness/handoff.md` documents the auth spike's actual finding (go/no-go)
  and, if "go," the SSE-rewrite scope cut as a residual/future item.

## Non-goals

- Response-side (model -> client) rewriting or any SSE chunk-boundary-safe
  token buffer. The response is relayed untouched in this mission; a token
  the model echoes back (e.g. `PII_EMAIL_0001`) appears literally on screen
  rather than being resolved to the real value. That gap is accepted here,
  same class of gap as BRIEF item 14 (`MessageDisplay` hook / `/reveal`,
  not built yet) already anticipates.
- Local LLM detector layer, `eval.py` metrics, audit log, frontend (BRIEF
  items 11-13) — untouched, separate missions.
- Deciding whether `adapters/hooks/` (mission-6) stays as defense-in-depth
  for the Bash leg or becomes redundant once the gateway exists — explicitly
  deferred to a future review once the gateway's real coverage is known.
- Multi-session / multi-window vault correlation in gateway mode.
- Modifying `core/detector.py`, `core/vault.py`, `core/tokenizer.py`.
- Changing the user's persistent shell profile / global `ANTHROPIC_BASE_URL`
  — this mission's own spike and any manual verification set the env var
  only for the lifetime of one spawned command, never persisted.

## Acceptance criteria

1. Live-verified (not assumed): a separate `claude -p` process, with
   `ANTHROPIC_BASE_URL` pointed at a throwaway pass-through proxy, either
   successfully authenticates and gets a real response using the existing
   subscription login (go), or fails in a way that identifies exactly what
   auth mechanism/header is missing (no-go, with evidence).
2. If go: a request through the real gateway containing a manifest PII value
   (e.g. an email) in the body results in a `PII_<TYPE>_<NNNN>` token — not
   the raw value — in what actually reaches `api.anthropic.com` (verified by
   inspecting the outbound request the gateway forwards, not just the
   tokenizer's unit-tested behavior in isolation).
3. The real API's response reaches the original client unmodified, streaming
   included — no truncation, no corruption, no dropped chunks.
4. Detection/vault/tokenizer failure inside the gateway fails closed: the
   request is not forwarded with raw PII in it.
5. `.harness/bin/verify` exits 0.
6. `.harness/handoff.md` states the auth-spike result plainly enough that a
   human can decide, without re-running anything, whether the gateway
   approach is viable at all.

## Risk

Risk class: high

Rationale: touches live authentication against the user's real Claude Code
account (spike 1), introduces a new runtime dependency, and adds a
network-facing component that sits between Claude Code and Anthropic (a bug
here can break the user's actual ability to use Claude Code if they later
point real traffic at it — though this mission itself never does that; only
a separate spawned process during the spike does). Detection/vault/
tokenization code itself is not modified, only invoked, which keeps the
highest-risk surface (per CLAUDE.md's "Review and risk" convention)
untouched — but the new network surface plus live-auth spike still warrants
`high`, not `medium`.

## Merge policy

No commit, push, or merge without explicit human review of the diff and the
auth-spike evidence in `.harness/handoff.md`. This mission does not merge
itself.

## Tooling And Approach Assessment

1. **What project type is this?** A local reverse HTTP proxy (network
   server + forwarding client) added to an existing Python package that so
   far has had zero network-facing code — a materially different risk
   surface than the vault/detector/tokenizer/CLI-script work done so far.
2. **What methods, tooling, or approaches does that imply?** An HTTP client
   library with real streaming support (`httpx`, chosen over stdlib
   `http.client`/`urllib` for SSE ergonomics and over `aiohttp` to keep one
   new dependency instead of two paradigms); a minimal listening server
   (stdlib `http.server.ThreadingHTTPServer`, sufficient for a single-
   developer local proxy — no need for an ASGI stack); a live spike against
   a real external endpoint and a real Claude Code binary before writing the
   adapter, because the two open risks are both about behavior of systems
   this repo does not control (Anthropic's auth flow, Claude Code's HTTP
   client) and cannot be resolved by reading this repo's own code.
3. **What risks or missing decisions does it reveal?** (a) TLS: the gateway
   must terminate TLS from the client side (Claude Code will speak HTTPS to
   `ANTHROPIC_BASE_URL` unless documented otherwise — to be confirmed in the
   spike) and originate a fresh TLS connection to the real API — a local
   self-signed cert or plain-HTTP-for-localhost approach needs to be
   confirmed as acceptable to Claude Code's client during the spike. (b)
   Header handling: `x-api-key`/OAuth bearer headers must be forwarded
   byte-for-byte, never logged. (c) What exactly the gateway does if the
   request body isn't the expected Anthropic Messages JSON shape (fails
   closed vs. passes through) needs an explicit answer before it's written,
   not discovered by a crash in front of a live model call.
4. **What should be added to validation?** A recorded transcript/log of the
   live auth spike's actual request/response (headers redacted of any
   secret, never of PII since none should be present) as evidence, not just
   a "it worked" claim; a test asserting a PII value never appears in the
   bytes the gateway sends upstream; a test that malformed/non-JSON request
   bodies still fail closed rather than forwarding raw bytes with a shrug.
5. **What is explicitly not needed now?** Response rewriting/streaming
   buffer (deferred, see Non-goals), the local LLM detector layer, TLS
   certificate pinning or a full mitmproxy-style CA setup (a spike-grade
   throwaway is enough to answer the auth question), and any change to how
   the hooks adapter (mission-6) is configured or loaded.

## Status

implemented — awaiting human review for merge (see `.harness/handoff.md`)
