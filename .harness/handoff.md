# Handoff

## Summary

The deterministic regex detection layer is implemented, together with the
synthetic corpus and the evaluation that measures it. All six gate rows are
green, and both independent validators have run against a fresh context.

The security-focused review returned two blocking findings — real false
negatives in the guaranteed floor — which are fixed and re-verified below.
Ready for human review. Nothing is committed; no remote exists.

Read the Issues Found section first. Five genuine detection defects surfaced
during this mission, and the mechanisms that caught them matter more than the
line count.

## Implemented

- `core/detector.py`: `EntityType` and `Detector` as `StrEnum`, a frozen
  `Span` whose `__post_init__` refuses offsets that disagree with its own
  text, the check-digit validators (`valid_dni`, `valid_nie`, `valid_iban`,
  `valid_card`, `valid_ipv4`, `valid_spanish_phone`), `merge_spans()` and
  `detect_regex()`.
- `demo/seed.py`: a 7-table SQLite corpus, ~740 PII instances, deterministic
  under a fixed Faker seed. It validates every value it generates against the
  same check digit the detector will apply, so a broken corpus fails loudly at
  generation instead of quietly costing recall.
- `demo/eval.py`: recall and precision per entity type and per detector,
  strict `(offset, value)` matching, a `--min-recall` threshold, and the
  concrete list of misses.
- `tests/test_detector.py` (check digits, offsets, overlap policy, regressions)
  and `tests/test_demo.py` (corpus integrity, measured contract).
- `.harness/bin/verify`: one new `eval` row that seeds, then scores at
  `--min-recall 1.0`.
- `pyproject.toml`: Faker added to the dev extras, pinned to one minor line.
- `.gitignore`: the generated manifest joins the generated database.

## Left Undone

- **Person names.** 250 instances sit in the corpus at 0.000 recall, reported
  as `(not gated)`. No regex can prove a name; this is the LLM layer's work
  and is published rather than hidden, per BRIEF section 4.1.
- **Dot-separated phone numbers are missed by design.** In Spanish the dot is
  the thousands separator, so accepting it as a digit separator laid claim to
  every nine-digit figure opening 6, 7 or 9. `912.345.678` is therefore read as
  a number, not a phone. The rule is recorded in the module docstring and
  reverses with one character if the trade ever looks wrong.
- Everything in the mission's Non-Goals: substitution, tokens, the vault,
  the audit log, adapters, the frontend, content-block caching.

## Commands Run

| Command | Exit Code | Notes |
|---|---:|---|
| `bash .harness/bin/verify` | 0 | All 6 rows green |
| `.venv/Scripts/pytest` | 0 | 74 passed in 0.66s |
| `.venv/Scripts/mypy` | 0 | strict, 17 source files, no `type: ignore` anywhere |
| `.venv/Scripts/ruff check . && ruff format --check .` | 0 | `.harness` excluded as vendored |
| `python -m veil_pii_middleware.demo.seed` | 0 | 743 instances; byte-identical across 3 runs |
| `python -m veil_pii_middleware.demo.eval --min-recall 1.0` | 0 | 1.000 recall on all 7 structured types, 0 false positives |
| `python -m build --no-isolation` | 0 | ~1.3s |

## Files Changed

```
M  .claude/hooks/grill-guard.sh          M  pyproject.toml
M  .claude/hooks/mission-state-guard.sh  M  veil_pii_middleware/core/detector.py
M  .gitignore                            M  veil_pii_middleware/demo/eval.py
M  .harness/bin/verify                   M  veil_pii_middleware/demo/seed.py
M  .harness/mission.md                   ?? tests/test_demo.py
M  .harness/validation.md                ?? tests/test_detector.py
```

## Issues Found

Seven real detection defects, in the order they surfaced. Every one was a
false negative except number 5, which is the interesting exception.

1. **An IP at the end of a sentence was invisible.** The trailing guard was
   `(?![0-9.])`, so the full stop closing a log line killed the match — a
   silent miss in the single highest-volume case for addresses. Caught by the
   corpus-coverage test.
