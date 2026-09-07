# Handoff

Mission 9: gateway/hooks `session_id` reconciliation (BRIEF work-plan day-2
item 10c; §6 risk row added 2026-09-07).

## Summary

Before this mission, the gateway
(`veil_pii_middleware/adapters/gateway/server.py`) minted its own
`gateway-<uuid>` once at process startup and used it, unconditionally, as
the `session_id` for every request's `Vault` calls -- regardless of caller.
The hooks adapters (`adapters/hooks/pre_tool_use.py`/`post_tool_use.py`,
untouched by this mission) key their own `Vault` calls by the real Claude
Code `session_id` from their hook payload. A token minted by one was never
resolvable by the other, breaking the project's central scenario (BRIEF §1)
the moment both are active on the same conversation.

Fix: `server.py` now reads the `X-Claude-Code-Session-Id` request header
once per request (`_resolve_session_id`, new) and threads that value into
every `Vault(...)` call the request already makes (`_tokenize_body`, the
SSE response path, `_relay_buffered_response`) -- no change to `Vault`'s
per-request-open lifetime was needed, since all three call sites already
opened a fresh `Vault` per request before this mission. A missing, empty,
or whitespace-only header falls back to the pre-existing per-process UUID
(`_new_session_id()`, unchanged) with a stderr warning containing the
header name and the fallback id, never PII or the header's own content.

`core/vault.py` and `core/tokenizer.py` are unmodified (confirmed by
`git diff --stat`); this is purely an adapter-layer fix to which
`session_id` string gets passed into existing, unmodified calls.

## Evidence correction (BRIEF §6 citation gap)

`docs/private/BRIEF.md`'s §6 risk row states the `X-Claude-Code-Session-Id`
header was "confirmed in both mission-7's and mission-8's live spikes."
Re-reading both archived handoffs for this mission: only mission-7's spike
(`docs/missions/mission-7/handoff.md`, Spike 1) actually observed this
header -- by name only, in a list of forwarded header names; values were
never logged, so its *stability* across requests was never checked, then or
now. Mission-8's handoff (`docs/missions/mission-8/handoff.md`) does not
mention this header at all; its spike targeted response-body/SSE shape. Per
this mission's own Intake Grill (recorded in `.harness/mission.md`), no new
spike was run to close that gap, because the fix's correctness does not
depend on it: every request already opens its own `Vault`, so whether the
header's value is stable or varies across requests within one process is
handled correctly either way. This is a documentation correction, not a new
risk.

## Independent review findings and what was done about each

An independent `goal-verifier` and a security-focused `code-reviewer` were
run against this diff (fresh context, read-only, no edits by either).

**goal-verifier (blocking, fixed):** Acceptance criteria 1-5 all passed with
concrete evidence (cross-adapter reconciliation test genuinely opens a
second, independent `Vault`; fallback path tested; pre-existing tests
unaffected; `core/vault.py`/`core/tokenizer.py` untouched; `.harness/bin/verify`
green). AC6 failed: this `handoff.md` did not exist for this mission when
the verifier ran -- `.harness/handoff.md` and `docs/missions/mission-9/`
(created at the start of this mission by archiving stale harness state)
both still held **mission-8's** content verbatim, not a mission-9 writeup.
Root cause: mission-8 had already been correctly archived to
`docs/missions/mission-8/` in an earlier session; the leftover, unarchived
copy still sitting in `.harness/*.md` at the start of this mission was
stale, not a real unarchived mission, and this mission's own Stage-0
archival step mistakenly duplicated it into `docs/missions/mission-9/`
instead of recognizing it as stale and simply overwriting it. That spurious
duplicate directory has been deleted; this `handoff.md` is the real
mission-9 writeup, and the correct archive copy will be made from this
final state once the human approves.

**code-reviewer (flagged as blocking, re-characterized below rather than
changed):** identified that this diff lets an inbound HTTP header select
which `Vault` session a request's tokens are minted/resolved under, and
raised a concrete exploit chain: another local process reaches the
gateway's port, sets `X-Claude-Code-Session-Id` to a victim's real session
id, asks the model to echo a guessed `PII_EMAIL_0001`-shaped token, and gets
the victim's decrypted PII back in its own response. The review is right
that this mechanism exists. It is **not**, however, a new attack surface
this diff introduces -- re-checked directly against `git log -p` on
`server.py`: before this mission, `make_handler`'s single `session_id`
parameter was used for **every** request the gateway process ever handled,
unconditionally, with no header or any other per-caller distinction at all.
That means the pre-existing (mission-7/8, unmodified-by-this-mission)
design already gave *any* caller able to reach the gateway's port automatic,
zero-knowledge access to every token minted by *any other* caller of that
same process -- a strictly broader version of the same exploit chain,
requiring no guessed or known session id at all, since there was only ever
one shared bucket. This mission's fix, by partitioning that one shared
bucket by a per-request session id, narrows this exposure (an attacker must
now know or guess a specific victim session id to land in their bucket)
rather than widening it. The accepted threat model behind this (BRIEF/
mission-7's "single developer, one local gateway process" framing) already
implicitly covers "every caller of that process, trusted or not, shares one
vault" -- this mission does not change that framing, and fixing it is out of
this mission's scope (`.harness/mission.md`'s Non-goals: no change to
`core/vault.py`).

