# Mission

## Objective

Implement the deterministic regex detection layer in `core/detector.py` — email,
DNI/NIE, IBAN, Spanish phones, credit card and IP, all with mandatory
check-digit validation — returning `Span` objects and never rewritten text.
Together with it, generate the synthetic demo database and its ground-truth
manifest, plus a minimal `eval.py`, so detector recall is measured against
ground truth from the first commit instead of eyeballed.

## Routing

Execution lane: agent-with-checkpoint
Human review trigger: before-code, before-merge
Delegation mechanism: main-session
Validation plan: .harness/validation.md

Defaults above are the conservative path (today's loop). Lane rules:
`.harness/docs/operating-model.md` `## Execution Lanes`. The gate's `contract`
row checks these rows through `.harness/adapter.yaml`.

## Intake Grill

| Question | Answer / Evidence |
|---|---|
| What shape is the `Span` model, the public API for tokenizer, vault and audit? | Frozen dataclass: `@dataclass(frozen=True, slots=True)` with `start`, `end`, `entity_type` (StrEnum), `text`, `detector`. Immutable and hashable, so it fits the content-block-hash cache; carries the fields the audit log requires (BRIEF 3.4); best fit for mypy strict. |
| How is the layer measured, given `demo/seed.py` and the manifest do not exist yet? | Expand scope: build the synthetic database and ground-truth manifest in this mission, so recall/precision are real rather than hand-written fixtures. |
| How are overlapping spans resolved inside the regex layer itself? | Resolve now via a reusable `merge_spans()`: longest span wins, ties go to regex. It is the same function the LLM union stage will need, so it is written and tested once. |
| Risk class and execution lane? | High risk, `agent-with-checkpoint`. CLAUDE.md names detection a high-risk surface; high risk is never `agent-owned`. |
| What volume does the seed generate? | Reduced and deterministic: ~20 customers, 50 users, 100 invoices, 30 tickets, 200 log lines. Same 7-table schema and same deliberate PII scattering as the BRIEF, but generating in under a second so the gate stays fast. Volume rises when the LLM layer needs real load. |
| Does `eval.py` land in this mission too? | Yes, minimal and regex-only: recall and precision per entity type, with the `detector` dimension already present for the future LLM layer. Without it the seed measures nothing. |
| Where does the new Faker dependency go? | Inside the existing `dev` extras. Simpler, and keeps a single `pip install -e ".[dev]"`; the runtime package still declares no dependencies. |
| Is eval wired as a gate in `.harness/bin/verify`? | Yes, with a 100% recall threshold on structured entities. The BRIEF already sets that expectation, and it makes "the detector works" binary. |
| Which entities, and which check digits? | Repo evidence — `CLAUDE.md` `## Project Conventions` and BRIEF 3.1: email, Spanish DNI and NIE, IBAN, Spanish phones (6xx/7xx/9xx), credit card, IP. Mandatory validation: DNI/NIE letter, IBAN mod-97, card Luhn. |
| Does substitution or token generation belong here? | Repo evidence — BRIEF 3.1 states the detector returns spans and substitution happens in code elsewhere; the user scoped substitution out when opening the mission. |
| Expected recall target per entity type? | Repo evidence — BRIEF 4.1: 100% on emails, IBANs and national IDs; partial on names, and names are LLM-layer work, out of scope here. |
| Is the generated database committed? | Repo evidence — `.gitignore` ignores `*.db`, `*.sqlite`, `*.sqlite3`. The gate must therefore seed before it evaluates. |

## Scope

- `veil_pii_middleware/core/detector.py` — `EntityType`, `Span`, the regex
  patterns, the check-digit validators, `merge_spans()` and the regex entry
  point.
- `veil_pii_middleware/demo/seed.py` — synthetic SQLite database plus
  `demo/manifest.json`.
- `veil_pii_middleware/demo/eval.py` — recall and precision per entity type and
  per detector, with a threshold flag.
- `tests/` — new test modules for the detector, the check digits and
  `merge_spans()`.
- `pyproject.toml` — add Faker to the `dev` extras.
- `.harness/bin/verify` — one new `eval` gate row.
- `.harness/validation.md`, `.harness/handoff.md`.
- `.claude/hooks/grill-guard.sh` and `.claude/hooks/mission-state-guard.sh` —
  added at the pre-code checkpoint with explicit human approval, and recorded
  here afterwards. Their `.harness/*` exemption is written with forward
  slashes, which never matches on Windows because the host normalizes
  `file_path` to backslashes; the grill guard therefore deadlocked, blocking
  the very file it demands be filled. The fix is additive: it restores the
  intended exemption for harness paperwork and leaves implementation files
  guarded, verified by probing both guards with simulated payloads.

## Non-Goals

- No substitution and no token generation. `core/tokenizer.py` stays untouched.
- No LLM detection layer, no Ollama, no span union beyond the reusable
  `merge_spans()`.
- No vault, no encryption, no HMAC lookup, no audit log.
- No adapters (command, hooks, gateway) and no frontend.
- No content-block-hash caching and no performance tuning.
- Do not modify or weaken the five existing gate rows.
- Do not commit the generated database.
- Do not raise the seed volume to the BRIEF's full figures.

## Acceptance Criteria

1. For every returned span, `text[span.start:span.end] == span.text`.
2. Values that fail their check digit produce zero spans: a DNI with the wrong
   letter, an IBAN failing mod-97, and a card number failing Luhn.
3. No two spans returned by the regex layer overlap; `merge_spans()` resolves
   longest-wins with a regex-priority tie-break and is tested in isolation.
4. Two consecutive seed runs produce a byte-identical `demo/manifest.json`.
5. The manifest records every inserted PII instance with table, column, offset,
   entity type and value.
6. `eval` reports recall and precision per entity type and per detector, and
   exits non-zero when recall on any structured entity is below 100%.
7. Recall is 100% for EMAIL, DNI, NIE, IBAN, PHONE, CARD and IP against the
   manifest.
8. `.harness/bin/verify` exits 0 with the new `eval` row wired in, and the five
   existing rows still green.

## Tooling And Approach Assessment

1. **What project type is this?** Pure-Python library work: text scanning with
   regular expressions plus check-digit arithmetic, and a deterministic
   synthetic-data generator writing SQLite. No network, no service, no UI.

2. **What methods, tooling, or approaches does that imply?** `re` with explicit
   word-boundary anchoring; check digits as small pure functions (DNI/NIE letter
   table, IBAN mod-97, Luhn); `dataclasses` plus `StrEnum` for the span model;
   stdlib `sqlite3`; Faker with the `es_ES` locale and a fixed seed; pytest
   parametrization for the per-entity valid/invalid matrix; mypy strict
   throughout.

3. **What risks or missing decisions does it reveal?**
   - False negatives are the real risk and are invisible without ground truth —
     which is why the manifest and the recall gate are in scope.
   - Offsets are easy to get wrong once patterns overlap, so `merge_spans()`
     needs tests independent of the patterns themselves.
   - Faker determinism depends on the Faker version as well as the seed. If it
     is not pinned, the manifest drifts and the recall gate goes flaky.
   - The generated database is git-ignored, so the gate must seed before it
     evaluates; gate runtime becomes a real constraint and drove the reduced
     volume decision.
   - Phones and IPs have no check digit and therefore carry the highest
     false-positive risk: an IP pattern also matches version strings, and a
     nine-digit run also matches order numbers. Precision must be visible even
     though only recall is gated.

4. **What should be added to validation?** A per-entity recall threshold; a
   determinism check comparing the manifest across two seed runs; a reported
   (not gated) precision figure so false positives stay visible; and
   confirmation that the five existing gate rows stay green.

5. **What is explicitly not needed now?** Ollama and the LLM layer, encryption
   and the vault, the audit log, every adapter, the frontend, prompt caching,
   and performance work — the corpus is deliberately small.

## Risk

Risk class: high

Detection is the high-risk surface named in `CLAUDE.md`: a missed span is a data
leak, and this regex layer is the guaranteed floor that must hold when the local
model is unavailable. Protected areas: the five existing gate rows must not be
weakened; the demo corpus must stay fully synthetic, with no real personal data
in fixtures or generated rows; the generated database must stay out of version
control.

## Merge Policy

High-risk human verification required. Independent `goal-verifier` and
`code-reviewer` passes before merge, including a security-focused review pass
because the change decides what counts as sensitive data.

## Status

approved — implementing

<!-- Lifecycle values read by .claude/hooks/mission-state-guard.sh:
     not started -> awaiting approval -> approved — implementing
     (or, when ALL agent-owned skip conditions hold:
      agent-owned — implementing / agent-owned — proceeding without
      pre-code approval). Implementation edits are blocked until the
      status starts with `approved` or `agent-owned`. -->
