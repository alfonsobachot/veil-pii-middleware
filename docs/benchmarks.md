# Detector benchmarks

Comparison of the soft (free-form) detection layer: local generative LLMs vs encoder NER
models. Measured on 2026-11-10/11 with `demo/eval.py` against the seeded synthetic corpus.

> **Superseded corpus.** These numbers come from the first corpus (533 cells, only 6 instances
> for ADDRESS/DOB/USERNAME/SENSITIVE). The corpus has since grown to 839 cells with at least
> 100 instances per type and 194 decoys. The scoring rules also changed (2026-11-11, see
> Matching below): soft coverage went from one span covering half of the value to the union
> of spans covering 90%, each span credits one value, and decoys count on any overlap. All
> runs must be repeated before these figures are presented.

## Setup

- **Hardware:** Windows 11, NVIDIA RTX 4090 (24 GB), PyTorch 2.11 (CUDA 12.8), Ollama 0.40.16.
- **Corpus:** `demo/seed.py` (Faker `es_ES`, fixed seed): 533 database cells, 941 ground-truth
  PII instances, 23 decoys. English free text with Spanish data (names, DNI/NIE, IBAN, CIF,
  NSS, addresses).
- **Modes:** every run is **regex + one soft detector**, the way the product actually runs.
  - LLMs: `--mode union`, temperature 0, `VEIL_LLM_REASONING_EFFORT=low` (gpt-oss) or `none`
    (qwen3.8) so the model answers with JSON directly.
  - Encoders: `--mode regex+encoder --encoder <spec>`, threshold 0.4, GPU, batched.
- **Matching:**
  - Structured types (DNI, IBAN, card, secret...) must match the exact span and type.
  - Soft types match when the soft spans credited to the value together cover at least 90%
    of its alphanumeric characters, whatever their label. For the soft layer the label is
    cosmetic: a company tagged `PERSON` is still tokenized and never leaves the machine.
  - Each span is credited to at most one value, so one span stretched over a name and a
    diagnosis detects only one of them. A span on part of a real value that falls short of
    90% is not a false positive.
- **Decoys:** values that *look* like PII but are not (DNI with a wrong control letter,
  Luhn-failing card, IP with an octet over 255, a clock time, `password=********`, a plate
  with vowels...). Any span overlapping a decoy is a guaranteed false positive.
- **Exit codes:** only `--mode regex` gates (1 on a recall or decoy failure); every other
  mode is report-only. Any mode exits 2 when a detection layer call failed, since its
  numbers would silently be regex-only.

## Metrics

- **Recall:** share of the real PII that was detected. This is the safety metric: every miss
  is a leak.
- **Precision:** share of the detections that were real PII. This is the noise metric: a false
  positive leaks nothing, it only over-tokenizes.
- **F1:** harmonic mean of the two.
- **F2:** weights recall twice as much as precision, which fits a privacy tool better than F1.

## Results: soft layer

PERSON, ORG, ADDRESS, DOB, USERNAME and SENSITIVE (health and other GDPR special categories);
289 instances.

| Detector | Type | Recall | Precision | F1 | F2 | Soft FP | Decoys | ms / cell |
|---|---|---|---|---|---|---|---|---|
| qwen3.8:27b-q4_K_M | LLM, 27.8B dense | **0.997** | **0.997** | **0.997** | **0.997** | 1 | 1 ¹ | 1532.5 |
| qwen3:4b-instruct-2507-q4_K_M | LLM, 4B (current default) | 0.971 | 0.955 | 0.963 | 0.967 | 14 | 0 | 254.5 |
| urchade/gliner_multi_pii-v1 | Encoder, GLiNER (mDeBERTa) | 0.993 | 0.826 | 0.902 | 0.955 | 64 | 2 | **35.5** |
| gpt-oss:20b | LLM, 21B MoE | 0.931 | **0.997** | 0.963 | 0.944 | 1 | 0 | 635.8 |
| FacebookAI/xlm-roberta-large-finetuned-conll02-spanish | Encoder, classic NER | 0.863 | 0.917 | 0.889 | 0.873 | 24 | 0 | 12.3 |
| OpenMed/privacy-filter-multilingual-v2 | Encoder, MoE token classifier | 0.827 | 0.819 | 0.823 | 0.825 | 56 | 0 | 297.1 |
| fastino/gliner2-privacy-filter-PII-multi | Encoder, GLiNER2 | 0.961 | 0.473 | 0.634 | 0.796 | 328 | 5 | 24.3 |
| mrm8488/bert-spanish-cased-finetuned-ner | Encoder, classic NER | 0.797 | 0.358 | 0.494 | 0.640 | 437 | 4 | 7.9 |

¹ The plate `1234 AEI` (invalid: Spanish plates have no vowels) was reported as `PLATE` by the
LLM. Fixed after the run: LLM-reported plates and MAC addresses must now pass the regex rule,
as check-digit types already did. Not re-run yet.

