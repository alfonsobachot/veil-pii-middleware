# Handoff

Mission 7: gateway adapter at `ANTHROPIC_BASE_URL` (BRIEF work-plan day-2,
items 9-10).

## Summary

Implemented `veil_pii_middleware/adapters/gateway/server.py`: a reverse
HTTP proxy that stands in for `https://api.anthropic.com`. For every
request it reads the body; if it's JSON, tokenizes every string leaf via
`core/tokenizer.py::tokenize()` against a per-process `core/vault.py::Vault`
before forwarding to the real API with the original method/path/headers
(including `Authorization`); the response is relayed back to the client
byte-for-byte untouched, streaming or not — no response-side rewriting is
attempted (deliberate scope cut, see Non-goals below). Fails closed on a
non-JSON body or any tokenize/vault error: nothing is forwarded upstream in
that case.

Added `httpx~=0.28.0` as a new runtime dependency (the project's first
network-facing code; previously only `cryptography` for the vault).

`core/detector.py`, `core/vault.py`, `core/tokenizer.py`,
`adapters/hooks/`, `adapters/command/` are untouched — confirmed via
`git diff --stat` against those paths (empty).

## Spike 1 — auth (go/no-go gate, run before any adapter code)

**Result: GO.** `ANTHROPIC_BASE_URL` works with the existing subscription
login; no `ANTHROPIC_API_KEY` is required.

Method: a throwaway pass-through proxy (`http.client.HTTPSConnection`
forwarding raw bytes to `api.anthropic.com`, no rewriting) listening on
`127.0.0.1:8787`, not committed. A **separate** process —
`ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude -p "Reply with exactly the
single word: OK"` — was spawned from this session (never touching this
session's own connection) and returned `OK`, exit `0`.

Evidence, from the throwaway proxy's own stderr log (header *names* only —
by design, values are never logged, so no secret appears here even in a
throwaway spike):

```
[proxy] listening on http://127.0.0.1:8787 -> https://api.anthropic.com
[proxy] -> POST /v1/messages?beta=true headers=['Accept', 'Accept-Encoding',
  'Authorization', 'Connection', 'Content-Length', 'Content-Type', 'Host',
  'User-Agent', 'X-Claude-Code-Session-Id', 'X-Stainless-Arch', ...,
  'anthropic-beta', 'anthropic-dangerous-direct-browser-access',
  'anthropic-version', 'x-app'] body_len=4132
[proxy] <- status=200 resp_headers=[...] body_len=647
[proxy] -> POST /v1/messages?beta=true [...] body_len=129132
[proxy] <- status=200 resp_headers=[...] body_len=671
```

Two requests, both `200` (Claude Code appears to make a second call —
consistent with a background title-generation request; not investigated
further, out of scope). Key findings:

- Auth travels as an **`Authorization`** header (bearer token from the
  subscription's OAuth flow), not `x-api-key`. The real gateway forwards
  whatever auth header is present unchanged, so this holds regardless.
- Endpoint is `POST /v1/messages?beta=true` — path *and* query string
  matter; the gateway must forward both untouched (it does: `self.path`
  includes the query string in `http.server.BaseHTTPRequestHandler`).
- Response headers include `Transfer-Encoding` (chunked) — confirms
  Spike 2's premise below: yes, the real response is delivered
  chunked/streamed, not a single buffered blob.

The throwaway proxy process was killed after this check
(`taskkill //PID <pid> //F`); nothing from it was committed.

## Spike 2 — SSE streaming (informational, not solved)

Per the mission's agreed scope cut, this mission does not attempt
chunk-boundary-safe response rewriting at all — see Non-goals. Spike 1's own
log (above) confirms the premise this scope cut is judged against: the real
API's response to Claude Code is in fact chunked/streamed
(`Transfer-Encoding` present), not a single JSON blob, so a naive
buffer-then-scan-then-forward approach on the response leg would have had
real chunk-boundary risk to deal with. Deferred to an optional follow-up
mission, not attempted here.

## Post-implementation live check (real gateway, real Claude Code, real PII)

After the gateway was built and the automated test suite passed, a live
check confirmed detection/tokenization actually fires on real conversation
content flowing through the real code path — not just in the isolated unit
tests against a fake upstream.

Method: started the real gateway (`python -m
veil_pii_middleware.adapters.gateway.server 8788`) with a fresh,
process-local `VEIL_VAULT_KEY`. Spawned a separate `claude -p` process with
`ANTHROPIC_BASE_URL=http://127.0.0.1:8788` and a prompt containing a real
email address (`developer@example.com`); it replied normally
(exit `0`).

Verification: inspected `.veil/vault.db` directly (read-only, local,
never transmitted) and found two new rows under that run's session id
(`gateway-b914bcbb-...`), both entity type `EMAIL`. Decrypting them with the
same process-local key (still exported in that terminal at the time)
confirmed the two plaintext values were `developer@example.com` (the
prompt's own PII) and `noreply@anthropic.com` (present somewhere in Claude
Code's own system prompt/metadata, not something this mission put there).
This confirms the detector fired on real content and the vault minted real
tokens for it — the same code path (`_tokenize_body` in `server.py`) that
the automated tests already prove never lets a raw value reach
`client.stream(...)`.

**Known limitation of this specific evidence, flagged by independent
review:** the process-local `VEIL_VAULT_KEY` used for that run was
generated fresh for the check and was not persisted afterward — correct
per `MASTER_KEY_ENV_VAR`'s own doctrine (never from the repository, treated
as ephemeral), but it also means those two specific ciphertext rows cannot
be re-decrypted later by anyone, including a reviewer, to re-confirm this
exact claim after the fact. The *mechanism* is not resting on that one
irreproducible artifact, though: `tests/test_gateway.py`'s
`TestRequestTokenization`/`TestFailsClosed` cases prove the same code path
structurally, with no secret involved, and can be re-run by anyone at any
time (`pytest tests/test_gateway.py -v`). A human wanting to re-confirm the
live-content claim specifically would need to re-run this exact live check
themselves (5 minutes: start the gateway, spawn `claude -p` with
`ANTHROPIC_BASE_URL` set, inspect `.veil/vault.db` with the key still in
scope) rather than trust this write-up alone — noted here explicitly so
that gap is visible, not silently assumed away.

## Independent review findings and what was done about each

An independent `goal-verifier` and a security-focused `code-reviewer` were
run against this diff (fresh context, read-only, no edits by either).

**Fixed:**

- **Blocking (code-reviewer):** a mid-stream upstream failure (e.g. the
  real API dropping the connection after the gateway had already relayed a
  status line and some response chunks to its client) triggered a *second*
  `send_response(502, ...)` on the same connection, splicing a fresh status
  line into an already-in-flight chunked body and corrupting the framing.
  Fixed in `server.py`'s `_handle`: a `response_started` flag now gates
  whether a failure may still call `_write_error` (only before any bytes of
  the response have gone out); past that point the handler just stops and
  lets the connection close. Covered by two new regression tests in
  `tests/test_gateway.py::TestMidStreamUpstreamFailure`, one asserting the
  literal raw wire bytes never contain a second `HTTP/1.1` status line, one
  asserting the gateway keeps serving subsequent requests afterward.
- **Should-fix (code-reviewer):** `core/vault.py::Vault._next_seq` is a
  read-then-insert with no transaction spanning both statements; under
  `ThreadingHTTPServer`'s real one-thread-per-request concurrency (the
  first adapter in this codebase to actually exercise concurrent access to
  one session), two simultaneous requests minting a token for the same
  entity type could race and collide. This already failed closed (caught
  by the generic exception handler, 500, no data leak) but was an
  availability bug, not just a theoretical one. Fixed **without touching
  `core/vault.py`** (out of this mission's scope): added a
  process-local `threading.Lock` in `server.py` serializing
  `_tokenize_body`'s vault access, eliminating the race at the one place
  it can actually occur (this gateway process) instead of changing shared
  core code.
- **goal-verifier:** this `handoff.md` did not exist for this mission
  before this review pass — the mission's own acceptance criteria (AC6)
  and merge policy require it. Written now, with the actual spike evidence
  above rather than a restated claim.

**Deferred (documented, not fixed, with reasoning):**

- **Should-fix (code-reviewer):** exceptions outside the two explicit
  `try`/`except` blocks in the original code (e.g. a malformed
  non-numeric `Content-Length`, or a `BrokenPipeError` from a client
  disconnecting mid-write) could fall through to `socketserver`'s default
  `handle_error`, dumping a traceback to stderr via an unaudited path. The
  blocking-bug fix's restructure incidentally widened the outer exception
  handling to cover this (the whole request-handling body up to and
  including the response-relay loop is now inside one `try`/`except
  Exception`), but `_write_error` itself is not separately guarded against
  raising on an already-broken socket. No exception message in this file
  ever interpolates header/body content today, so no concrete leak was
  found — tracked as a residual risk, not a re-opened blocker.
- **Should-fix (code-reviewer):** the gateway's own listening socket has no
  authentication — any local process reaching `127.0.0.1:<port>` can mint
  tokens into the one shared per-process vault, and since
  `tokenize()`/`TokenFormatOverflowError` correctly refuses a 5th-digit
  token, ~10,000 distinct values of one entity type in a session
  permanently fails every later request containing that entity type (fails
  closed, but a real availability cliff with no documented mitigation).
  Accepted as a known limitation for this mission's target user (a single
  developer running one local gateway process for their own Claude Code
  session) — not fixed here.
