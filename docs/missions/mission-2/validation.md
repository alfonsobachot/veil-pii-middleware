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

| Check | Command | Required Result |
|---|---|---|
| Harness gate | `.harness/bin/verify` | exit `0` |
| Lint | `.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .` | exit `0` |
| Typecheck | `.venv/Scripts/mypy` | exit `0`, strict, no new `type: ignore` |
| Unit tests | `.venv/Scripts/pytest` | exit `0` |
| Build | `.venv/Scripts/python -m build --no-isolation` | exit `0` |
| Detector recall (unchanged) | `eval` GATES row: seed, then `python -m veil_pii_middleware.demo.eval --min-recall 1.0 --max-false-positives 0` | exit `0`; recall `1.00` on all structured types, `0` decoy hits |
| Decoys absent from the manifest | pytest: no `(table, column, row_id, offset)` in `decoys.json` also appears in `manifest.json`; `decoy_samples` never appears as a manifest table | exit `0` |
| Zero decoy hits under normal operation | pytest: `evaluate_decoys()` returns `[]` against the unmodified detector | exit `0` |
| Decoy determinism | seed twice, compare `demo/decoys.json` | identical SHA-256 (or byte-identical file contents) |
| Sabotage flips the gate | pytest, parametrized over `valid_dni`, `valid_card`, `valid_iban`: monkeypatch the matching `_PATTERNS` entry to always accept, assert `evaluate_decoys()` returns a hit for that entity type and `DecoyReport.exceeds(0)` is `True` | exit `0` (the test itself passes by observing the gate *would* fail) |
| Concrete listing, not just a count | `eval.py`'s printed report names table/column/row id/value/reason for every decoy hit | manual read during the User-Testing Validator pass |

## Scrutiny Validator

- Check the diff against the objective and non-goals — confirm
  `core/detector.py` is untouched.
- Confirm `demo/decoys.json` and `manifest.json` are disjoint on every
  identifying field, not merely "different files."
- Confirm the three check-digit decoys (DNI, card, IBAN) are asserted invalid
  at generation time in `seed.py` — a decoy that accidentally validates would
  silently defeat the whole mechanism.
- Confirm the sabotage test patches `_PATTERNS` (a value swap, observed
  through the public `detect_regex()` entry point) rather than editing
  `detector.py` or monkeypatching in a way that has no real effect (e.g.
  rebinding `detector.valid_dni` alone, which `_PATTERNS` would not see,
  since it holds direct references to the original function objects captured
  at import time).
- Confirm the new `eval` gate row change is additive: `--min-recall 1.0` is
  still present and unchanged, `--max-false-positives 0` is new alongside it.
- Confirm the five pre-existing gate rows are unmodified.
- Confirm no fixture or generated value contains real personal data.
- Confirm the false-positive threshold is visible in the gate command itself
  (not a silent default that could be dropped unnoticed).

## User-Testing Validator

- Run the seed, open `demo/decoys.json`, and confirm each entry reads as a
  plausible negative control: a DNI-shaped value with a letter that doesn't
  match its digits, a Luhn-failing card number, a mod-97-failing IBAN, a
  Spanish thousands figure, a version string, a zero-padded account number.
- Run `python -m veil_pii_middleware.demo.eval` and confirm the report shows
  a decoy section reading zero hits under the current, unmodified detector.
- Manually reproduce the sabotage: temporarily make a copy of
  `core/detector.py`, change `valid_dni`'s first line to `return True`, rerun
  `eval.py --max-false-positives 0`, confirm it now exits non-zero and prints
  the specific DNI decoy that leaked through — then restore the file.
- Confirm `demo/decoys.json` stays untracked by git, same as `manifest.json`
  and the generated database.

## Stop Conditions

Stop and ask for human input if the gate exits `2`, if a decoy accidentally
passes its own check digit (defeating the mechanism before it starts), or if
closing the blind spot appears to require touching `core/detector.py` itself.

Mission-specific stop condition: if a generated decoy cannot be made to fail
its check digit deterministically under the fixed seed after a reasonable
number of attempts, stop and report it — do not ship a decoy that might
validate by chance, and do not loosen the zero-tolerance threshold to paper
over it.