### Per entity type (found / total)

| Detector | PERSON | ORG | ADDRESS | DOB | USERNAME | SENSITIVE |
|---|---|---|---|---|---|---|
| qwen3.8:27b | 261/262 | 20/20 | 6/6 | 6/6 | 6/6 | 6/6 |
| qwen3:4b | 259/262 | 16/20 | 6/6 | 6/6 | 6/6 | 4/6 |
| gliner_multi_pii-v1 | 262/262 | 20/20 | 6/6 | 6/6 | 6/6 | 4/6 |
| gpt-oss:20b | 241/262 | 20/20 | 6/6 | 6/6 | 6/6 | 6/6 |
| xlm-roberta CoNLL-es | 252/262 | 8/20 | 0/6 | 0/6 | 4/6 | 0/6 |
| OpenMed privacy-filter | 235/262 | 1/20 | 6/6 | 5/6 | 6/6 | 0/6 |
| gliner2-privacy-filter | 262/262 | 11/20 | 5/6 | 6/6 | 6/6 | 4/6 |
| bert-spanish NER | 233/262 | 10/20 | 0/6 | 0/6 | 1/6 | 0/6 |

## Results: structured layer (regex)

The regex layer finds **100%** of every check-digit or rule-based type in every run, with 0
false positives and 0 decoys on its own (`--mode regex`, the gated mode): EMAIL, DNI, NIE,
IBAN, CARD, PHONE, IP (v4 and v6), CIF, NSS, SECRET, PLATE, MAC, GPS.

In a few combined runs a structured value is reported as missed: GLiNER-PII on 4 of 6 plates,
qwen3.8 on 3 of 20 secrets. **None of these leak.** The soft detector returned a longer span
that contains the value (`Courier van 5219 MDX` tagged `ORG`; `billing:g7bHSF33PO52FL` tagged
`SECRET`). The merge rule keeps the longest span, so the value is still tokenized, just inside
a larger token. It is a measurement artifact plus some over-tokenization, not a leak.

## Findings

- **Best quality:** qwen3.8:27b, near-perfect on every soft type, but the slowest at 1.5 s per
  cell (6x the current default).
- **Best quality / speed trade-off:** GLiNER-PII (`urchade/gliner_multi_pii-v1`). It has 0.993
  recall at 35 ms per cell: 7x faster than the current 4B LLM and 44x faster than qwen3.8. Its
  weakness is precision (0.826): it over-flags people, which over-tokenizes but never leaks.
  On F2 it is third overall.
- **gpt-oss:20b** is the most precise, and perfect on companies and health data. It misses
  names inside code: about 8% of the people named in Python stack traces, e.g.
  `raise BillingError(f'card holder Maximiano de Atienza declined')`.
- **Biggest gain over the current default (qwen3:4b):** companies (80% to 100%) and health data
  (67% to 100%) with the larger LLMs.
- **Classic NER models** (CoNLL-trained) have no address, date-of-birth or health labels, so
  they cannot cover the soft layer on their own.
- **On GPU, an encoder is also fast:** running on GPU adds speed to its size advantage. It is
  not a CPU-only trade-off.

## Caveats

- The corpus is synthetic and small for some types (ADDRESS, DOB, USERNAME, SENSITIVE have 6
  instances each). It compares detectors against each other; it is not an absolute
  real-world accuracy claim.
- One run per configuration. LLMs at temperature 0 are near-deterministic, but timings vary
  with GPU state. The first call of each LLM includes loading it into VRAM: 30-45 s, counted
  in the totals.
- The `qwen3:4b` row comes from the 2026-11-11 run, after the fixes below. An earlier run
  (before fixes) had 15 decoy hits.

## Reproducing

```bash
.venv/Scripts/python -m veil_pii_middleware.demo.seed
# encoders (pip install -e ".[ner]" plus torch from the CUDA wheel index)
.venv/Scripts/python -m veil_pii_middleware.demo.eval --mode regex+encoder \
  --encoder gliner:urchade/gliner_multi_pii-v1
# LLMs (Ollama)
VEIL_LLM_BASE_URL=http://127.0.0.1:11434 VEIL_LLM_MODEL=gpt-oss:20b \
  VEIL_LLM_REASONING_EFFORT=low VEIL_LLM_TIMEOUT=180 \
  .venv/Scripts/python -m veil_pii_middleware.demo.eval --mode union
```

## Fixes made during benchmarking

- **IBAN leak:** an IBAN followed by a 4-letter word (`... 1332 with care`) was not detected at
  all. This bug predates the benchmark and is fixed now.
- **LLM checks:** structured spans reported by the LLM must pass the same check-digit or
  rule validation as regex. This removed all LLM decoy hits for qwen3:4b (15 to 0).
- **Unknown labels:** soft-layer spans with an unrecognized label are kept as `OTHER` instead
  of failing the whole block.
- **GLiNER2 offsets:** they drift on repeated whitespace, so offsets are now verified against
  the source text.
