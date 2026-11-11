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
from functools import partial
from pathlib import Path
from typing import Any, Final

from faker import Faker

from veil_pii_middleware.core.detector import (
    EntityType,
    detect_regex,
    valid_card,
    valid_cif,
    valid_dni,
    valid_iban,
    valid_ipv4,
    valid_ipv6,
    valid_nie,
    valid_nss,
    valid_spanish_phone,
)

__all__ = [
    "DEMO_EMAIL",
    "Decoy",
    "Instance",
    "DEFAULT_DB_PATH",
    "DEFAULT_DECOYS_PATH",
    "DEFAULT_MANIFEST_PATH",
    "SEED",
    "seed_demo",
]

SEED: Final = 20260907
DEFAULT_DB_PATH: Final = Path("demo/veil_demo.db")
DEFAULT_MANIFEST_PATH: Final = Path("demo/manifest.json")
DEFAULT_DECOYS_PATH: Final = Path("demo/decoys.json")

N_CUSTOMERS: Final = 20
N_USERS: Final = 50
N_INVOICES: Final = 100
N_TICKETS: Final = 30
N_LOG_LINES: Final = 200

#: Entity kinds the manifest may carry that no regex can prove. They are
#: recorded so the evaluation reports them as misses until the LLM layer lands.
PERSON: Final = "PERSON"
ORG: Final = "ORG"
ADDRESS: Final = "ADDRESS"
DOB: Final = "DOB"
USERNAME: Final = "USERNAME"
SENSITIVE: Final = "SENSITIVE"

_VALIDATORS: Final[dict[str, Callable[[str], bool]]] = {
    EntityType.DNI: valid_dni,
    EntityType.NIE: valid_nie,
    EntityType.IBAN: valid_iban,
    EntityType.CARD: valid_card,
    EntityType.PHONE: valid_spanish_phone,
    EntityType.CIF: valid_cif,
    EntityType.NSS: valid_nss,
    EntityType.IP: lambda v: valid_ipv4(v) or valid_ipv6(v),
}

