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

## Executable Checks

List every command or acceptance check the worker and validators must run.
Executable checks must be wired into the gate — as `.harness/bin/verify` GATES
rows or a script the gate runs. Checks that stay manual belong under the
validator sections below, not here.

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore` |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Build | `.venv/Scripts/python -m build --no-isolation` | exit `0` |
| Detector recall | new `eval` GATES row: seed, then `python -m veil_pii_middleware.demo.eval --min-recall 1.0` | exit `0`; recall `1.00` for EMAIL, DNI, NIE, IBAN, PHONE, CARD, IP |
| Span offset integrity | pytest case asserting `text[s.start:s.end] == s.text` for every span over the whole corpus | exit `0` |
| Check-digit rejection | pytest cases: DNI with wrong letter, IBAN failing mod-97, card failing Luhn | zero spans returned for each |
| Overlap resolution | pytest cases for `merge_spans()` in isolation, plus an assertion that no two returned spans overlap | exit `0` |
| Seed determinism | run the seed twice, compare `demo/manifest.json` | identical SHA-256 |

## Scrutiny Validator

Code-level review requirements:

- Check the diff against the objective and non-goals.
- Confirm tests or checks cover the changed behavior.
- Confirm the gate was not weakened.
- Confirm each check-digit validator **rejects** a deliberately corrupted value,
  not merely accepts a valid one. A validator that always returns `True` passes
  a recall gate perfectly and is the most dangerous failure mode here.
- Confirm `merge_spans()` is tested independently of the regex patterns, so an
  offset bug cannot hide behind a pattern that happens not to overlap.
- Confirm no fixture, test constant or generated row contains real personal
  data — everything must come from Faker under the fixed seed or be obviously
  synthetic.
- Confirm the recall threshold lives in the gate row and that lowering it would
  be a visible gate change, not a silent default.
- Confirm the five pre-existing gate rows are unmodified.
- Confirm no substitution, tokenization, vault or audit code was added; those
  are explicit non-goals.

## User-Testing Validator

Behavior-level review requirements:

- Check the user-visible workflow or documented outcome.
- Confirm acceptance criteria with observable evidence.
- Note anything a human should manually verify.
- Run the seed, open the SQLite database, and confirm PII really does land in
  free-text places that column-level masking cannot reach: an email inside a
  ticket body, a national ID inside a webhook JSON payload, a name only inside
  a stack trace, a phone in `invoices.notes`.
- Read the eval report and confirm it names the **concrete misses**, not only
  aggregate percentages. Per BRIEF section 4.1, false negatives are documented,
  not hidden.
- Confirm the generated database and manifest stay untracked by git.

## Stop Conditions

Stop and ask for human input if the gate exits `2`, high-risk evidence is unclear,
or the task requires changing verification policy.

Mission-specific stop condition: if recall cannot reach 100% on a structured
entity type, stop and report the miss with its cause. Do not relax the threshold,
loosen a check-digit validator, or remove the offending case from the corpus —
those all convert a detection defect into a green gate.
