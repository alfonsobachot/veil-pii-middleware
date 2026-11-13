# Detector benchmarks

Comparison of the soft (free-form) detection layer: local generative LLMs vs encoder NER
models. Measured on 2026-11-12 with `demo/eval.py` against the seeded synthetic corpus, at
commit `5806341` (the detector has not changed since).

## Setup

- **Hardware:** Windows 11, NVIDIA RTX 4090 (24 GB), PyTorch 2.11 (CUDA 12.8), Ollama 0.40.16.
- **Corpus:** `demo/seed.py` (Faker `es_ES`, fixed seed): 1,063 database cells, 2,590
  ground-truth PII instances (at least 100 per type), 194 decoys. English free text with
  Spanish data (names, DNI/NIE, IBAN, CIF, NSS, addresses), plus the hand-written demo
  customer (`docs/demo-script.md`).
- **Modes:** every run is **regex + one soft detector**, the way the product actually runs.
  - LLMs: `--mode union`, temperature 0, `VEIL_LLM_TIMEOUT=180`,
    `VEIL_LLM_REASONING_EFFORT=low` (gpt-oss) or `none` (qwen3.8) so the model answers with
    JSON directly.
  - Encoders: `--mode regex+encoder --encoder <spec> --encoder-threshold 0.4`, GPU, batched.
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
  with vowels, software names, role phrases...). Any span overlapping a decoy counts.
- **Exit codes:** only `--mode regex` gates (1 on a recall or decoy failure); every other
  mode is report-only. Any mode exits 2 when a detection layer call failed (see ¹ below).

## Metrics

- **Recall:** share of the real PII that was detected. This is the safety metric: every miss
  is a leak.
- **Precision:** share of the detections that were real PII. This is the noise metric: a false
  positive leaks nothing, it only over-tokenizes.
- **F1:** harmonic mean of the two.
- **F2:** weights recall twice as much as precision, which fits a privacy tool better than F1.
- **Soft FP:** soft-layer spans that matched no real value. **Decoys:** decoys hit by any
  layer in the run.

## Results: soft layer

Soft types: PERSON, ORG, ADDRESS, DOB, USERNAME and SENSITIVE (health and other GDPR special
categories); 1,063 instances.

| Detector | Type | Recall | Precision | F1 | F2 | Soft FP | Decoys | ms / cell |
|---|---|---|---|---|---|---|---|---|
| qwen3.8:27b-q4_K_M ¹ | LLM, 27.8B dense | 0.987 | 0.997 | 0.992 | 0.989 | 3 | 7 | 2060.1 |
| gpt-oss:20b | LLM, 21B MoE | 0.950 | 0.995 | 0.972 | 0.959 | 5 | 3 | 821.9 |
| qwen3:4b + GLiNER-PII ² | regex + LLM + encoder (demo config) | 0.958 | 0.769 | 0.853 | 0.913 | 305 | 51 | 340.8 |
| qwen3:4b-instruct-2507-q4_K_M | LLM, 4B | 0.907 | 0.986 | 0.945 | 0.922 | 14 | 4 | 321.0 |
| urchade/gliner_multi_pii-v1 | Encoder, GLiNER (mDeBERTa) | 0.913 | 0.769 | 0.835 | 0.880 | 291 | 47 | 17.6 |
| fastino/gliner2-privacy-filter-PII-multi | Encoder, GLiNER2 | 0.856 | 0.717 | 0.780 | 0.824 | 359 | 75 | 16.6 |
| OpenMed/privacy-filter-multilingual-v2 | Encoder, MoE token classifier | 0.610 | 0.994 | 0.756 | 0.661 | 4 | 16 | 137.5 |
| FacebookAI/xlm-roberta-large-finetuned-conll02-spanish | Encoder, classic NER | 0.516 | 0.972 | 0.674 | 0.569 | 16 | 0 | 11.3 |
| mrm8488/bert-spanish-cased-finetuned-ner | Encoder, classic NER | 0.383 | 0.327 | 0.353 | 0.370 | 839 | 10 | 7.2 |

### Per entity type (found / total)

| Detector | PERSON | ORG | ADDRESS | DOB | USERNAME | SENSITIVE |
|---|---|---|---|---|---|---|
| qwen3.8:27b-q4_K_M | 453/456 | 100/101 | 151/152 | 146/153 | 99/100 | 100/101 |
| gpt-oss:20b | 426/456 | 99/101 | 149/152 | 153/153 | 99/100 | 84/101 |
| qwen3:4b + GLiNER-PII | 456/456 | 101/101 | 151/152 | 153/153 | 100/100 | 57/101 |
| qwen3:4b-instruct-2507-q4_K_M | 435/456 | 91/101 | 149/152 | 151/153 | 98/100 | 40/101 |
| gliner_multi_pii-v1 | 456/456 | 99/101 | 127/152 | 153/153 | 93/100 | 42/101 |
| gliner2-privacy-filter-PII-multi | 452/456 | 74/101 | 72/152 | 153/153 | 100/100 | 59/101 |
| privacy-filter-multilingual-v2 | 378/456 | 6/101 | 101/152 | 73/153 | 90/100 | 0/101 |
| xlm-roberta-large-finetuned-conll02-spanish | 432/456 | 83/101 | 0/152 | 0/153 | 29/100 | 4/101 |
| bert-spanish-cased-finetuned-ner | 348/456 | 58/101 | 0/152 | 0/153 | 1/100 | 0/101 |

² Measured on 2026-11-13 with the new `--mode all` (regex + LLM + encoder, the gateway's
full configuration), same corpus and detector code. Together the two small detectors find
95.8% of the soft values, but GLiNER's noise stays (precision 0.769) and health data is
still the gap (57/101).