SCHEMA: Final = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    national_id TEXT NOT NULL, city TEXT NOT NULL
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
    email TEXT NOT NULL, full_name TEXT NOT NULL, phone TEXT NOT NULL,
    national_id TEXT NOT NULL, date_of_birth TEXT NOT NULL, address TEXT NOT NULL
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
CREATE TABLE decoy_samples (
    id INTEGER PRIMARY KEY, category TEXT NOT NULL, note TEXT NOT NULL
);
CREATE TABLE crm_notes (
    id INTEGER PRIMARY KEY, author TEXT NOT NULL, body TEXT NOT NULL
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


@dataclass(frozen=True, slots=True)
class Decoy:
    """A value that must never be detected: invalid PII, or a PII look-alike.

    Recorded separately from `Instance` and never written to the manifest.
    The recall gate has a known-positive set to measure itself against; this
    is the known-negative counterpart, so a false-positive gate has something
    concrete to test itself against too -- including the case where a
    check-digit validator silently regresses to accepting everything.
    """

    table: str
    column: str
    row_id: int
    offset: int
    value: str
    reason: str


class _Corpus:
    """Accumulates rows and the ground truth describing them."""

    def __init__(self, faker: Faker) -> None:
        self.faker = faker
        self.instances: list[Instance] = []
        self.decoys: list[Decoy] = []

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

    def note_at(
        self,
        table: str,
        column: str,
        row_id: int,
        text: str,
        value: str,
        entity_type: str,
        offset: int,
    ) -> None:
        """Like ``note``, for a value located at a known ``offset``."""
        validator = _VALIDATORS.get(entity_type)
        if validator is not None and not validator(value):
            raise ValueError(f"generated {entity_type} fails its own check digit: {value!r}")
        if text[offset : offset + len(value)] != value:
            raise ValueError(f"{entity_type} {value!r} is not at offset {offset} in {table}")
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

    def note_decoy(
        self, table: str, column: str, row_id: int, text: str, value: str, reason: str
    ) -> None:
        """Record ``value`` as a known-negative: it must never produce a span.

        Unlike `note`, nothing here is validated as *correct* PII -- the whole
        point is that it either fails its check digit or was never PII to
        begin with. It goes to `self.decoys`, not `self.instances`, so it
        never reaches the manifest.

        Checked against the *whole* detection surface, not just the decoy's
        own entity type: a card deliberately failing Luhn could still be
        picked up by an unrelated pattern with a looser floor (the
        zero-padded-account decoy is exactly this shape for
        ``valid_international_phone``). A decoy that validates by accident,
        under any entity type, defeats the mechanism before it starts, so
        this fails loudly here instead of surfacing as an unexplained hit in
        `demo/eval.py` later.
        """
        offset = text.find(value)
        if offset < 0:
            raise ValueError(f"decoy {value!r} is not present in {table}.{column}")
        collisions = [
            span for span in detect_regex(text) if span.start == offset and span.text == value
        ]
        if collisions:
            raise ValueError(
                f"decoy {value!r} ({reason}) is actually detected as "
                f"{collisions[0].entity_type} -- construct a different decoy"
            )
        self.decoys.append(
            Decoy(
                table=table,
                column=column,
                row_id=row_id,
                offset=offset,
                value=value,
                reason=reason,
            )
        )

    def phone(self, variant: int) -> str:
        """A Spanish phone number drawn from the main generator."""
        return _spanish_phone(self.faker, variant)


def _spanish_phone(faker: Faker, variant: int) -> str:
    """Faker's es_ES phone formats vary; build one this layer must catch.

    Cycles through the ways a Spanish number is actually written. The corpus
    previously emitted only the bare nine-digit form, so the recall gate never
    exercised separators, groupings or the country prefix -- and two real gaps
    sat behind a green gate until review found them. The 2-3-2-2 form is how a
    Madrid landline is normally printed.
    """
    first = faker.random_element(("6", "7", "9"))
    digits = first + "".join(str(faker.random_digit()) for _ in range(8))
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
        # Some records carry the hyphenated form. The dotted `12.345.678-Z` is
        # deliberately absent: no pattern claims it, so seeding it would only
        # manufacture a miss.
        if entity is EntityType.DNI and customer_id % 3 == 1 and len(national_id) == 9:
            national_id = f"{national_id[:8]}-{national_id[8]}"
        cursor.execute(
            "INSERT INTO customers VALUES (?, ?, ?, ?)",
            (customer_id, name, national_id, faker.city()),
        )
        corpus.note("customers", "national_id", customer_id, national_id, national_id, entity)
        corpus.note("customers", "name", customer_id, name, name, ORG)
        customer_emails.append(f"customer{customer_id}@project.com")

    # --- users: email in its own column, name only a model will catch -----
    # The profile columns (phone, DNI, birth date, address) were added later
    # and come from their own generator, so the values drawn from `faker`
    # here and in every table after this one did not change when they landed.
    profile = Faker("es_ES")
    profile.seed_instance(SEED + 1)
    for user_id in range(1, N_USERS + 1):
        customer_id = (user_id % N_CUSTOMERS) + 1
        email = f"user{user_id}@{faker.domain_name()}"
        full_name = faker.name()
        phone = _spanish_phone(profile, user_id)
        national_id = _dni(profile)
        birth = profile.date_of_birth(minimum_age=18, maximum_age=80).strftime("%Y-%m-%d")
        address = f"{profile.street_address()}, {profile.postcode()} {profile.city()}"
        cursor.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, customer_id, email, full_name, phone, national_id, birth, address),
        )
        corpus.note("users", "email", user_id, email, email, EntityType.EMAIL)
        corpus.note("users", "full_name", user_id, full_name, full_name, PERSON)
        corpus.note("users", "phone", user_id, phone, phone, EntityType.PHONE)
        corpus.note("users", "national_id", user_id, national_id, national_id, EntityType.DNI)
        corpus.note("users", "date_of_birth", user_id, birth, birth, DOB)
        corpus.note("users", "address", user_id, address, address, ADDRESS)

    # --- invoices: phone buried in a free-text note -----------------------
    for invoice_id in range(1, N_INVOICES + 1):
        customer_id = (invoice_id % N_CUSTOMERS) + 1
        if invoice_id % 3 == 0:
            phone = corpus.phone(invoice_id)
            notes = f"Billing contact: {phone}. Check before issuing."
        else:
            phone = ""
            notes = "No issues."
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
        extra, extra_notes = _ticket_extra(corpus, ticket_id)
        body = (
            f"Customer wrote from {rendered} asking for the invoice. "
            f"Says they were charged on card {card} by mistake. {extra}"
        )
        cursor.execute(
            "INSERT INTO support_tickets VALUES (?, ?, ?, ?)",
            (ticket_id, customer_id, "Duplicate charge", body),
        )
        corpus.note("support_tickets", "body", ticket_id, body, shown, EntityType.EMAIL)
        corpus.note("support_tickets", "body", ticket_id, body, card, EntityType.CARD)
        for value, extra_type in extra_notes:
            corpus.note("support_tickets", "body", ticket_id, body, value, extra_type)

    # --- logs: a name that exists only inside a stack trace, plus an IP ---
    for log_id in range(1, N_LOG_LINES + 1):
        ip = (
            faker.ipv6()
            if log_id % 4 == 1
            else ".".join(str(faker.random_int(1, 254)) for _ in range(4))
        )
        holder = faker.name()
        message = (
            f"Traceback (most recent call last):\n"
            f'  File "billing/charge.py", line 88, in charge\n'
            f"    raise BillingError(f'card holder {holder} declined')\n"
            f"BillingError: declined from {ip}"
        )
        log_extra: list[tuple[str, str]] = []
        if log_id % 4 == 2:
            mac = faker.mac_address()
            message += f"\nDEBUG device {mac} dropped from the session"
            log_extra.append((mac, EntityType.MAC))
        if log_id % 10 == 3:
            line, secret = _secret_line(corpus, log_id)
            message += f"\nDEBUG loaded config: {line}"
            log_extra.append((secret, EntityType.SECRET))
        cursor.execute(
            "INSERT INTO error_logs VALUES (?, ?, ?)",
            (log_id, faker.random_element(("ERROR", "WARN")), message),
        )
        corpus.note("error_logs", "message", log_id, message, ip, EntityType.IP)
        corpus.note("error_logs", "message", log_id, message, holder, PERSON)
        for value, extra_type in log_extra:
            corpus.note("error_logs", "message", log_id, message, value, extra_type)

    # --- webhooks: a national id inside a JSON payload --------------------
    for webhook_id in range(1, N_TICKETS + 1):
        national_id = faker.nif()
        cif = _cif(corpus)
        nss = _nss(corpus, webhook_id)
        payload = json.dumps(
            {
                "event": "customer.updated",
                "data": {
                    "tax_id": national_id,
                    "company_tax_id": cif,
                    "social_security": nss,
                    "source": "portal",
                },
            },
            sort_keys=True,
        )
        cursor.execute(
            "INSERT INTO webhooks VALUES (?, ?, ?)", (webhook_id, "customer.updated", payload)
        )
        corpus.note("webhooks", "payload", webhook_id, payload, national_id, EntityType.DNI)
        corpus.note("webhooks", "payload", webhook_id, payload, cif, EntityType.CIF)
        corpus.note("webhooks", "payload", webhook_id, payload, nss, EntityType.NSS)


#: (sentence fragment, the sensitive term the ground truth records). The
#: manifest records the core term; soft layers are scored by overlap, so a
#: model returning the whole phrase still counts.
_CONDITIONS: Final = (
    ("is diabetic", "diabetic"),
    ("is undergoing chemotherapy", "chemotherapy"),
    ("uses a wheelchair", "wheelchair"),
    ("has a severe hearing impairment", "hearing impairment"),
    ("is on sick leave for depression", "depression"),
    ("is pregnant", "pregnant"),
)
_PLATE_LETTERS: Final = "BCDFGHJKLMNPRSTVWXYZ"
_ALNUM: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _random_string(corpus: _Corpus, alphabet: str, length: int) -> str:
    return "".join(corpus.faker.random_element(alphabet) for _ in range(length))


