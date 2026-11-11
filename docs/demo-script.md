# Demo script

The live turn for the presentation on 2026-11-13. Every value below is synthetic: the demo
customer is hand-written in `veil_pii_middleware/demo/seed.py` (`_build_demo_incident`).

## Prompts

Main:

```bash
scripts/ask.sh --demo-db "Customer lucia.ferrer@riberanorte.es has an invoice problem. Can you check the database and tell me what happened?"
```

Fallback (same customer, asks for the data directly):

```bash
scripts/ask.sh --demo-db "Can you pull up everything we have on the user lucia.ferrer@riberanorte.es?"
```

Neither prompt names files, modules or tables, so the panel's prompt pane should show only
the email as a token (S5).

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
| `crm_notes` | 307 | name, health data (`medical leave`), date of birth, DNI |

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
medical leave
```

City names (`Valladolid` on its own) and amounts are not PII and are expected in clear.