2. **A 12-digit Maestro PAN was below the validator's floor.** `valid_card`
   required 13 digits; Faker generated a valid 12-digit card and the seed's
   self-check refused to proceed. Fixed by widening the detector, not by
   trimming the corpus — which the validation contract explicitly forbids.
3. **Zero-padded octets were missed.** `192.168.001.001` returned nothing.
   Octets are now range-checked in `valid_ipv4` rather than pattern-matched.
4. **Dot-separated phone and card numbers were missed.** Reported by the
   security review, on the premise that dots are the house style on Spanish
   invoices. Fixed by widening the separator class — and then reversed, see 5.
5. **Accepting the dot as a phone separator was itself a defect.** Human
   challenge to the premise, checked against evidence: in Spanish the dot is
   the *thousands separator*, so `El importe asciende a 912.345.678 euros`
   was being tokenized as a phone. Not a leak, but a systematic false positive
   that destroys an invoice total — breaking the debugging workflow the
   product exists to serve. A phone carries no check digit, so nothing
   downstream would ever have caught it. Dots were removed from phone and
   card, and kept for DNI and NIE, where the control letter filters what the
   looser pattern over-claims. That principle is now written down.
6. **Phone grouping was hard-coded to 3-3-3**, so `91 234 56 78` — simply how
   a Madrid landline is printed — was invisible. Now any grouping of nine
   digits is accepted and `valid_spanish_phone` decides.
7. **Dotted national IDs were missed.** `12.345.678-Z` is an everyday way to
   write a DNI; the validator accepted it, but the pattern never proposed it.

The country prefix was also hard-coded to `+34`/`0034`; international numbers
are now recognised generically, so `+44 20 7946 0958` is detected.

Two corpus defects surfaced as well, both caught by the seed validating its own
ground truth: an email variant recorded in canonical form while the text held a
capitalized one, and — the important one — **the corpus only ever generated
bare nine-digit phones**, which is exactly why defects 4 and 6 sat behind a
green gate. It now cycles five written forms and includes dotted DNIs.

Nothing was ever resolved by weakening a check.

## Gotchas Learned

| Trigger | Symptom | Root Cause | Fix | Prevention | Evidence |
|---|---|---|---|---|---|
| Writing `.harness/mission.md` through Edit/Write on Windows | `grill-guard.sh` blocked the write, demanding a filled Intake Grill | The guard's `.harness/*` exemption uses forward slashes; the host normalizes `file_path` to backslashes, so it never matched. The guard deadlocked on the very file it demands be filled | Added a backslash pattern to the `case` in both guards | Probe path guards with simulated payloads in the host's native path format before trusting them | `grill-guard.sh`, `mission-state-guard.sh`; both probed with synthetic payloads and confirmed to still block `detector.py` while the mission was unapproved |
| Green recall gate on a narrow corpus | 1.000 recall on PHONE while dotted numbers were undetectable | The generator emitted one format; the manifest can only measure what it contains | Vary the generated formats | A recall gate proves coverage of the corpus, never of reality. Corpus breadth is part of the check, not a detail of it | Defect 2 above; corpus now covers 5 phone formats |
| A reviewer's premise about a local convention | Dots were added as a phone separator, creating a systematic false positive on Spanish figures | The premise ("dots are the house style for Spanish phones") was accepted without checking, and the opposite fact — the dot is the thousands separator — is the decisive one | Removed dots from patterns with no check digit; kept them where a control character filters | Verify a domain-convention claim against the notation itself before widening a pattern. A reviewer and a native speaker can both be confident and only one be right | Defect 5; `test_spanish_thousands_separator_is_not_a_phone` |
| A generator that shares a helper with the code under test | Ground truth could agree with a broken detector by construction | Circularity | Faker generates the values; the seed validates them; unit tests anchor the validators against published constants (`12345678Z`, `X1234567L`, the ISO IBAN example, `4111111111111111`) | Anchor check-digit logic on external published values, never on the implementation's own output | `tests/test_detector.py` |

## Procedure Compliance