def _ticket_extra(corpus: _Corpus, ticket_id: int) -> tuple[str, list[tuple[str, str]]]:
    """One extra sentence per ticket, rotating through the newer categories."""
    faker = corpus.faker
    kind = ticket_id % 5
    if kind == 0:
        person = faker.name()
        dob = faker.date_of_birth(minimum_age=18, maximum_age=90).strftime("%d/%m/%Y")
        return f"Contact person: {person}, born {dob}.", [(person, PERSON), (dob, DOB)]
    if kind == 1:
        address = f"{faker.street_address()}, {faker.postcode()} {faker.city()}"
        return f"Please ship the corrected invoice to {address}.", [(address, ADDRESS)]
    if kind == 2:
        username = f"{faker.user_name()}{faker.random_int(10, 99)}"
        return f"Their portal username is {username}.", [(username, USERNAME)]
    if kind == 3:
        person = faker.name()
        phrase, term = _CONDITIONS[ticket_id % len(_CONDITIONS)]
        return (
            f"Note from the agent: {person} {phrase}, please handle with priority.",
            [(person, PERSON), (term, SENSITIVE)],
        )
    plate = f"{faker.random_int(1000, 9999)} {_random_string(corpus, _PLATE_LETTERS, 3)}"
    lat = f"{faker.random_int(36, 43)}.{faker.random_int(100000, 999999)}"
    lon = f"-{faker.random_int(1, 8)}.{faker.random_int(100000, 999999)}"
    gps = f"{lat}, {lon}"
    return (
        f"Courier van {plate} reported its last location at {gps}.",
        [(plate, EntityType.PLATE), (gps, EntityType.GPS)],
    )