¹ One of the 1,063 cells got an empty response from qwen3.8 and fell back to regex-only for
that cell (the run exited 2). Its numbers are therefore a lower bound by at most that cell;
the run was not repeated (40 minutes of GPU the day before the presentation).

## Results: structured layer (regex)

The regex layer finds **100%** of every check-digit or rule-based type, with 0 false
positives and 0 decoys on its own (`--mode regex`, the gated mode, 0.1 ms per cell): EMAIL,
DNI, NIE, IBAN, CARD, PHONE, IP (v4 and v6), CIF, NSS, SECRET, PLATE, MAC, GPS.

In the combined runs no structured value is reported as missed any more. The previous
benchmark showed plates and secrets "missed" when a soft span swallowed them; the merge rule
now gives regex spans precedence and trims overlapping soft spans, so those values keep their
own exact token. The only structured noise left comes from the LLMs: extra `SECRET` spans
(qwen3.8 11, qwen3 4b 2, gpt-oss 1), e.g. on the masked placeholder `xxxxxx`, and gpt-oss
tagging two order numbers as phones.

## Findings

- **Best quality:** qwen3.8:27b. Recall 0.987 and precision 0.997, and the only detector
  that finds nearly all health data (100/101). It is also the slowest: 2.1 s per cell, 6x
  qwen3 4b and over 100x GLiNER.
- **gpt-oss:20b** is second (F2 0.959) at 0.8 s per cell. It catches most health data
  (84/101) but misses about 7% of the people (426/456).
- **qwen3:4b (the demo's LLM)** is precise (0.986) but misses 60% of health facts (40/101)
  and about 10% of companies. That gap is what the live demo runs showed too.
- **GLiNER-PII (`urchade/gliner_multi_pii-v1`)** has slightly better recall than qwen3 4b
  (0.913 vs 0.907) at 18 ms per cell, 18x faster. It finds every person (456/456) and every
  date of birth, but it is noisy: 291 soft false positives and 47 decoys (it tags invalid DNIs
  as people and invalid IPs as addresses). Noise over-tokenizes; it never leaks.
- **qwen3 4b and GLiNER fail differently**, which is why the demo runs both: GLiNER finds
  the names and birth dates qwen misses, qwen finds the addresses GLiNER misses (149 vs 127
  of 152). Neither handles health data well; only the larger LLMs do.
- **Classic NER models** (CoNLL-trained) have no address, date-of-birth or health labels, so
  they cannot cover the soft layer on their own. OpenMed's privacy filter is precise but finds
  almost no companies (6/101).
- **Health and other special-category data is the hardest type for every small detector.**
  For a deployment that must protect it, the 27B model (or a dedicated classifier) is the
  realistic option.

## The demo turn, end to end

Measured on 2026-11-12/13: the presentation prompt (`docs/demo-script.md`) through the real
gateway, hooks and Claude API, with a recording proxy between the gateway and the API. A leak
is any of the 22 synthetic values of the demo customer in clear in a forwarded request.

| Gateway configuration | Runs | Turn time (s) | Median | Demo PII leaked |
|---|---|---|---|---|
| regex + qwen3.8 27B | 6 | 71, 96, 101, 107, 115, 115 | 104 s | 0 of 6 runs |
| regex + qwen3.8 27B + GLiNER | 3 | 173, 252, 300 | 252 s | 0 of 3 runs |
| regex + qwen3 4B + GLiNER (rehearsed demo) | 2 | 63, 181 | 122 s | 0 of 2 runs |
| regex + qwen3 4B (2026-11-11) | 8 | — | — | 5 of 8 runs |

- qwen3.8 alone is as fast as the rehearsed setup and leaked nothing in 6 runs. The hooks call
  the 27B model too, so every tool result goes through it.
- qwen3.8 with GLiNER loads 21.7 of the GPU's 24 GB and every call slows down: about 2.5x
  slower, no gain in leaks.
- Separate from the detectors: when the model runs `which`, the printed PATH holds the
  Windows username (`/c/Users/<user>/bin`), and no layer tags it (1 of 6 runs with qwen3.8,
  1 of 2 with qwen3 4B + GLiNER). It is not demo data; `scripts/ask.sh` already strips `$HOME`
  from its own PATH, but Claude Code's Bash tool starts a login shell that adds it back.

## Caveats

- The corpus is synthetic. It compares detectors against each other; it is not an absolute
  real-world accuracy claim.
- One run per configuration. LLMs at temperature 0 are near-deterministic, but timings vary
  with GPU state. The first call of each LLM includes loading it into VRAM, counted in the
  totals.
- The three-layer demo configuration is in the table since 2026-11-13 (`--mode all`). The
  three-layer run with qwen3.8 27B was not measured (40 GPU minutes on presentation day).
- Decoy hits by the soft layers are over-tokenization, not leaks: an invalid DNI tagged as a
  person still becomes a token.

## Reproducing

```bash
.venv/Scripts/python -m veil_pii_middleware.demo.seed
# encoders (pip install -e ".[ner]" plus torch from the CUDA wheel index)
.venv/Scripts/python -m veil_pii_middleware.demo.eval --mode regex+encoder \
  --encoder gliner:urchade/gliner_multi_pii-v1 --encoder-threshold 0.4
# LLMs (Ollama)
VEIL_LLM_BASE_URL=http://127.0.0.1:11434 VEIL_LLM_MODEL=gpt-oss:20b \
  VEIL_LLM_REASONING_EFFORT=low VEIL_LLM_TIMEOUT=180 \
  .venv/Scripts/python -m veil_pii_middleware.demo.eval --mode union
```