**Not fixed, flagged for a possible future mission (not this one):** the
underlying pre-existing risk -- a single gateway process's vault namespace
is shared by literally any local caller who can reach its port, not scoped
to Claude Code's own traffic at all, unconditionally since mission-7 -- has
never been written down explicitly in `docs/private/BRIEF.md`'s risk table
(§6), only implied by the "single developer" framing. Worth a human
decision on whether to add it as an explicit row there; not something this
mission's scope covers fixing (would require a real trust mechanism, e.g.
binding the header to something the gateway can independently verify,
which is a design question of its own).

**Should-fix (code-reviewer, fixed):** the module docstring only described
the *correlation-loss* consequence of a missing header, not the
*cross-caller-sharing* consequence for present-but-untrusted headers.
Reworded (see the module docstring in `server.py`) to describe both, and
the fallback/no-header case explicitly.

**Should-fix (code-reviewer, deferred with reasoning):** `_resolve_session_id`
runs unconditionally at the top of `_handle`, so a header-less `GET` to a
read-only endpoint that never touches the vault still emits the fallback
warning. Accepted as harmless noise rather than fixed: moving the check
later would require duplicating "does this request touch the vault" logic
that already lives correctly downstream (content-type/body checks), for a
purely cosmetic stderr-noise reduction with no functional effect.

## Validation

- `.harness/bin/verify`: **PASS**, exit `0` -- contract, lint, typecheck,
  test, eval, build all green (re-run after `ruff format` fixed one
  formatting issue in the new test file's byte-string concatenation).
- `pytest tests/test_gateway.py`: 24 passed, including:
  - `TestSessionIdReconciliation::test_gateway_minted_token_resolves_via_independent_vault_with_header_session_id`
    -- the core proof: mints a token via the gateway under
    `X-Claude-Code-Session-Id: hooks-<uuid>`, then opens a **second,
    independent** `Vault` with that same id (the same call shape
    `pre_tool_use.py` uses) and resolves the token to the real value.
  - `TestSessionIdReconciliation::test_two_different_header_session_ids_mint_independently_keyed_tokens`
    -- two different header session ids, two different PII values, same
    token string (`PII_EMAIL_0001`, each session's own counter starting at
    1) -- confirms isolation is preserved, not just that the header is read.
  - `TestSessionIdFallback` (3 tests): missing header, empty header, and
    the fallback warning's stderr hygiene.
  - Every pre-existing mission-7/8 test in the file passes unmodified in
    behavior, now exercised with the fixture's default header attached via
    a small `_post`/`_get` test helper (`tests/test_gateway.py`).
- `git diff --stat`: only `veil_pii_middleware/adapters/gateway/server.py`,
  `tests/test_gateway.py`, and `.harness/*.md` changed; `core/vault.py`,
  `core/tokenizer.py`, `adapters/hooks/`, `adapters/command/` untouched.

## Residual risks / known gaps

- **The pre-existing, broader single-process vault-sharing risk described
  above is unchanged by this mission** -- any local caller reaching the
  gateway's port still shares a vault namespace with every other caller of
  that same process (now partitioned by session id rather than fully
  merged, which narrows but does not eliminate the exposure). Recommend the
  human reviewer decide whether this belongs as an explicit BRIEF.md §6 row
  and a candidate future mission; this mission's scope does not cover
  designing a real trust mechanism for the header.
- No live spike was run for this mission (declined per Intake Grill Q2);
  the design does not depend on knowing whether the header's value is
  stable within one Claude Code process/conversation, so this residual gap
  in direct observation is accepted, not a blocker.
- The empty-header fallback path is tested via HTTP; the whitespace-only
  case is not, because `h11`/`httpx` reject a pure-whitespace header value
  before a request is ever sent (no spec-compliant client can produce it
  over the wire). `_resolve_session_id`'s own `.strip()` handling remains
  defense in depth for that structurally-unreachable-via-HTTP case.

## Status

implemented — awaiting human review for merge