- **Nit (code-reviewer):** `_HOP_BY_HOP` omits `TE`/`Trailer`/
  `Proxy-Authenticate`/`Proxy-Authorization`. Claude Code never sends these
  to this proxy in practice; left as-is, noted for a future reviewer.
- **goal-verifier:** flagged that `TestResponseIsRelayedUntouched` only
  exercises a `Content-Length`-framed fake-upstream response, not one the
  fake upstream itself sends as real chunked/streamed framing (only the
  gateway's own re-chunking *to the client* is exercised, via the
  large-body test). The code path (`httpx`'s `.stream()`/`iter_bytes()`) is
  framing-agnostic on the upstream side, so risk is judged low, but this is
  a real gap against the validation table's literal wording and is not
  closed here.

## Non-goals kept out of scope (unchanged from the mission)

- No response-side tokenize/detokenize call exists anywhere — confirmed by
  the independent reviewer via `grep` on `server.py`: every `tokenize`/
  `Vault(` reference lives in the request path (`_tokenize_body` and its
  one call site); the response-relay block contains none. A token the
  model echoes back (e.g. `PII_EMAIL_0001`) appears literally on screen.
- `adapters/hooks/` vs. gateway coverage overlap/redundancy: still an open
  question, not decided here.
- Multi-process/multi-window vault correlation in gateway mode: still one
  session per running process, documented in `server.py`'s module
  docstring.

## Human verification still needed before merge

1. Read this handoff and the diff (`git diff` — touches only
   `veil_pii_middleware/adapters/gateway/`, `pyproject.toml`,
   `tests/test_gateway.py`, `.harness/*`).
2. `.harness/bin/verify` — currently green (contract, lint, typecheck, test,
   eval, build — 6/6). Re-run it yourself.
3. If you want to independently re-confirm the live-content claim above
   (recommended, since that specific evidence is not re-decryptable after
   the fact): `VEIL_VAULT_KEY=$(python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())") .venv/Scripts/python -m veil_pii_middleware.adapters.gateway.server 8788` in one terminal, then in another
   `ANTHROPIC_BASE_URL=http://127.0.0.1:8788 claude -p "test email
   someone@example.com, just reply OK"`, then inspect `.veil/vault.db` with
   the same key while it's still in scope.
4. No commit/push/merge has been made by this session, per policy.