def _secret_line(corpus: _Corpus, log_id: int) -> tuple[str, str]:
    """A config line leaking one credential, cycling through common shapes."""
    kind = (log_id // 10) % 4
    if kind == 0:
        password = _random_string(corpus, _ALNUM, 14)
        return f"DATABASE_URL=postgres://billing:{password}@db.internal:5432/app", password
    if kind == 1:
        key = "AKIA" + _random_string(corpus, "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", 16)
        return f"aws_access_key_id={key}", key
    if kind == 2:
        body = _random_string(corpus, _ALNUM, 24)
        signature = _random_string(corpus, _ALNUM, 32)
        jwt = f"eyJhbGciOiJIUzI1NiJ9.eyJ{body}.{signature}"
        return f"Authorization: Bearer {jwt}", jwt
    key = "sk_live_" + _random_string(corpus, _ALNUM, 24)
    return f"stripe.api_key = {key}", key


_DNI_LETTERS: Final = "TRWAGMYFPDXBNJZSQVHLCKE"


def _dni(faker: Faker) -> str:
    """A valid DNI drawn from ``faker``'s own generator.

    Faker's ``nif()`` draws from the global ``random`` module, not from the
    instance, so calling it on a separately seeded generator would still
    shift every value the main generator produces after it.
    """
    number = faker.random_int(0, 99_999_999)
    return f"{number:08d}{_DNI_LETTERS[number % 23]}"


def _cif(corpus: _Corpus) -> str:
    """A valid company tax ID (letter B: limited company, numeric control)."""
    digits = "".join(str(corpus.faker.random_digit()) for _ in range(7))
    for control in "0123456789":
        candidate = f"B{digits}{control}"
        if valid_cif(candidate):
            return candidate
    raise ValueError(f"no valid CIF control for {digits!r}")  # pragma: no cover


def _nss(corpus: _Corpus, row: int) -> str:
    """A valid social security number, alternately compact and slash-separated."""
    province = corpus.faker.random_int(1, 52)
    number = corpus.faker.random_int(10_000_000, 99_999_999)
    head = f"{province:02d}{number:08d}"
    control = int(head) % 97
    compact = f"{head}{control:02d}"
    if not valid_nss(compact):  # pragma: no cover - same rule as the validator
        raise ValueError(f"generated NSS fails its own check: {compact!r}")
    return compact if row % 2 else f"{head[:2]}/{head[2:]}/{control:02d}"


# --------------------------------------------------------------------------
# Decoys: deliberately invalid PII, and non-PII that resembles it. Inserted
# into the database, never into the manifest, so the false-positive gate in
# `demo/eval.py` has a known-negative set to test itself against -- the same
# way the manifest gives the recall gate a known-positive set.
# --------------------------------------------------------------------------

_DNI_DECOY_LETTERS: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Spanish thousands-separated figures. Nine digits opening 6, 7 or 9 would be
# read as a phone if the dot were ever accepted as a separator again -- see
# the separator policy in `core/detector.py`.
_THOUSANDS_DECOYS: Final = ("912.345.678", "612.345.678")
# Semantic version strings: three dot-separated groups, not four, so today's
# IP pattern already ignores them. A regression guard against a looser one.
_VERSION_DECOYS: Final = ("1.2.3", "10.4.2")
# Zero-padded reference/account numbers. `0012345678` is eight digits after a
# `00` prefix, one short of the legacy international-phone floor of nine.
_ZERO_PADDED_DECOYS: Final = ("0012345678", "0098765432")
# Four dot-separated groups shaped exactly like an IP, but with an octet over
# 255 -- `valid_ipv4` must reject these; added 2026-09-08 (BRIEF SS6, item 16)
# because the regex layer's own decoy corpus had no invalid-octet IP to catch
# a regression, and an LLM layer accepting one as a real IP was previously
# unmeasured in every `--mode` (`evaluate_decoys()` hardcoded `detect_regex()`).
_INVALID_OCTET_IP_DECOYS: Final = ("192.168.1.256", "999.1.2.3")


def _bad_dni_letters(faker: Faker, count: int) -> list[str]:
    """DNI-shaped values whose control letter is deliberately wrong."""
    values: list[str] = []
    for _ in range(count):
        digits = "".join(str(faker.random_digit()) for _ in range(8))
        for letter in _DNI_DECOY_LETTERS:
            candidate = f"{digits}{letter}"
            if not valid_dni(candidate):
                values.append(candidate)
                break
        else:  # pragma: no cover - 23 candidate letters, one is always wrong
            raise ValueError(f"could not construct an invalid DNI from {digits!r}")
    return values


def _bad_luhn_cards(faker: Faker, count: int) -> list[str]:
    """Card-shaped digit runs whose Luhn checksum is deliberately wrong."""
    values: list[str] = []
    for _ in range(count):
        digits = "".join(str(faker.random_digit()) for _ in range(16))
        if valid_card(digits):
            digits = digits[:-1] + str((int(digits[-1]) + 1) % 10)
        if valid_card(digits):  # pragma: no cover - Luhn always changes on a +1 bump
            raise ValueError(f"could not construct a Luhn-failing card from {digits!r}")
        values.append(digits)
    return values


def _bad_mod97_ibans(faker: Faker, count: int) -> list[str]:
    """Valid-shaped IBANs whose mod-97 check digit is deliberately wrong."""
    values: list[str] = []
    for _ in range(count):
        iban = faker.iban()
        if valid_iban(iban):
            last = iban[-1]
            # Spanish IBANs (this corpus's only locale) always end in a digit.
            bumped = str((int(last) + 1) % 10) if last.isdigit() else "X"  # pragma: no cover
            iban = iban[:-1] + bumped
        if valid_iban(iban):  # pragma: no cover - mod-97 is sensitive to every digit
            raise ValueError(f"could not construct a mod-97-failing IBAN from {iban!r}")
        values.append(iban)
    return values


def _bad_cifs(corpus: _Corpus, count: int) -> list[str]:
    """CIF-shaped values whose control digit is deliberately wrong."""
    values: list[str] = []
    for _ in range(count):
        good = _cif(corpus)
        bad = good[:-1] + str((int(good[-1]) + 1) % 10)
        if valid_cif(bad):  # pragma: no cover - one control per digit set
            raise ValueError(f"could not construct an invalid CIF from {good!r}")
        values.append(bad)
    return values


def _bad_nss(corpus: _Corpus, count: int) -> list[str]:
    """NSS-shaped values whose control digits are deliberately wrong."""
    values: list[str] = []
    for row in range(count):
        bad = ""
        while not bad or valid_card(bad):  # a 12-digit run can pass Luhn by chance
            good = _nss(corpus, 1).replace("/", "")
            bad = f"{good[:10]}{(int(good[10:]) + 1) % 97:02d}"
        if valid_nss(bad):  # pragma: no cover - mod 97 changes on +1
            raise ValueError(f"could not construct an invalid NSS from {good!r}")
        values.append(bad if row % 2 else f"{bad[:2]}/{bad[2:10]}/{bad[10:]}")
    return values


def _bad_nies(corpus: _Corpus, count: int) -> list[str]:
    """NIE-shaped values whose control letter is deliberately wrong."""
    values: list[str] = []
    for _ in range(count):
        good = corpus.faker.nie()
        for letter in _DNI_DECOY_LETTERS:
            candidate = good[:-1] + letter
            if not valid_nie(candidate):
                values.append(candidate)
                break
    return values


# --------------------------------------------------------------------------
# CRM notes: free-text agent notes, four sentences each, mixing entity types.
# Exists to give every type at least ~100 ground-truth instances -- with a
# handful per type, a 100% score is statistically meaningless (6/6 is
# compatible with a true recall as low as 61%).
# --------------------------------------------------------------------------

#: Extra instances per type the notes add on top of the base corpus, chosen so
#: every type reaches at least 100.
NOTE_QUOTAS: Final[dict[str, int]] = {
    ADDRESS: 94,
    DOB: 94,
    USERNAME: 94,
    SENSITIVE: 94,
    EntityType.PLATE: 94,
    EntityType.GPS: 94,
    ORG: 80,
    EntityType.SECRET: 80,
    EntityType.NIE: 96,
    EntityType.CIF: 70,
    EntityType.NSS: 70,
    EntityType.CARD: 70,
    EntityType.PHONE: 67,
    EntityType.DNI: 54,
    EntityType.MAC: 50,
    EntityType.EMAIL: 20,
}
SENTENCES_PER_NOTE: Final = 4

#: (phrase as written, core term the ground truth records) -- health,
#: disability, religion, sexual orientation, political and union affiliation.
_SENSITIVE_FACTS: Final = (
    ("is diabetic", "diabetic"),
    ("is undergoing chemotherapy", "chemotherapy"),
    ("uses a wheelchair", "wheelchair"),
    ("has a severe hearing impairment", "hearing impairment"),
    ("is on sick leave for depression", "depression"),
    ("is pregnant", "pregnant"),
    ("was diagnosed with epilepsy", "epilepsy"),
    ("is HIV positive", "HIV positive"),
    ("has multiple sclerosis", "multiple sclerosis"),
    ("is recovering from a heart attack", "heart attack"),
    ("suffers from chronic anxiety", "chronic anxiety"),
    ("is visually impaired", "visually impaired"),
    ("is in treatment for alcohol addiction", "alcohol addiction"),
    ("has bipolar disorder", "bipolar disorder"),
    ("is autistic", "autistic"),
    ("had a kidney transplant", "kidney transplant"),
    ("is a practising Muslim", "practising Muslim"),
    ("is a Jehovah's Witness", "Jehovah's Witness"),
    ("is an observant Jew", "observant Jew"),
    ("is a devout Catholic", "devout Catholic"),
    ("is openly gay", "gay"),
    ("is transgender", "transgender"),
    ("is bisexual", "bisexual"),
    ("is a member of the CCOO union", "member of the CCOO union"),
    ("is a UGT union delegate", "UGT union delegate"),
    ("is a Vox party member", "Vox party member"),
    ("is a Podemos activist", "Podemos activist"),
    ("has a criminal record", "criminal record"),
)
_DOB_FORMATS: Final = ("%d/%m/%Y", "%Y-%m-%d", "%d %B %Y", "%d-%m-%Y")


def _note_sentence(corpus: _Corpus, entity: str, index: int) -> tuple[str, list[tuple[str, str]]]:
    """One sentence carrying one instance of ``entity`` (plus a PERSON when natural)."""
    faker = corpus.faker
    variant = index % 4
    if entity == ADDRESS:
        address = f"{faker.street_address()}, {faker.postcode()} {faker.city()}"
        if variant == 1:
            address = f"{faker.street_address()}, {faker.city()}"
        templates = (
            "Ship the replacement card to {v}.",
            "Customer moved; new address is {v}.",
            "Billing address on file: {v}.",
            "Technician visit booked at {v}.",
        )
        return templates[variant].format(v=address), [(address, ADDRESS)]
    if entity == DOB:
        dob = faker.date_of_birth(minimum_age=18, maximum_age=90).strftime(
            _DOB_FORMATS[index % len(_DOB_FORMATS)]
        )
        person = faker.name()
        templates = (
            "{p} was born on {v}.",
            "Identity check for {p}: DOB {v}.",
            "{p}'s date of birth is {v}, verified by phone.",
            "KYC record for {p} (born {v}) updated.",
        )
        return templates[variant].format(p=person, v=dob), [(person, PERSON), (dob, DOB)]
    if entity == USERNAME:
        username = (
            f"{faker.user_name()}{faker.random_int(10, 99)}",
            f"@{faker.user_name()}",
            f"{faker.first_name().lower()}.{faker.last_name().lower()}{faker.random_int(1, 9)}",
            f"{faker.user_name()}_{faker.random_int(100, 999)}",
        )[variant]
        templates = (
            "Their portal username is {v}.",
            "Customer complained publicly as {v} on social media.",
            "Login {v} locked after five failed attempts.",
            "Reset the 2FA device for account {v}.",
        )
        return templates[variant].format(v=username), [(username, USERNAME)]
    if entity == SENSITIVE:
        phrase, term = _SENSITIVE_FACTS[index % len(_SENSITIVE_FACTS)]
        person = faker.name()
        templates = (
            "Agent note: {p} {f}, please handle with care.",
            "{p} mentioned that they {f2}.",
            "Escalated because {p} {f} and needs flexible payment dates.",
            "Do not call {p} before noon; they {f2}.",
        )
        # "they is ..." reads badly; keep the third-person phrase for 0/2 only.
        sentence = templates[variant].format(p=person, f=phrase, f2=phrase)
        return sentence, [(person, PERSON), (term, SENSITIVE)]
    if entity == EntityType.PLATE:
        plate = f"{faker.random_int(1000, 9999)}{' ' if variant % 2 else '-'}" + _random_string(
            corpus, _PLATE_LETTERS, 3
        )
        templates = (
            "Courier van {v} delivered the package late.",
            "Parking fine for vehicle {v} disputed by the customer.",
            "Rental car {v} returned with a scratch.",
            "Insurance claim filed for car {v}.",
        )
        return templates[variant].format(v=plate), [(plate, EntityType.PLATE)]
    if entity == EntityType.GPS:
        lat = f"{faker.random_int(36, 43)}.{faker.random_int(100000, 999999)}"
        lon = f"-{faker.random_int(1, 8)}.{faker.random_int(100000, 999999)}"
        gps = f"{lat}, {lon}" if variant % 2 else f"{lat},{lon}"
        templates = (
            "The app reported the device location at {v}.",
            "Last known GPS position: {v}.",
            "Driver coordinates {v} logged at handover.",
            "Geo fix for the tracker: {v}.",
        )
        return templates[variant].format(v=gps), [(gps, EntityType.GPS)]
    if entity == ORG:
        org = faker.company()
        templates = (
            "Invoice disputed by {v} after the merger.",
            "{v} asked to add a second billing contact.",
            "Contract with {v} renews next quarter.",
            "Payment from {v} bounced twice.",
        )
        return templates[variant].format(v=org), [(org, ORG)]
    if entity == EntityType.SECRET:
        line, secret = _secret_line(corpus, index * 10)
        return f"Customer pasted a config snippet: {line}", [(secret, EntityType.SECRET)]
    if entity == EntityType.NIE:
        nie = faker.nie()
        return f"Foreign resident ID (NIE) {nie} attached to the claim.", [(nie, EntityType.NIE)]
    if entity == EntityType.DNI:
        dni = faker.nif()
        return f"Holder identified with DNI {dni} at the branch.", [(dni, EntityType.DNI)]
    if entity == EntityType.CIF:
        cif = _cif(corpus)
        return f"Company tax ID {cif} must appear on the invoice.", [(cif, EntityType.CIF)]
    if entity == EntityType.NSS:
        nss = _nss(corpus, index)
        return f"Payroll lists social security number {nss}.", [(nss, EntityType.NSS)]
    if entity == EntityType.CARD:
        card = faker.credit_card_number()
        return f"Refund goes back to card {card} within five days.", [(card, EntityType.CARD)]
    if entity == EntityType.PHONE:
        phone = corpus.phone(index)
        return f"Call back on {phone} after 6 pm.", [(phone, EntityType.PHONE)]
    if entity == EntityType.EMAIL:
        email = faker.free_email() if variant % 2 else faker.company_email()
        return f"Send the statement to {email} instead.", [(email, EntityType.EMAIL)]
    if entity == EntityType.MAC:
        mac = faker.mac_address()
        if variant % 2:
            mac = mac.replace(":", "-").upper()
        return f"Router {mac} keeps dropping the connection.", [(mac, EntityType.MAC)]
    raise ValueError(f"no note template for {entity}")  # pragma: no cover


def _build_notes(corpus: _Corpus, connection: sqlite3.Connection) -> None:
    """Insert ``crm_notes`` rows, four mixed-type sentences each, meeting ``NOTE_QUOTAS``."""
    queue: list[tuple[str, int]] = []
    remaining = dict(NOTE_QUOTAS)
    index = 0
    while any(remaining.values()):
        for entity in NOTE_QUOTAS:
            if remaining[entity]:
                queue.append((entity, index))
                remaining[entity] -= 1
                index += 1
    cursor = connection.cursor()
    for note_id, start in enumerate(range(0, len(queue), SENTENCES_PER_NOTE), start=1):
        sentences: list[str] = []
        notes: list[tuple[str, str]] = []
        for entity, i in queue[start : start + SENTENCES_PER_NOTE]:
            sentence, found = _note_sentence(corpus, entity, i)
            sentences.append(sentence)
            notes.extend(found)
        body = " ".join(sentences)
        author = corpus.faker.random_element(("agent", "supervisor", "billing", "fraud"))
        cursor.execute("INSERT INTO crm_notes VALUES (?, ?, ?)", (note_id, author, body))
        cursor_pos = 0
        for value, entity in notes:
            # Locate each value after the previous one so a repeated substring
            # (e.g. a short username inside a later sentence) gets its own offset.
            offset = body.find(value, cursor_pos)
            if offset < 0:
                raise ValueError(f"{entity} {value!r} is not present in crm_notes.body")
            corpus.note_at("crm_notes", "body", note_id, body, value, entity, offset)
            cursor_pos = offset + len(value)


def _build_decoys(corpus: _Corpus, connection: sqlite3.Connection) -> None:
    """Insert every decoy into its own table and record it as a known-negative.

    A dedicated `decoy_samples` table keeps decoy offsets independent of the
    real corpus's row content, so neither can perturb the other's offsets.
    """
    faker = corpus.faker
    cursor = connection.cursor()

    samples: list[tuple[str, str, str]] = []  # (category, note, decoy value)

    for dni in _bad_dni_letters(faker, 2):
        samples.append(
            (
                "DNI_BAD_LETTER",
                f"Applicant DNI: {dni} (verify before approving).",
                dni,
            )
        )
    for card in _bad_luhn_cards(faker, 2):
        samples.append(
            (
                "CARD_BAD_LUHN",
                f"Charge reference rejected, card {card} does not match.",
                card,
            )
        )
    for iban in _bad_mod97_ibans(faker, 2):
        samples.append(
            (
                "IBAN_BAD_MOD97",
                f"Provisional IBAN pending confirmation: {iban}.",
                iban,
            )
        )
    for number in _THOUSANDS_DECOYS:
        samples.append(
            (
                "THOUSANDS_SEPARATOR",
                f"The accumulated amount comes to {number} euros.",
                number,
            )
        )
    for version in _VERSION_DECOYS:
        samples.append(("VERSION_STRING", f"Client updated to version {version}.", version))
    for account in _ZERO_PADDED_DECOYS:
        samples.append(
            ("ZERO_PADDED_ACCOUNT", f"Reference account {account} has no activity.", account)
        )
    for ip in _INVALID_OCTET_IP_DECOYS:
        samples.append(("INVALID_OCTET_IP", f"Recorded source address: {ip}.", ip))
    for cif in _bad_cifs(corpus, 2):
        samples.append(("CIF_BAD_CONTROL", f"Supplier tax ID on file: {cif}.", cif))
    for nss in _bad_nss(corpus, 2):
        samples.append(("NSS_BAD_CONTROL", f"Employee social security number: {nss}.", nss))
    samples.append(("FLOAT_PAIR", "Model weights 3.14159, 2.71828 loaded.", "3.14159, 2.71828"))
    samples.append(("CLOCK_TIME", "Batch started at 12:30:45 and ended late.", "12:30:45"))
    samples.append(("CPP_SCOPE", "Crash inside std::vector::at during export.", "std::vector"))
    samples.append(("MASKED_PASSWORD", "Login failed, password=******** was rejected.", "********"))
    samples.append(("PLATE_WITH_VOWELS", "Parking spot 1234 AEI is reserved.", "1234 AEI"))

    # --- larger decoy set ------------------------------------------------
    for dni in _bad_dni_letters(faker, 16):
        samples.append(("DNI_BAD_LETTER", f"Applicant DNI {dni} rejected by the registry.", dni))
    for nie in _bad_nies(corpus, 16):
        samples.append(("NIE_BAD_LETTER", f"Resident card {nie} failed validation.", nie))
    for card in _bad_luhn_cards(faker, 16):
        samples.append(("CARD_BAD_LUHN", f"Gateway declined {card}: invalid number.", card))
    for iban in _bad_mod97_ibans(faker, 16):
        samples.append(("IBAN_BAD_MOD97", f"Transfer to {iban} bounced, check digits wrong.", iban))
    for cif in _bad_cifs(corpus, 16):
        samples.append(("CIF_BAD_CONTROL", f"Vendor registered as {cif} (typo suspected).", cif))
    for nss in _bad_nss(corpus, 16):
        samples.append(("NSS_BAD_CONTROL", f"Rejected SS number {nss} on the payslip.", nss))
    for ip in ("300.12.4.1", "10.256.0.1", "1.2.3.999", "256.256.256.256", "192.168.0.300"):
        samples.append(("INVALID_OCTET_IP", f"Malformed address {ip} in the access log.", ip))
    for version in ("2.14.1", "v3.0.12", "1.0.0-rc1", "4.18.2"):
        samples.append(("VERSION_STRING", f"Upgraded the SDK to {version} last night.", version))
    for clock in ("09:15:00", "23:59:59", "07:30:12", "18:45:03"):
        samples.append(("CLOCK_TIME", f"Cron job ran at {clock} without errors.", clock))
    for _ in range(6):
        uuid = corpus.faker.uuid4()
        samples.append(("UUID", f"Request {uuid} timed out upstream.", uuid))
    for _ in range(6):
        digest = corpus.faker.sha1()
        samples.append(("HEX_HASH", f"Deployed commit {digest} to production.", digest))
    for plate in ("4821 AEB", "9033 OUA", "1500 EIO", "7777 KIA"):
        samples.append(("PLATE_WITH_VOWELS", f"Warehouse bay {plate} is full.", plate))
    for masked in ("password=********", "api_key: <redacted>", "secret=xxxxxx", "token: [hidden]"):
        value = masked.split("=")[-1].split(": ")[-1]
        samples.append(("MASKED_SECRET", f"Config dump shows {masked} as expected.", value))
    for order in ("512345678", "412300987", "123456789", "300200100"):
        samples.append(("ORDER_NUMBER", f"Order {order} shipped from Zaragoza.", order))
    for pair in ("0.7512, 0.2488", "12.5000, 7.2500", "1.4142, 1.7320", "98.6000, 37.0000"):
        samples.append(("FLOAT_PAIR", f"Model scores {pair} were logged.", pair))
    for scope in ("os::path", "std::string", "Foo::bar", "boost::asio"):
        samples.append(("CPP_SCOPE", f"Stack frame in {scope} during shutdown.", scope))
    # Soft-layer decoys: proper nouns and phrases that are not personal data.
    # The regex layer never claims them; they measure LLM/encoder over-flagging.
    for product in (
        "Microsoft Teams",
        "Visual Studio Code",
        "PostgreSQL",
        "Claude Code",
        "Google Chrome",
        "Amazon Web Services",
        "Salesforce",
        "Jira",
        "Slack",
        "Kubernetes",
        "GitHub Actions",
        "Microsoft Excel",
        "Zoom",
        "Stripe Dashboard",
    ):
        samples.append(("SOFTWARE_NAME", f"The issue started after updating {product}.", product))
    for role in (
        "the account manager",
        "our support team",
        "the billing department",
        "the on-call engineer",
        "the legal team",
        "a senior developer",
    ):
        samples.append(("ROLE_PHRASE", f"Please forward this to {role} today.", role))
    for date in (
        "12/03/2024",
        "2026-01-15",
        "30/06/2025",
        "2025-11-02",
        "01/09/2026",
        "2024-12-31",
    ):
        samples.append(("NON_BIRTH_DATE", f"Invoice dated {date} is still unpaid.", date))
    for tech in ("health check", "memory leak", "disk failure", "cold start"):
        samples.append(("TECH_HEALTH", f"The server {tech} alert fired twice.", tech))

    for row_id, (category, note, value) in enumerate(samples, start=1):
        cursor.execute("INSERT INTO decoy_samples VALUES (?, ?, ?)", (row_id, category, note))
        corpus.note_decoy(
            "decoy_samples",
            "note",
            row_id,
            note,
            value,
            reason=f"{category}: deliberately invalid or non-PII value; must never be detected",
        )


# --------------------------------------------------------------------------
# The presentation's customer: one hand-written incident (an invoice charged
# twice, once to a cancelled direct-debit mandate) spread over every table, so
# the rows an investigation naturally reads -- the user, their company, its
# invoices, payments and tickets -- return a dozen kinds of personal data, not
# two or three. Fixed literals, no generator draws: the rest of the corpus is
# unaffected, and `docs/demo-script.md` can list the exact values to check.
# --------------------------------------------------------------------------

DEMO_EMAIL: Final = "lucia.ferrer@riberanorte.es"
DEMO_CUSTOMER_ID: Final = N_CUSTOMERS + 1
DEMO_INVOICE_ID: Final = N_INVOICES + 2  # the one charged twice
_DEMO_NAME: Final = "Lucía Ferrer Montoya"
_DEMO_COMPANY: Final = "Transportes Ribera Norte S.L."
_DEMO_CIF: Final = "B84712637"
_DEMO_DNI: Final = "47392816M"
_DEMO_PHONE: Final = "+34 612 384 905"
_DEMO_BIRTH: Final = "1986-03-14"
_DEMO_IBAN: Final = "ES5601822370420201593318"  # current mandate
_DEMO_OLD_IBAN: Final = "ES9820385778983000760236"  # cancelled, still charged
_DEMO_CARD: Final = "4532014876239153"
_DEMO_IP: Final = "88.12.45.201"
_DEMO_LANDLINE: Final = "983 214 776"
_DEMO_COLLEAGUE_EMAIL: Final = "jorge.alcaraz@riberanorte.es"
_DEMO_COLLEAGUE_NAME: Final = "Jorge Alcaraz Prieto"
_DEMO_COLLEAGUE_DNI: Final = "52618407L"
_DEMO_COLLEAGUE_IBAN: Final = "ES6100811532610001286377"


def _next_id(connection: sqlite3.Connection, table: str) -> int:
    query = f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}"  # noqa: S608
    return int(connection.execute(query).fetchone()[0])


