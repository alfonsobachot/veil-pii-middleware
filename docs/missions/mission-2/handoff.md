# Handoff

## Summary

The gate is no longer blind to a sabotaged check-digit validator. A new
false-positive gate scores the detector against twelve deliberate decoys —
six categories, two variants each — that must never be detected: a DNI with
the wrong check letter, a card failing Luhn, an IBAN failing mod-97, a
Spanish thousands-separated figure, a version string, and a zero-padded
account number. None of them appear in `demo/manifest.json`. All six gate
rows are green, and both independent validators have run against a fresh
context.

The code-reviewer's pass found a real gap — the card and IBAN decoy
generators only checked their own type's validator, not the full detection
surface, so a Luhn-failing card could in principle be picked up by an
unrelated, looser pattern (a reproduced, concrete collision with
`valid_international_phone`). That is fixed below and re-verified. Ready for
human review. Nothing is committed; no remote exists.

## Implemented

- `demo/seed.py`: `Decoy` dataclass, `_Corpus.decoys` / `note_decoy()`, a new
  `decoy_samples` table, and generators for the six decoy categories
  (`_bad_dni_letters`, `_bad_luhn_cards`, `_bad_mod97_ibans`, plus the fixed
  thousands/version/zero-padded value tuples), all deterministic under the
  existing fixed Faker seed. `note_decoy()` now runs `detect_regex()` over
  the full rendered text and raises if *any* span — of any entity type —
  lands on the decoy's offset and value, not only the type it was built to
  fail. `DEFAULT_DECOYS_PATH` and `demo/decoys.json` output added alongside
  the existing manifest write.
- `demo/eval.py`: `DecoyHit`, `DecoyReport.exceeds()`, `evaluate_decoys()`
  (strict offset+value matching against the decoy set, symmetric to how
  `evaluate()` matches the manifest), `--decoys` and `--max-false-positives`
  CLI flags (default `0`), and a printed decoy section naming every hit's
  table, column, row id, value and reason — not only a count.
- `.harness/bin/verify`: the `eval` row now runs
  `--min-recall 1.0 --max-false-positives 0`, both thresholds explicit.
- `.gitignore`: `demo/decoys.json` joins `demo/manifest.json` as a generated,
  git-ignored artifact.
- `tests/test_decoys.py` (new): decoys are disjoint from the manifest and
  live only in `decoy_samples`; all six categories are present; zero decoy
  hits under the real, unmodified validators; decoy-set determinism across
  two seed runs; a parametrized test sabotaging `valid_dni`, `valid_card` and
  `valid_iban` in turn (by monkeypatching the module-level `_PATTERNS` tuple,
  never `detector.py` itself) that proves each one flips
  `DecoyReport.exceeds(0)` to `True`.
- `tests/test_demo.py`: the existing fixture and the seed-determinism test
  now pass an explicit temp `decoys_path`, so they stop writing to the
  repo's real `demo/decoys.json` during test runs.
- `.harness/mission.md`, `.harness/validation.md`: written for this mission.
- `docs/missions/mission-1/`, `docs/missions/mission-2/`: the prior
  (detector) mission's and this mission's harness paperwork archived here,
  per the user's request to keep per-mission history outside the live,
  overwritten `.harness/*` files.

## Left Undone

Nothing beyond this mission's own Non-Goals: `core/detector.py` is untouched
(confirmed empty diff), no new entity type or pattern, no manifest schema
change, no increase in corpus volume beyond the twelve decoys.

## Commands Run

| Command | Exit Code | Notes |
|---|---:|---|
| `bash .harness/bin/verify` | 0 | All 6 rows green, both before and after the reviewer fix |
| `.venv/Scripts/pytest -v` | 0 | 84 passed (was 74 before this mission; +7 in `test_decoys.py`, +3 from the parametrized sabotage case) |
| `.venv/Scripts/mypy` | 0 | strict, 18 source files, no `type: ignore` |
| `.venv/Scripts/ruff check . && ruff format --check .` | 0 | |
| `python -m veil_pii_middleware.demo.seed` | 0 | manifest and decoys both byte-identical across reruns |
| `python -m veil_pii_middleware.demo.eval --max-false-positives 0` | 0 | 0 decoy hits, recall 1.000 on all structured types |
| Manual sabotage: patched `valid_dni` to `return True` in a live copy of `detector.py`, ran eval, restored | 1, then 0 | Recall stayed 1.000; decoy section reported 2 DNI hits by name (`decoy_samples.note#1/#2`); exit 1. Restored, reran, exit 0, `git status` confirmed no diff on `detector.py` |
| Reproduced the reviewer's collision directly: constructed `0087654321987654` (fails Luhn) and called `note_decoy` on it | raises `ValueError` | Confirms the fix: caught as "actually detected as PHONE" before the fix would have shipped silently |

## Files Changed

```
M  .gitignore                        M  veil_pii_middleware/demo/eval.py
M  .harness/bin/verify                M  veil_pii_middleware/demo/seed.py
M  .harness/mission.md
M  .harness/validation.md
M  tests/test_demo.py
?? docs/missions/
?? tests/test_decoys.py
```

## Issues Found

Two issues, both from the independent code-reviewer pass, neither from the
goal-verifier (which passed all six acceptance criteria on the first check
but flagged that this file was still the *prior* mission's handoff — fixed
by writing this one).

