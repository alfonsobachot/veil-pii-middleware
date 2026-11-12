# Q&A notes

Short answers for the questions most likely to come up after the demo on 2026-11-13. Each one
names the honest limit first, then what contains it. Numbers come from `docs/benchmarks.md`
and the recorded demo runs in `docs/demo-script.md`.

## "Is it really nothing? How do you know no PII leaked?"

The demo runs were recorded: a proxy between the gateway and the API saved every request,
and each one was searched for every PII value of the demo customer. With regex + qwen3 4b +
GLiNER, 6 consecutive turns had zero matches. With regex + qwen3 4b alone, 5 of 8 turns let
one soft value through (a company, a birth date, a health fact). That is why the demo runs
all three layers.

## "What does the regex guarantee, and what doesn't it?"

13 types are validated by check digits or strict rules (DNI/NIE letter, IBAN mod-97, card
Luhn, CIF, NSS, IP ranges, secrets, plates, MAC, GPS, email, phone): 100% recall and zero
decoys on the corpus, every run, in 0.1 ms per cell. Names, companies, addresses, birth
dates, usernames and health data cannot be proven by a regex; they depend on the soft
layers, which are measured, not guaranteed.

## "What happens if the local LLM is down?" (D7)

Detection falls back to regex-only and the request is forwarded: structured PII is still
tokenized, soft PII (names, companies) is not. A slow model has the same effect: with the old
30 s timeout a 52-row table dump timed out and its names reached the API, which is why the
timeout is now 120 s. A strict mode that blocks instead of falling back is the planned fix;
the panel should also show when a fallback happened.

## "A failing command skips the hook?" (S3)

Yes: when a Bash call exits non-zero, Claude Code does not run PostToolUse, so the hook never
tokenizes that output. In the demo setup the gateway still tokenizes it on the next request,
so it does not reach the API in clear. In hooks-only mode (no gateway) it would.

## "What about the `/veil` command?" (H14)

The `/veil` slash command tokenizes the text it is given, but Claude Code also copies the raw
argument into the transcript as `<command-args>`. So `/veil` alone does not protect the
prompt; the gateway does. It stays as a tested code path, not as a guarantee.

## "Does the LLM traffic leave the machine?"

No. The detection model runs in Ollama on this machine (`127.0.0.1`); the URL comes from
`VEIL_LLM_BASE_URL`, never a hardcoded host. GLiNER runs in the gateway process on the GPU.
The only outbound traffic is the tokenized request to the Claude API.

## "Can the model still do its job with tokens?"

Yes, because tokens are stable: the same value always maps to the same token within a
session, so the model can join rows, compare accounts and spot the duplicate charge. When it
needs a real value (a SQL `WHERE`), PreToolUse swaps the token back just before the command
runs locally. In the demo the model found the duplicate charge, the cancelled mandate and the
contact preference without seeing any of the values.

## "Why not just use a bigger model?"

qwen3.8 27b is the most accurate soft detector we measured (recall 0.987, and 100/101 health
facts against 40/101 for qwen3 4b), but it takes 2.1 s per database cell, 6x qwen3 4b. See
`docs/benchmarks.md`. The demo uses qwen3 4b plus GLiNER (18 ms per cell): they fail
differently, GLiNER finds the names and birth dates the 4B misses and the 4B finds the
addresses GLiNER misses. Health data is the weak spot of every small detector; protecting it
properly needs the larger model.

## "Where are the real values stored?"

In `.veil/vault.db`: AES-GCM ciphertext plus an HMAC lookup, never plaintext; the key comes
from the environment. The audit log records tokens, types and detectors, never values.
Revealing a value in the panel is an explicit action and is logged.

## Known cosmetic issues

- GLiNER sometimes tokenizes ordinary words in the model's shell commands (`Name`, `-Force`).
  PreToolUse resolves them before the command runs, so nothing breaks, but the panel shows it.
- A turn takes about a minute, and occasionally three when a local LLM call runs long.