def _insert_noted(
    corpus: _Corpus,
    connection: sqlite3.Connection,
    table: str,
    row: dict[str, Any],
    noted: dict[str, list[tuple[str, str]]],
) -> None:
    """Insert ``row`` into ``table`` and record each ``(value, type)`` per column."""
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})",  # noqa: S608
        tuple(row.values()),
    )
    for column, values in noted.items():
        text = str(row[column])
        for value, entity in values:
            corpus.note(table, column, int(row["id"]), text, value, entity)


def _build_demo_incident(corpus: _Corpus, connection: sqlite3.Connection) -> None:
    """Insert the presentation customer's rows across every table."""
    add = partial(_insert_noted, corpus, connection)
    customer = DEMO_CUSTOMER_ID
    add(
        "customers",
        {"id": customer, "name": _DEMO_COMPANY, "national_id": _DEMO_CIF, "city": "Valladolid"},
        {"name": [(_DEMO_COMPANY, ORG)], "national_id": [(_DEMO_CIF, EntityType.CIF)]},
    )
    users = (
        (
            N_USERS + 1,
            DEMO_EMAIL,
            _DEMO_NAME,
            _DEMO_PHONE,
            _DEMO_DNI,
            _DEMO_BIRTH,
            "Calle de Santiago 27, 3º B, 47001 Valladolid",
        ),
        (
            N_USERS + 2,
            _DEMO_COLLEAGUE_EMAIL,
            _DEMO_COLLEAGUE_NAME,
            "655 902 117",
            _DEMO_COLLEAGUE_DNI,
            "1979-11-02",
            "Paseo de Zorrilla 112, 47006 Valladolid",
        ),
    )
    for user_id, email, name, phone, dni, birth, address in users:
        add(
            "users",
            {
                "id": user_id,
                "customer_id": customer,
                "email": email,
                "full_name": name,
                "phone": phone,
                "national_id": dni,
                "date_of_birth": birth,
                "address": address,
            },
            {
                "email": [(email, EntityType.EMAIL)],
                "full_name": [(name, PERSON)],
                "phone": [(phone, EntityType.PHONE)],
                "national_id": [(dni, EntityType.DNI)],
                "date_of_birth": [(birth, DOB)],
                "address": [(address, ADDRESS)],
            },
        )

    first, charged_twice, latest = N_INVOICES + 1, DEMO_INVOICE_ID, N_INVOICES + 3
    invoices: tuple[tuple[int, float, str, list[tuple[str, str]]], ...] = (
        (first, 1240.5, "No issues.", []),
        (
            charged_twice,
            1875.0,
            f"Billing contact: {_DEMO_NAME}, {_DEMO_PHONE}. Customer reports a duplicate "
            "direct debit for this invoice.",
            [(_DEMO_NAME, PERSON), (_DEMO_PHONE, EntityType.PHONE)],
        ),
        (
            latest,
            1312.75,
            f"Billing contact: {_DEMO_LANDLINE}. Hold until the duplicate charge is refunded.",
            [(_DEMO_LANDLINE, EntityType.PHONE)],
        ),
    )
    for invoice_id, amount, notes, noted in invoices:
        add(
            "invoices",
            {"id": invoice_id, "customer_id": customer, "amount": amount, "notes": notes},
            {"notes": noted},
        )
    payment_id = _next_id(connection, "payments")
    payments = (
        (first, _DEMO_IBAN, "paid"),
        (charged_twice, _DEMO_IBAN, "paid"),
        (charged_twice, _DEMO_OLD_IBAN, "paid"),
        (latest, _DEMO_COLLEAGUE_IBAN, "pending"),
    )
    for offset, (invoice_id, iban, status) in enumerate(payments):
        add(
            "payments",
            {"id": payment_id + offset, "invoice_id": invoice_id, "iban": iban, "status": status},
            {"iban": [(iban, EntityType.IBAN)]},
        )

    ticket = (
        f"{_DEMO_NAME} (DNI {_DEMO_DNI}) wrote from {DEMO_EMAIL}: invoice {charged_twice} was "
        f"charged twice, once to {_DEMO_IBAN} and again to her old account {_DEMO_OLD_IBAN}, "
        f"which she cancelled last year. She also paid a late fee with card {_DEMO_CARD} by "
        f"mistake and wants it refunded too. Call back on {_DEMO_PHONE}. "
        f"Her colleague {_DEMO_COLLEAGUE_NAME} ({_DEMO_COLLEAGUE_EMAIL}) is copied."
    )
    add(
        "support_tickets",
        {
            "id": _next_id(connection, "support_tickets"),
            "customer_id": customer,
            "subject": "Invoice charged twice",
            "body": ticket,
        },
        {
            "body": [
                (_DEMO_NAME, PERSON),
                (_DEMO_DNI, EntityType.DNI),
                (DEMO_EMAIL, EntityType.EMAIL),
                (_DEMO_IBAN, EntityType.IBAN),
                (_DEMO_OLD_IBAN, EntityType.IBAN),
                (_DEMO_CARD, EntityType.CARD),
                (_DEMO_PHONE, EntityType.PHONE),
                (_DEMO_COLLEAGUE_NAME, PERSON),
                (_DEMO_COLLEAGUE_EMAIL, EntityType.EMAIL),
            ]
        },
    )

    log = (
        "Traceback (most recent call last):\n"
        '  File "billing/sepa.py", line 141, in collect\n'
        f"    raise DuplicateCollection(f'invoice {charged_twice} already paid by {DEMO_EMAIL}')\n"
        f"DuplicateCollection: mandate {_DEMO_OLD_IBAN} still active, request from {_DEMO_IP}"
    )
    add(
        "error_logs",
        {"id": _next_id(connection, "error_logs"), "level": "ERROR", "message": log},
        {
            "message": [
                (DEMO_EMAIL, EntityType.EMAIL),
                (_DEMO_OLD_IBAN, EntityType.IBAN),
                (_DEMO_IP, EntityType.IP),
            ]
        },
    )

    payload = json.dumps(
        {
            "event": "payment.collected",
            "data": {
                "customer_email": DEMO_EMAIL,
                "debtor_iban": _DEMO_OLD_IBAN,
                "invoice_id": charged_twice,
                "source_ip": _DEMO_IP,
            },
        },
        sort_keys=True,
    )
    add(
        "webhooks",
        {"id": _next_id(connection, "webhooks"), "event": "payment.collected", "payload": payload},
        {
            "payload": [
                (DEMO_EMAIL, EntityType.EMAIL),
                (_DEMO_OLD_IBAN, EntityType.IBAN),
                (_DEMO_IP, EntityType.IP),
            ]
        },
    )

    crm = (
        f"Call with {_DEMO_NAME} about the double charge on invoice {charged_twice}. She is "
        "on sick leave for depression and asked to be contacted by email only. Born 14/03/1986, "
        f"identity checked against DNI {_DEMO_DNI}."
    )
    add(
        "crm_notes",
        {"id": _next_id(connection, "crm_notes"), "author": "billing", "body": crm},
        {
            "body": [
                (_DEMO_NAME, PERSON),
                ("depression", SENSITIVE),
                ("14/03/1986", DOB),
                (_DEMO_DNI, EntityType.DNI),
            ]
        },
    )


def seed_demo(
    db_path: Path = DEFAULT_DB_PATH,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    decoys_path: Path = DEFAULT_DECOYS_PATH,
) -> list[Instance]:
    """Regenerate the demo database, manifest and decoy set from the fixed seed."""
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
        _build_notes(corpus, connection)
        _build_decoys(corpus, connection)
        _build_demo_incident(corpus, connection)
        connection.commit()

    decoy_payload: list[dict[str, Any]] = [asdict(decoy) for decoy in corpus.decoys]
    decoys_path.write_text(
        json.dumps(decoy_payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

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
    parser.add_argument("--decoys", type=Path, default=DEFAULT_DECOYS_PATH)
    args = parser.parse_args(argv)

    instances = seed_demo(args.db, args.manifest, args.decoys)
    by_type: dict[str, int] = {}
    for instance in instances:
        by_type[instance.entity_type] = by_type.get(instance.entity_type, 0) + 1
    print(f"seeded {args.db} — {len(instances)} PII instances in {args.manifest}")
    for entity_type in sorted(by_type):
        print(f"  {entity_type:<8} {by_type[entity_type]:>5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
