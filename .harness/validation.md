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

## Live Spike

None. Declined by the Intake Grill (Q2): the fix threads a per-request
header value into `Vault(...)` calls that `server.py` already opens fresh
per request, so correctness does not depend on whether the header's value
is stable or varies across requests within one process. No throwaway spike
gates this mission's design, unlike missions 7 and 8.

## Executable Checks

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore`, no implicit `Any` |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Cross-adapter reconciliation (AC1) | pytest: send a request through the gateway carrying `X-Claude-Code-Session-Id: <hook-shaped test sid>` containing PII; open a second `Vault(VAULT_DB_PATH, "<same sid>")` directly (the same call shape `pre_tool_use.py` uses) and call `.resolve()` on the token the gateway's forwarded body shows; assert it returns the original value | exit `0` |
| Header-missing fallback (AC2) | pytest: send a request with no `X-Claude-Code-Session-Id` header; assert the request still succeeds (200) and a token is still minted (same behavior as before this mission) | exit `0` |
| Header empty falls back the same way as missing | pytest: send the header as `""`; assert it behaves identically to the missing-header case, not as a distinct error path. (A pure-whitespace value was *not* separately tested this way: `h11`/`httpx` reject leading/trailing OWS in a header value before the request is even sent, so no spec-compliant client can produce it over the wire — `_resolve_session_id`'s `.strip()` check remains defense in depth for that unreachable-via-HTTP case, documented rather than tested via a fake transport.) | exit `0` |
| Fallback warning contains no PII/secret | pytest, extending the existing `test_no_secret_or_pii_appears_in_gateway_stderr` pattern: trigger the header-missing fallback path with a request containing a real-looking email; assert stderr contains neither the email nor the vault master key | exit `0` |
| Every pre-existing `tests/test_gateway.py` test still passes with the fixture's new default header (AC3) | `.venv/Scripts/pytest tests/test_gateway.py` | exit `0`, no pre-existing assertion needed to change because of this mission |
| `core/vault.py`/`core/tokenizer.py` untouched (AC4) | `git diff --stat` reviewed by hand | no lines changed in either file |
| Two different header session ids on two requests to the same process mint independently-keyed tokens | pytest: send two requests with two different `X-Claude-Code-Session-Id` values, each containing the same PII value; assert each mints its own `PII_EMAIL_0001` under its own session id in the vault (not sharing state), matching `core/vault.py`'s existing per-session isolation | exit `0` |

## Scrutiny Validator

- Confirm the diff touches only `veil_pii_middleware/adapters/gateway/server.py`,
  `tests/test_gateway.py`, `.harness/*`, and `docs/private/BRIEF.md` if
  updated — `core/vault.py`, `core/tokenizer.py`, `adapters/hooks/`, and
  `adapters/command/` stay untouched (`git diff --stat`).
- Confirm the header is read once per request in `_handle()` and threaded
  into all three existing `Vault(...)` call sites for that request
  (`_tokenize_body`, the SSE response path, `_relay_buffered_response`) —
  not re-derived independently in each, which could let one request use two
  different session ids for its own request and response legs.
- Confirm no `Vault` instance is held open across requests or given
  process-lifetime scope — each request's `Vault(...)` calls must still
  open fresh, keyed by that request's own resolved session id (header or
  fallback), matching the mission's stated reason for not needing a live
  spike.
- Confirm the fallback path is reached only for a missing, empty, or
  whitespace-only header — not for any other validation failure invented
  beyond that (no new format/schema check).
- Confirm the fallback warning logs the session id it's falling back to
  (for operator debuggability) but never a header value that could carry
  attacker- or client-supplied content beyond that same id, and never the
  vault master key.
- Confirm `make_handler`'s `session_id` parameter (or its renamed
  equivalent) is documented as the *fallback* default, not "the" session id,
  and that `main()`'s startup log line no longer implies one fixed session
  governs every request.
- Confirm the new cross-adapter reconciliation test actually opens a
  second, independent `Vault` instance against the same `VAULT_DB_PATH` —
  not reusing the gateway's own in-process `Vault` object — since reusing it
  would not prove anything about keying by session id.

## User-Testing Validator

- No live spike or manual end-to-end check against the real Anthropic API is
  required for this mission (declined per the Live Spike section above) —
  the automated reconciliation test is the proof, since it exercises the
  same `Vault`-keying code path a real hook invocation would.
- If the human reviewer wants an extra manual check anyway: run the real
  gateway, send one request by hand (e.g. `curl` or `httpx`) with a
  `X-Claude-Code-Session-Id` header matching a real Claude Code session id
  captured from a hook payload, and confirm the same id appears as a
  `session_id` value in `.veil/vault.db`'s rows for that request — optional,
  not required to close this mission.