1. **Should-fix, real and reproduced.** `_bad_luhn_cards` and
   `_bad_mod97_ibans` only checked the value against their own type's
   validator (`valid_card`, `valid_iban`), never against the full
   `detect_regex()` surface. The reviewer traced a concrete collision: a
   16-digit Luhn-failing card decoy starting with `"00"` would be picked up
   by the unmodified `_PHONE_INTL` pattern, since `valid_international_phone`
   is a length floor with no checksum. Today's fixed seed happens not to draw
   such a value, so every gate passed — but that was incidental to the seed,
   not guaranteed by the generator's logic. Fixed by moving the check into
   `note_decoy()` itself: it now runs the *whole* detector over the rendered
   text and raises if any span, of any entity type, lands on the decoy's
   offset and value. Reproduced the exact collision manually against the
   patched code and confirmed it now raises instead of silently shipping.
2. **Nits.** An unreachable `"X"` fallback branch in `_bad_mod97_ibans`
   (Spanish IBANs always end in a digit) marked `# pragma: no cover` instead
   of left silently untested; `read_text("utf-8")` in the new test file
   changed to the codebase's `read_text(encoding="utf-8")` convention.

## Gotchas Learned

| Trigger | Symptom | Root Cause | Fix | Prevention | Evidence |
|---|---|---|---|---|---|
| A decoy generator validated against its own entity type only | A card decoy could theoretically be misclassified as PHONE by an unrelated, looser pattern, defeating the "always 0 false positives" guarantee | Multiple regex patterns compete for the same digit run; checking one type's validator says nothing about whether a *different* pattern's looser validator would still accept the same text at the same offset | `note_decoy()` now runs the full `detect_regex()` over the rendered text and rejects any decoy that produces a span under any entity type, not just its own | A "must never be detected" fixture needs to be checked against the whole detection surface, the same way a "must always be detected" fixture (`note()`, for real instances) already is — type-local validation is not equivalent to system-level validation | `_bad_luhn_cards`/`_bad_mod97_ibans` in `seed.py`; reproduced manually with `0087654321987654` |

## Procedure Compliance

`.harness/mission.md` and `.harness/validation.md` were written and approved
(explicit human approval via `AskUserQuestion`) before any implementation
file was edited. The lane was `agent-with-checkpoint`, high risk, and the
pre-code stop was honoured. Validators ran as independent subagents with
fresh context; the main session did not review its own work. Per the human's
request mid-checkpoint, the prior mission's and this mission's harness
paperwork were archived to `docs/missions/mission-1/` and
`docs/missions/mission-2/` before the live `.harness/*` files were
overwritten for this mission.

## Risk Review

**High**, unchanged from the mission. `core/detector.py` itself was never
touched — confirmed by an empty `git diff --stat` throughout, independently
re-checked by both validators. The mitigations that worked: a false-positive
gate with a concrete, named listing (not just a count); a sabotage test that
patches the detector's actual dispatch table rather than a name that would
have had no effect; and an independent security review that caught a
generator-level gap the mission's own author-side testing had not surfaced.

The residual risk is the same shape as the prior mission's: the decoy set is
twelve values under one fixed seed. `note_decoy()`'s system-wide check now
makes "this decoy is undetectable today" a build-time guarantee rather than
an incidental measurement, but a category not yet imagined (a decoy shape
nobody has thought of yet) is still not covered — the same partial-floor
trade the detector itself already documents.

## Human Verification Guide

Run everything below from **Git Bash**.

```bash
bash .harness/bin/verify                                              # expect: 6 rows green, exit 0
.venv/Scripts/python -m veil_pii_middleware.demo.eval                 # expect: recall 1.000 all types, decoys: 0 false positive(s), PASS
```

Confirm the false-positive gate reacts to a sabotaged validator (take a copy
first — this edits a live file):

```bash
cp veil_pii_middleware/core/detector.py /tmp/detector.bak
# Make valid_dni's first statement `return True`, then:
.venv/Scripts/python -m veil_pii_middleware.demo.eval --max-false-positives 0
# expect: recall still 1.000, but a "decoys: 2 false positive(s)" section
# naming the two DNI decoys by table/row/value, and exit 1.
cp /tmp/detector.bak veil_pii_middleware/core/detector.py
git status --short veil_pii_middleware/core/detector.py               # expect: no output
```

Or run the equivalent as a test, no live edits required:

```bash
.venv/Scripts/pytest tests/test_decoys.py -v -k sabotag
# expect: 3 passed (DNI, CARD, IBAN)
```

Judgement calls worth a human eye:

1. **Zero-tolerance threshold** (`--max-false-positives 0`, hard-coded as the
   default and explicit in the gate row). Any single decoy detected fails
   the gate; there is no budget to raise quietly.
2. **`note_decoy()`'s system-wide check** is new generation-time cost (one
   `detect_regex()` call per decoy, twelve total) in exchange for a
   structural guarantee instead of a seed-incidental one. Worth confirming
   the reasoning holds: a "must never be detected" fixture should be checked
   against everything that could detect it, not just the validator it was
   built to fail.
3. **Decoys live in a dedicated `decoy_samples` table**, not interleaved into
   the seven real tables — keeps decoy offsets independent of the real
   corpus, but means decoys don't currently exercise "a decoy next to real
   PII in the same free-text cell," which is a plausible next iteration if
   that scenario matters later.

## Verdict

Ready for human review. The code-reviewer's should-fix finding is resolved
and re-verified (manually reproduced, then confirmed caught); not committed.