`.harness/mission.md` and `.harness/validation.md` were both written and
approved before any implementation file was edited; the mission-state guard
mechanically enforced this, and was observed blocking `detector.py` while the
status was `awaiting approval`. The lane was `agent-with-checkpoint`, and the
pre-code stop was honoured with explicit human approval. Validators ran as
independent subagents with fresh context; the main session did not review its
own work.

One deviation: `.claude/hooks/*.sh` were edited outside the originally declared
Scope. The human approved that fix explicitly at the checkpoint, but it was not
written into the mission document at the time. The goal-verifier caught the
omission and Scope has since been corrected to record it.

## Risk Review

**High**, unchanged from the mission. This code decides what counts as personal
data, and every defect above was a false negative — the failure mode that leaks.
The mitigations that actually did work: check-digit validation as a hard filter,
a corpus that validates its own ground truth, strict `(offset, value)` matching
in the evaluation, and an independent security review that found two blocking
misses the gate could not see.

The residual risk is corpus breadth. Recall is 1.000 against 743 instances of
synthetic Spanish data; it is not a claim about real traffic. Defect 2 is the
proof — a green gate coexisted with an entire invisible number format.

## Human Verification Guide

Run everything below from **Git Bash**, not PowerShell and not WSL. The harness
scripts shipped with CRLF line endings, which a POSIX bash refuses to parse,
and WSL will not find the Windows virtualenv executables.

Shortest path, about a minute:

```bash
bash .harness/bin/verify                                   # expect: 6 rows green, exit 0
.venv/Scripts/python -m veil_pii_middleware.demo.eval      # expect: 1.000 on all 7, PERSON 0.000 (not gated)
```

Then confirm the gate cannot be satisfied dishonestly:

```bash
# 1. Stub a check digit to always accept, and watch the tests fail.
#    Take a copy FIRST — this is a deliberate act of vandalism on a live file,
#    and an editor undo is not a safety net if the edit goes wrong.
cp veil_pii_middleware/core/detector.py /tmp/detector.bak
#    Now make `valid_dni` return True as its first statement, and run:
.venv/Scripts/pytest tests/test_detector.py -k check_digit
#    Expect FAILED assertions. An import error or SyntaxError means the file
#    got mangled, not that the check ran — restore and try again.
cp /tmp/detector.bak veil_pii_middleware/core/detector.py
#    Once the change is committed, `git checkout -- <file>` restores it too.
#    Until then it does not: the commit predates this work and would wipe it.

# 2. Confirm the corpus really hides PII where column masking cannot reach.
.venv/Scripts/python -c "import sqlite3;print(sqlite3.connect('demo/veil_demo.db').execute('select body from support_tickets limit 1').fetchone()[0])"

# 3. Confirm the separator policy in both directions.
.venv/Scripts/python -c "
from veil_pii_middleware.core.detector import detect_regex
for t in ['Telefono 91 234 56 78', 'Telefono +44 20 7946 0958', 'DNI 12.345.678-Z',
          'El importe asciende a 912.345.678 euros', 'Servidor 91.234.56.78']:
    print(t, '->', [(s.entity_type.value, s.text) for s in detect_regex(t)])"
# expect: PHONE, PHONE, DNI, nothing, IP
```

Judgement calls worth a human eye, in priority order:

1. **Card floor at 12 digits** (`valid_card`). Buys real Maestro PANs, costs
   precision on 12-digit reference codes. Deliberate, and reversible in one line.
2. **Zero-padded octets accepted** as IPv4, which RFC 3986 treats as ambiguous
   with octal.
3. **The separator policy**: dots accepted for DNI and NIE, refused for phone
   and card. The rule is "a pattern may be loose only where a check digit
   filters it", which is worth agreeing with explicitly, since it is the reason
   `912.345.678` is a figure and `12.345.678-Z` is a national ID.
4. **The two `.claude/hooks/*.sh` edits** — security controls, changed outside
   the original scope with approval. Worth reading the diff directly.

## Verdict

Ready for human review. Blocking review findings resolved and re-verified; not
committed.
