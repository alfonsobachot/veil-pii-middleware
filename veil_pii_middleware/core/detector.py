"""Hybrid detection: deterministic regex layer unioned with a local LLM layer.

Regex covers email, DNI/NIE, IBAN, Spanish phones, credit cards and IPs with
mandatory check-digit validation. The local model (via Ollama) covers person
names, addresses and indirect references, and must return spans as JSON --
never rewritten text. On overlap the longest span wins; ties go to regex.

This module implements the deterministic layer only. It is the guaranteed
floor: when the local model is down, returns malformed JSON, or silently skips
a row, every entity below is still detected.

Check-digit validation is mandatory rather than cosmetic. A bare nine-digit
run is an order number as often as a phone, and a sixteen-digit run is a
reference code as often as a card. Validating the control digit is what keeps
precision usable without lowering recall.

Separator policy, decided from Spanish notation rather than from convenience.
The dot is the thousands separator in Spanish, so any pattern that accepts it
as a digit separator lays claim to every nine-digit figure it meets. Patterns
that carry a check digit can afford that, because the control character
discards what the pattern over-claims: ``12.345.678-Z`` is a DNI, and the
letter proves it. Phone cannot -- it has no check digit, so nothing would
stand between ``912.345.678 euros`` and a token. Card is excluded too: Luhn
only discards nine candidates in ten, and cards are not written with dots in
practice, so allowing them would buy no recall.

That also settles four-group dotted numbers such as ``91.234.56.78``: with
the dot gone from the phone pattern, they are addresses, unambiguously.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "STRUCTURED_TYPES",
    "Detector",
    "EntityType",
    "Span",
    "detect_regex",
    "merge_spans",
    "valid_card",
    "valid_dni",
    "valid_iban",
    "valid_international_phone",
    "valid_ipv4",
    "valid_nie",
    "valid_spanish_phone",
]


class EntityType(StrEnum):
    """Entity kinds the deterministic layer can prove with a check digit."""

    EMAIL = "EMAIL"
    DNI = "DNI"
    NIE = "NIE"
    IBAN = "IBAN"
    PHONE = "PHONE"
    CARD = "CARD"
    IP = "IP"


class Detector(StrEnum):
    """Which layer produced a span. Recorded in the audit log, never dropped."""

    REGEX = "REGEX"
    LLM = "LLM"


#: Every type this layer emits. The recall gate holds these to 100%.
STRUCTURED_TYPES: Final = frozenset(EntityType)


@dataclass(frozen=True, slots=True)
class Span:
    """A detected value located by half-open offsets into the source text.

    Frozen and hashable so spans can key the content-block cache, and so a
    consumer cannot silently shift an offset after detection.
    """

    start: int
    end: int
    entity_type: EntityType
    text: str
    detector: Detector = Detector.REGEX

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError(f"span start must be non-negative, got {self.start}")
        if self.end <= self.start:
            raise ValueError(f"span end {self.end} must be greater than start {self.start}")
        if len(self.text) != self.end - self.start:
            raise ValueError(
                f"span text is {len(self.text)} chars but offsets cover {self.end - self.start}"
            )

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: Span) -> bool:
        """True when the two half-open ranges share at least one character."""
        return self.start < other.end and other.start < self.end


# --------------------------------------------------------------------------
# Check digits
# --------------------------------------------------------------------------

_DNI_LETTERS: Final = "TRWAGMYFPDXBNJZSQVHLCKE"
_NIE_PREFIX: Final = {"X": "0", "Y": "1", "Z": "2"}
# Validators strip dots as well as spaces and hyphens, because a value that
# reaches them may legitimately carry any of the three -- `12.345.678-Z`. Which
# entities are allowed to *propose* a dotted candidate is decided by the
# patterns further down, not here; see the separator policy in the docstring.
_SEPARATORS: Final = re.compile(r"[\s\-.]")


def _compact(value: str) -> str:
    """Drop the spaces, hyphens and dots humans sprinkle through a value."""
    return _SEPARATORS.sub("", value)


def _control_letter(number: int) -> str:
    return _DNI_LETTERS[number % 23]


def valid_dni(value: str) -> bool:
    """Eight digits plus the control letter indexed by ``number % 23``."""
    compact = _compact(value)
    if len(compact) != 9 or not compact[:8].isdigit():
        return False
    return compact[8].upper() == _control_letter(int(compact[:8]))


def valid_nie(value: str) -> bool:
    """X/Y/Z plus seven digits; the prefix folds to 0/1/2 before the DNI rule."""
    compact = _compact(value).upper()
    if len(compact) != 9 or compact[0] not in _NIE_PREFIX or not compact[1:8].isdigit():
        return False
    return compact[8] == _control_letter(int(_NIE_PREFIX[compact[0]] + compact[1:8]))


def valid_iban(value: str) -> bool:
    """ISO 13616 mod-97: move the first four chars to the end, expect 1."""
    compact = _compact(value).upper()
    if not 15 <= len(compact) <= 34:
        return False
    if not compact.isalnum() or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    rearranged = compact[4:] + compact[:4]
    # int(ch, 36) maps 0-9 to themselves and A-Z to 10-35, which is exactly the
    # letter expansion ISO 13616 prescribes.
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def valid_card(value: str) -> bool:
    """Luhn checksum over 12 to 19 digits.

    The floor is 12, not the more common 13: Maestro PANs are issued as short
    as 12 digits, and the corpus surfaced one. Missing a real card is a leak,
    while the extra false positives are only over-tokenized order numbers, so
    the trade is worth taking in this direction.
    """
    compact = _compact(value)
    if not compact.isdigit() or not 12 <= len(compact) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(compact)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def valid_ipv4(value: str) -> bool:
    """Four dot-separated decimal octets in 0-255.

    Zero-padded octets are accepted. RFC 3986 calls them ambiguous with octal
    and many parsers reject them, but log exporters emit ``192.168.001.001``
    and a value we refuse to recognise is a value we forward in clear.
    """
    parts = value.split(".")
    if len(parts) != 4:
        return False
    return all(part.isdigit() and len(part) <= 3 and int(part) <= 255 for part in parts)


def valid_spanish_phone(value: str) -> bool:
    """Nine digits starting 6, 7 or 9, with an optional +34 / 0034 prefix.

    Spanish numbers carry no check digit, so this is the only filter available
    and phones stay the highest false-positive entity in the layer.
    """
    compact = _compact(value)
    for prefix in ("+34", "0034"):
        if compact.startswith(prefix):
            compact = compact[len(prefix) :]
            break
    return len(compact) == 9 and compact.isdigit() and compact[0] in "679"


def valid_international_phone(value: str) -> bool:
    """A number in international form: any country code, not just Spain's.

    ``+`` is an unambiguous marker, so eight digits after it are enough. The
    legacy ``00`` form demands one more, because a value that merely starts
    ``00`` is just as likely to be a zero-padded account or reference number.
    Fifteen digits is the E.164 ceiling.
    """
    compact = _compact(value)
    if compact.startswith("+"):
        digits, floor = compact[1:], 8
    elif compact.startswith("00"):
        digits, floor = compact[2:], 9
    else:
        return False
    return digits.isdigit() and floor <= len(digits) <= 15


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

# Every pattern exposes a `value` group. Context needed only for anchoring --
# a trailing boundary, a surrounding quote -- stays outside it, so the emitted
# offsets cover the value itself and nothing more.
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+\-])(?P<value>[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*"
    r"\.[A-Za-z]{2,})"
)
# Dots are allowed here: `12.345.678-Z` is an everyday way to write a DNI, and
# the control letter is what keeps the looser pattern honest.
_DNI = re.compile(
    r"(?<![0-9A-Za-z])(?P<value>[0-9](?:[ \-.]?[0-9]){7}[ \-.]?[A-Za-z])(?![0-9A-Za-z])"
)
_NIE = re.compile(
    r"(?<![0-9A-Za-z])(?P<value>[XYZxyz][ \-.]?[0-9](?:[ \-.]?[0-9]){6}[ \-.]?[A-Za-z])"
    r"(?![0-9A-Za-z])"
)
_IBAN = re.compile(
    r"(?<![0-9A-Za-z])(?P<value>[A-Za-z]{2}[0-9]{2}(?:[ \-]?[0-9A-Za-z]{4}){3,7})(?![0-9A-Za-z])"
)
# Nine digits opening 6, 7 or 9, in ANY grouping. Fixing the grouping at 3-3-3
# missed `91 234 56 78`, which is simply how a Madrid landline is written.
_PHONE_ES = re.compile(r"(?<![0-9+])(?P<value>[679](?:[ \-]?[0-9]){8})(?![0-9])")
# Any country in international form, not a hard-coded +34.
_PHONE_INTL = re.compile(r"(?<![0-9+])(?P<value>(?:\+|00)(?:[ \-]?[0-9]){8,15})(?![0-9])")
_CARD = re.compile(r"(?<![0-9])(?P<value>[0-9](?:[ \-]?[0-9]){11,18})(?![0-9])")
# Octets are matched loosely and range-checked in `valid_ipv4`, so zero-padded
# forms survive. The trailing guard rejects a longer dotted number but must
# still allow the full stop that ends a sentence -- `(?![0-9.])` silently
# missed every IP written at the end of a line, which is where logs put them.
_IP = re.compile(r"(?<![0-9.])(?P<value>(?:[0-9]{1,3}\.){3}[0-9]{1,3})(?![0-9])(?!\.[0-9])")

_Validator = Callable[[str], bool]

_PATTERNS: Final[tuple[tuple[EntityType, re.Pattern[str], _Validator | None], ...]] = (
    (EntityType.EMAIL, _EMAIL, None),
    (EntityType.DNI, _DNI, valid_dni),
    (EntityType.NIE, _NIE, valid_nie),
    (EntityType.IBAN, _IBAN, valid_iban),
    # Two spellings of the same entity. `+34 612 345 678` matches both, and
    # merge_spans keeps the longer one, so the country code is not dropped.
    (EntityType.PHONE, _PHONE_ES, valid_spanish_phone),
    (EntityType.PHONE, _PHONE_INTL, valid_international_phone),
    (EntityType.CARD, _CARD, valid_card),
    (EntityType.IP, _IP, valid_ipv4),
)

_DETECTOR_RANK: Final = {Detector.REGEX: 0, Detector.LLM: 1}


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def merge_spans(spans: Iterable[Span]) -> list[Span]:
    """Drop overlaps: the longest span wins, and an exact tie goes to regex.

    Shared with the future LLM union stage, which resolves the same conflict
    between layers that the patterns already create among themselves -- an
    IBAN contains digit runs a card or phone pattern will happily claim.

    Returns the survivors ordered by start offset.
    """
    ordered = sorted(
        spans,
        key=lambda span: (-span.length, _DETECTOR_RANK[span.detector], span.start),
    )
    kept: list[Span] = []
    for span in ordered:
        if any(span.overlaps(other) for other in kept):
            continue
        kept.append(span)
    return sorted(kept, key=lambda span: span.start)


def detect_regex(text: str) -> list[Span]:
    """Find every structured entity in ``text``, offsets into ``text`` itself.

    Returns non-overlapping spans ordered by start offset. Values that fail
    their check digit are discarded, not downgraded.
    """
    found: list[Span] = []
    for entity_type, pattern, validator in _PATTERNS:
        for match in pattern.finditer(text):
            value = match.group("value")
            if validator is not None and not validator(value):
                continue
            start, end = match.span("value")
            found.append(
                Span(
                    start=start,
                    end=end,
                    entity_type=entity_type,
                    text=value,
                    detector=Detector.REGEX,
                )
            )
    return merge_spans(found)
