"""Generates the fake SaaS billing database and its ground-truth manifest.

Faker with the ``es_ES`` locale and a fixed seed, valid check digits, and PII
deliberately scattered where column-level masking cannot reach. Emits
``demo/manifest.json`` alongside the SQLite database.

Two properties matter more than volume:

* **Every generated value passes its own check digit.** The BRIEF is explicit
  that a DNI with a wrong letter is not matched by the regex and then looks
  like a detector bug when it is not. This module asserts validity as it
  generates, so a bad corpus fails loudly here instead of quietly costing
  recall later.
* **The manifest is the ground truth.** It records every instance inserted,
  including person names that only the LLM layer will ever catch. Reporting
  those as misses is the point: false negatives are documented, not hidden.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from faker import Faker

from veil_pii_middleware.core.detector import (
    EntityType,
    valid_card,
    valid_dni,
    valid_iban,
    valid_nie,
    valid_spanish_phone,
)

__all__ = ["Instance", "DEFAULT_DB_PATH", "DEFAULT_MANIFEST_PATH", "SEED", "seed_demo"]

SEED: Final = 20260907
DEFAULT_DB_PATH: Final = Path("demo/veil_demo.db")
DEFAULT_MANIFEST_PATH: Final = Path("demo/manifest.json")

N_CUSTOMERS: Final = 20
N_USERS: Final = 50
N_INVOICES: Final = 100
N_TICKETS: Final = 30
N_LOG_LINES: Final = 200

#: Entity kinds the manifest may carry that no regex can prove. They are
#: recorded so the evaluation reports them as misses until the LLM layer lands.
PERSON: Final = "PERSON"

_VALIDATORS: Final[dict[str, Callable[[str], bool]]] = {
    EntityType.DNI: valid_dni,
    EntityType.NIE: valid_nie,
    EntityType.IBAN: valid_iban,
    EntityType.CARD: valid_card,
    EntityType.PHONE: valid_spanish_phone,
}

SCHEMA: Final = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    national_id TEXT NOT NULL, city TEXT NOT NULL
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
    email TEXT NOT NULL, full_name TEXT NOT NULL
);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
    amount REAL NOT NULL, notes TEXT NOT NULL
);
CREATE TABLE payments (
    id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL,
    iban TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE support_tickets (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE error_logs (
    id INTEGER PRIMARY KEY, level TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE webhooks (
    id INTEGER PRIMARY KEY, event TEXT NOT NULL, payload TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Instance:
    """One PII value as inserted, located inside a specific column."""

    table: str
    column: str
    row_id: int
    offset: int
    entity_type: str
    value: str


class _Corpus:
    """Accumulates rows and the ground truth describing them."""

    def __init__(self, faker: Faker) -> None:
        self.faker = faker
        self.instances: list[Instance] = []

    def note(
        self, table: str, column: str, row_id: int, text: str, value: str, entity_type: str
    ) -> None:
        """Record where ``value`` sits inside the stored ``text``."""
        validator = _VALIDATORS.get(entity_type)
        if validator is not None and not validator(value):
            raise ValueError(f"generated {entity_type} fails its own check digit: {value!r}")
        offset = text.find(value)
        if offset < 0:
            raise ValueError(f"{entity_type} {value!r} is not present in {table}.{column}")
        self.instances.append(
            Instance(
                table=table,
                column=column,
                row_id=row_id,
                offset=offset,
                entity_type=entity_type,
                value=value,
            )
        )

    def phone(self, variant: int) -> str:
        """Faker's es_ES phone formats vary; build one this layer must catch.

        Cycles through the ways a Spanish number is actually written. The
        corpus previously emitted only the bare nine-digit form, so the recall
        gate never exercised separators, groupings or the country prefix --
        and two real gaps sat behind a green gate until review found them.
        The 2-3-2-2 form is how a Madrid landline is normally printed.
        """
        first = self.faker.random_element(("6", "7", "9"))
        digits = first + "".join(str(self.faker.random_digit()) for _ in range(8))
        head, middle, tail = digits[:3], digits[3:6], digits[6:]
        formats = (
            digits,
            f"{head} {middle} {tail}",
            f"{digits[:2]} {digits[2:5]} {digits[5:7]} {digits[7:]}",
            f"{head}-{middle}-{tail}",
            f"+34 {head} {middle} {tail}",
        )
        return formats[variant % len(formats)]


def _build(corpus: _Corpus, connection: sqlite3.Connection) -> None:
    faker = corpus.faker
    cursor = connection.cursor()

    # --- customers: national id in its own column -------------------------
    customer_emails: list[str] = []
    for customer_id in range(1, N_CUSTOMERS + 1):
        name = faker.company()
        national_id = faker.nie() if customer_id % 5 == 0 else faker.nif()
        entity = EntityType.NIE if customer_id % 5 == 0 else EntityType.DNI
        # Some records carry the dotted form, `12.345.678-Z`, which is an
        # everyday way to write it and which the layer used to miss entirely.
        if entity is EntityType.DNI and customer_id % 3 == 1 and len(national_id) == 9:
            national_id = (
                f"{national_id[:2]}.{national_id[2:5]}.{national_id[5:8]}-{national_id[8]}"
            )
        cursor.execute(
            "INSERT INTO customers VALUES (?, ?, ?, ?)",
            (customer_id, name, national_id, faker.city()),
        )
        corpus.note("customers", "national_id", customer_id, national_id, national_id, entity)
        customer_emails.append(f"customer{customer_id}@project.com")

    # --- users: email in its own column, name only a model will catch -----
    for user_id in range(1, N_USERS + 1):
        customer_id = (user_id % N_CUSTOMERS) + 1
        email = f"user{user_id}@{faker.domain_name()}"
        full_name = faker.name()
        cursor.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?)", (user_id, customer_id, email, full_name)
        )
        corpus.note("users", "email", user_id, email, email, EntityType.EMAIL)
        corpus.note("users", "full_name", user_id, full_name, full_name, PERSON)

    # --- invoices: phone buried in a free-text note -----------------------
    for invoice_id in range(1, N_INVOICES + 1):
        customer_id = (invoice_id % N_CUSTOMERS) + 1
        if invoice_id % 3 == 0:
            phone = corpus.phone(invoice_id)
            notes = f"Contacto de facturacion: {phone}. Revisar antes de emitir."
        else:
            phone = ""
            notes = "Sin incidencias."
        cursor.execute(
            "INSERT INTO invoices VALUES (?, ?, ?, ?)",
            (invoice_id, customer_id, round(faker.random_int(10, 5000) + 0.99, 2), notes),
        )
        if phone:
            corpus.note("invoices", "notes", invoice_id, notes, phone, EntityType.PHONE)

    # --- payments: IBAN in its own column ---------------------------------
    for payment_id in range(1, N_INVOICES + 1):
        iban = faker.iban()
        cursor.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?)",
            (payment_id, payment_id, iban, faker.random_element(("paid", "failed", "pending"))),
        )
        corpus.note("payments", "iban", payment_id, iban, iban, EntityType.IBAN)

    # --- tickets: the cases column masking cannot reach -------------------
    for ticket_id in range(1, N_TICKETS + 1):
        customer_id = (ticket_id % N_CUSTOMERS) + 1
        email = customer_emails[customer_id - 1]
        # The same customer written three different ways, per the BRIEF:
        # capitalized, padded with stray whitespace, and embedded in a URL.
        # `shown` is the substring that actually lands in the text, which is
        # what the manifest must record -- collapsing those variants back to
        # one canonical value is the vault's job, not the detector's.
        variant = ticket_id % 3
        shown = email.capitalize() if variant == 0 else email
        if variant == 1:
            rendered = f"  {shown}  "
        elif variant == 2:
            rendered = f"https://app.project.com/u/{shown}"
        else:
            rendered = shown
        card = faker.credit_card_number()
        body = (
            f"El cliente escribio desde {rendered} pidiendo la factura. "
            f"Dice que le cobraron en la tarjeta {card} por error."
        )
        cursor.execute(
            "INSERT INTO support_tickets VALUES (?, ?, ?, ?)",
            (ticket_id, customer_id, "Cobro duplicado", body),
        )
        corpus.note("support_tickets", "body", ticket_id, body, shown, EntityType.EMAIL)
        corpus.note("support_tickets", "body", ticket_id, body, card, EntityType.CARD)

    # --- logs: a name that exists only inside a stack trace, plus an IP ---
    for log_id in range(1, N_LOG_LINES + 1):
        ip = ".".join(str(faker.random_int(1, 254)) for _ in range(4))
        holder = faker.name()
        message = (
            f"Traceback (most recent call last):\n"
            f'  File "billing/charge.py", line 88, in charge\n'
            f"    raise BillingError(f'card holder {holder} declined')\n"
            f"BillingError: declined from {ip}"
        )
        cursor.execute(
            "INSERT INTO error_logs VALUES (?, ?, ?)",
            (log_id, faker.random_element(("ERROR", "WARN")), message),
        )
        corpus.note("error_logs", "message", log_id, message, ip, EntityType.IP)
        corpus.note("error_logs", "message", log_id, message, holder, PERSON)

    # --- webhooks: a national id inside a JSON payload --------------------
    for webhook_id in range(1, N_TICKETS + 1):
        national_id = faker.nif()
        payload = json.dumps(
            {"event": "customer.updated", "data": {"tax_id": national_id, "source": "portal"}},
            sort_keys=True,
        )
        cursor.execute(
            "INSERT INTO webhooks VALUES (?, ?, ?)", (webhook_id, "customer.updated", payload)
        )
        corpus.note("webhooks", "payload", webhook_id, payload, national_id, EntityType.DNI)


def seed_demo(
    db_path: Path = DEFAULT_DB_PATH, manifest_path: Path = DEFAULT_MANIFEST_PATH
) -> list[Instance]:
    """Regenerate the demo database and manifest from the fixed seed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    faker = Faker("es_ES")
    # Both generators are seeded: Faker drives the providers, `random` drives
    # nothing here directly but keeps any future use reproducible too.
    Faker.seed(SEED)
    random.seed(SEED)

    corpus = _Corpus(faker)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(SCHEMA)
        _build(corpus, connection)
        connection.commit()

    payload: list[dict[str, Any]] = [asdict(instance) for instance in corpus.instances]
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return corpus.instances


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    args = parser.parse_args(argv)

    instances = seed_demo(args.db, args.manifest)
    by_type: dict[str, int] = {}
    for instance in instances:
        by_type[instance.entity_type] = by_type.get(instance.entity_type, 0) + 1
    print(f"seeded {args.db} — {len(instances)} PII instances in {args.manifest}")
    for entity_type in sorted(by_type):
        print(f"  {entity_type:<8} {by_type[entity_type]:>5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
