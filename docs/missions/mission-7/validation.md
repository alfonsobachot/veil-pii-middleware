# Validation Contract

Write this before implementation starts.

## Gate

The required gate is:

```bash
.harness/bin/verify
```

Exit codes:

- `0` = pass
- `1` = fail
- `2` = unconfigured

## Live Spikes (before any adapter code is written)

These are not automatable checks — they exercise the user's real Claude Code
account and a real external endpoint. Run and record evidence before Stage 6
implementation begins; a failure on spike 1 stops the mission (go/no-go).

| Spike | Method | Required result to proceed |
|---|---|---|
| 1. Auth | Start a throwaway local pass-through proxy (plain TCP/HTTP relay to `https://api.anthropic.com`, no rewriting). Spawn a **separate** process — `ANTHROPIC_BASE_URL=http://127.0.0.1:<port> claude -p "<short probe prompt>"` — and capture its stdout/exit code and the proxy's own log of what it saw. | The spawned process returns a real model response using the existing subscription login — no `ANTHROPIC_API_KEY` set, no auth-failure output. Record the exact evidence (proxy log line(s) showing the request/response passed through, and the spawned process's output) in `handoff.md`. If it fails, record exactly what error Claude Code surfaces and stop — do not build the gateway on a broken precondition. |
| 2. SSE (scoped out, not solved) | N/A — this mission does not attempt response rewriting. Confirm instead, from spike 1's own proxy log, whether the response Claude Code receives was in fact SSE (`text/event-stream`, chunked) or a single JSON blob, so `handoff.md` accurately states what the "relay untouched" non-goal is relaying. | Observation recorded either way; no pass/fail — informational only, feeds the future streaming-buffer mission. |

## Executable Checks

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore`, no implicit `Any` |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Build | `.venv/Scripts/python -m build --no-isolation` | exit `0` with `httpx` present in `pyproject.toml` dependencies |
| Detector recall/decoys (unchanged) | `python -m veil_pii_middleware.demo.eval --min-recall 1.0 --max-false-positives 0` | exit `0`, unmodified — this mission does not touch `detector.py` |
| Gateway tokenizes a PII value before it leaves the process | pytest: build a fake upstream (local `http.server` or `httpx` mock/`respx`-style handler standing in for `api.anthropic.com`) that records the raw bytes of every request it receives; send a request through the real gateway with a manifest email in the body; assert (a) the fake upstream's recorded body contains a `PII_EMAIL_NNNN` token and not the raw email, (b) it also does not contain the master key or any other plaintext secret | exit `0` |
| Response is relayed byte-for-byte unmodified | pytest: fake upstream returns a fixed body (including one that looks like a `PII_`-shaped token to make sure the gateway isn't scanning/rewriting responses at all); assert the client receives those exact bytes, untouched, for both a `Content-Length` response and a chunked/streamed one | exit `0` |
| Fail-closed: detector/vault/tokenizer failure blocks forwarding | pytest: force a `tokenize()`/`Vault` failure (e.g. `VEIL_VAULT_KEY` unset, or a monkeypatched raise) on a request containing PII; assert the fake upstream receives **no** request at all (not a request with unmodified raw PII, not a request with a partial rewrite) and the client gets an error response | exit `0` |
| Fail-closed: malformed/non-JSON request body | pytest: send a request whose body isn't valid JSON (or isn't the expected Anthropic Messages shape); assert the gateway does not forward it as opaque raw bytes with a shrug — either it fails closed (no forward) or it forwards byte-identical non-JSON content only if that's an explicit, documented decision (see Scrutiny Validator) | exit `0` |
| Headers forwarded, never logged | pytest/static: assert `x-api-key`/`authorization` header values are present unchanged in what reaches the fake upstream, and grep the gateway source for any logging/print statement that could include a header value | exit `0` |
| One `Vault` session per gateway process | pytest: two sequential requests through the same running gateway instance containing the same email produce the same token; a second, independently-started gateway instance (fresh process/session id) does not necessarily reuse it — assert same-process stability, not cross-process | exit `0` |
| `adapters/hooks/` and `adapters/command/` untouched | grep/diff: no changes outside `adapters/gateway/`, `pyproject.toml`, `tests/`, harness mission files, `README.md` | exit `0` |

## Scrutiny Validator

- Confirm the diff touches only `veil_pii_middleware/adapters/gateway/`,
  `pyproject.toml`, `tests/`, `README.md`, and the harness mission files —
  `detector.py`, `vault.py`, `tokenizer.py`, `audit.py`, `adapters/hooks/`,
  `adapters/command/`, and `frontend/` stay untouched.
- Confirm the gateway never re-implements detection/substitution logic
  inline — every tokenization call goes through
  `core/tokenizer.py::tokenize()` against a real `core/vault.py::Vault`.
- Confirm the response path contains no tokenization/detokenization call at
  all — this mission's whole scope reduction depends on the response leg
  being a pure relay, not a partial or best-effort rewrite that could split
  a token across a chunk boundary and corrupt it.
- Confirm auth headers (`x-api-key`, `authorization`, cookies if any) are
  forwarded unmodified and never appear in a log/print/exception message.
- Confirm the fail-closed paths (vault error, detector error, malformed
  body) never forward a request upstream with raw PII still in it — a
  network error contacting the real API on the *client's* side is
  acceptable; a request reaching `api.anthropic.com` with unrewritten PII is
  not, under any failure mode.
- Confirm the one-session-per-process design (fixed `session_id` generated
  at gateway startup) is documented as a scope limitation in code comments
  and in `handoff.md`, not silently assumed.
- Confirm the throwaway auth-spike proxy script (if kept) is clearly
  separated from and not imported by the real gateway adapter — it existed
  to answer a question, not to become part of the shipped code path.
- Confirm no secret (master key, `x-api-key` value, any token from the live
  spike) was written into `.harness/handoff.md`, `.harness/memory/`, or any
  committed file.
- Confirm `httpx` is pinned to one major line in `pyproject.toml`, matching
  how `cryptography` was pinned, with a comment explaining why.

## User-Testing Validator

- **Live auth spike (must be run and recorded, not skipped):** see the Live
  Spikes table above. `handoff.md` must state plainly, in one place, whether
  `ANTHROPIC_BASE_URL` worked with the subscription login with no API key —
  this is the single fact a human needs to decide whether the gateway
  approach is viable at all.
- Manually point a real, interactive `claude` session at the finished
  gateway (`ANTHROPIC_BASE_URL` set for that one invocation only, never the
  user's persistent shell profile) and send a prompt containing a real
  manifest PII value; confirm by eye by inspecting the gateway's own log
  and, if feasible, a packet/proxy capture between the gateway and the real
  API, that no raw PII value crosses that second hop. Record whether this
  step was actually performed or not — do not claim it happened if it did
  not.
