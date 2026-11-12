# Demo script

The live turn for the presentation on 2026-11-13. Every value below is synthetic: the demo
customer is hand-written in `veil_pii_middleware/demo/seed.py` (`_build_demo_incident`).

## Setup

Run everything from Git Bash.

1. Reload the Ollama model first. The runner's RAM grows with use (prompt cache): after a
   few hours of requests it held 6.4 GB of RAM and 16 GB of commit, and GLiNER then failed
   to load for lack of commit memory. A fresh load takes 0.8 GB.

   ```bash
   curl -s http://127.0.0.1:11434/api/generate \
     -d '{"model":"qwen3:4b-instruct-2507-q4_K_M","keep_alive":0}' > /dev/null
   ```

2. Terminal 1, the gateway with all three layers (regex, qwen3 4b, GLiNER). Both warm-up
   lines and `veil encoder: loaded gliner:...` must print:

   ```bash
   export VEIL_LLM_BASE_URL=http://127.0.0.1:11434
   export VEIL_LLM_MODEL=qwen3:4b-instruct-2507-q4_K_M
   export VEIL_ENCODER=gliner:urchade/gliner_multi_pii-v1
   scripts/run-gateway.sh
   ```

3. Terminal 2, the same two `VEIL_LLM_*` exports, then the prompt below. The hooks only use
   the LLM when it is exported in the shell that runs `ask.sh`; they inherit that shell's
   environment, not the gateway's.

4. Browser: open `http://127.0.0.1:8787/veil-panel/flow` before running the prompt. It works
   offline (vendored scripts). The classic panel stays at `/veil-panel` as a fallback.

## Showing it

A turn takes about a minute, so narrate while it runs and use the replay afterwards:

- While it runs, each step appears as a card and the diagram animates the hop: prompt →
  gateway → Claude API, then for each query Claude → PreToolUse → local database →
  PostToolUse → back to the API, then the answer → gateway → your screen.
- The cards to stop on: **1 Your prompt** (only the email becomes a token); the
  **PreToolUse** card with the user lookup (the token on the left, the real email on the
  right: only the local database sees it); the big **PostToolUse** card (left: the
  database output with names, DNI, IBANs, card; right: what Claude gets, all tokens);
  **The answer** (tokens on the left, real values on your screen on the right).
- **▶ Replay this turn** walks through the finished turn one step every 2.6 s.
- The counters on top: distinct values kept local, local tool calls, API requests.
- Sidebar: **Vault** → filter `IBAN` → **Reveal** one token to show the encrypted store
  resolving it locally; the **Audit log** lists every token minted, never a value.

Why each piece is there (live runs on 2026-11-11, recording what the gateway sent upstream):

- **GLiNER.** With regex + qwen3 4b only, 5 of 8 runs leaked a soft value: a company in a
  CRLF error output, a birth date and address next to existing tokens, "depression" in a
  7.7 KB block. GLiNER caught each of those blocks in under 0.5 s. With all three layers,
  6 consecutive runs had zero leaks.
- **`VEIL_LLM_TIMEOUT` 120 s** (the scripts' default). The model sometimes dumps a whole
  table (`select * from users`, ~8 KB); qwen3 4b needs ~45 s for it. With 30 s the call
  failed, detection fell back to regex-only and 52 users' names, birth dates and addresses
  reached the API.

Known cosmetic noise: GLiNER sometimes tokenizes words inside the model's shell commands
(`Name`, `Get-ChildItem -Force`). PreToolUse resolves them before the command runs, so the
queries work, but the panel shows them. One turn in six took ~3 min: a qwen call ran long
and returned no JSON (GLiNER covered that block).

Accents: PreToolUse adds `-X utf8` to every Python call (the model often runs `python -I`,
which ignores `PYTHONUTF8`), so "Lucía" no longer comes back as "Luc�a". The isolated demo
settings allow the rewritten command; without those rules `claude -p` refuses every query.

## Prompts

Main:

```bash
scripts/ask.sh --demo-db "The user lucia.ferrer@riberanorte.es has an invoice problem. Can you check the database and tell me what happened?"
```

Not "Customer lucia.ferrer@...": GLiNER tags the word "Customer" as a PERSON, so the prompt
pane shows `PII_PERSON_0001 PII_EMAIL_0001 has ...`.

Fallback (same customer, asks for the data directly):

```bash
scripts/ask.sh --demo-db "Can you pull up everything we have on the user lucia.ferrer@riberanorte.es?"
```

Neither prompt names files, modules or tables, so the panel's prompt pane shows only the
email as a token (S5): `The user PII_EMAIL_0001 has an invoice problem. ...` in the last
three runs. The fallback was checked against the detector only, not run end to end.

## The story in the data

Invoice 102 of Transportes Ribera Norte S.L. was collected twice: once from the current
direct-debit IBAN and again from an old IBAN whose mandate the customer cancelled. The
support ticket, an error log, a webhook and a CRM note all point at it.

| Table | Rows | PII the rows carry |
|---|---|---|
| `users` | 51 (Lucía), 52 (colleague) | email, full name, phone, DNI, date of birth, address |
| `customers` | 21 | company name (ORG), CIF |
| `invoices` | 101, 102, 103 | person name, mobile and landline phones in the notes |
| `payments` | 101–104 (two for invoice 102) | three IBANs |
| `support_tickets` | 31 | name, DNI, email, two IBANs, card, phone, colleague's name and email |
| `error_logs` | 201 | email, IBAN, IP |
| `webhooks` | 31 | email, IBAN, IP |
| `crm_notes` | 307 | name, health data (`depression`), date of birth, DNI |

The first five tables follow from the email by foreign keys, so a normal investigation
(user → customer → invoices → payments → tickets) returns at least 19 distinct PII values
of 10 types; `tests/test_demo.py::test_demo_customer_rows_carry_many_kinds_of_pii` pins
that. The last three are only found by searching text for the email, the invoice or the
name.

## Expected flow

1. Prompt: the gateway tokenizes the email (`PII_EMAIL_0001`) before the request leaves.
2. The model queries the demo DB with the token; PreToolUse resolves it to the real email.
3. The query output is tokenized by PostToolUse (and again by the gateway on the next
   request), so the model sees only `PII_*` tokens.
4. The answer comes back with tokens; the gateway resolves them on screen.

## Values to grep in captured upstream requests

None of these may appear in clear in any request the gateway forwards.

```text
lucia.ferrer@riberanorte.es
Lucía Ferrer Montoya
+34 612 384 905
47392816M
1986-03-14
14/03/1986
Calle de Santiago 27, 3º B, 47001 Valladolid
jorge.alcaraz@riberanorte.es
Jorge Alcaraz Prieto
655 902 117
52618407L
1979-11-02
Paseo de Zorrilla 112, 47006 Valladolid
Transportes Ribera Norte S.L.
B84712637
983 214 776
ES5601822370420201593318
ES9820385778983000760236
ES6100811532610001286377
4532014876239153
88.12.45.201
depression
```

City names (`Valladolid` on its own) and amounts are not PII and are expected in clear.
