"""Hybrid detection: deterministic regex layer unioned with a local LLM layer.

Regex covers email, DNI/NIE, IBAN, Spanish phones, credit cards and IPs with
mandatory check-digit validation. The LLM layer, reached over an OpenAI-
compatible ``/v1/chat/completions`` endpoint (LM Studio, or anything else
serving that schema), is asked for every entity type this module knows about
-- not just names -- so recall can be compared per entity type per detector,
and must return spans as JSON, never rewritten text. On overlap the longest
span wins; ties go to regex.

The LLM layer is switched on only when a caller supplies a constructed
``LLMClient`` to ``detect()``; nothing in this module reads the environment
itself. ``VEIL_LLM_BASE_URL``/``VEIL_LLM_MODEL`` are the environment variables
callers are expected to read to build one -- the base URL is never hardcoded
to any host or port, since the model may run on a different, more powerful
machine on the same LAN rather than on ``localhost``.

Fail-closed floor: ``detect()`` catches any failure from the LLM leg --
connection refused, timeout, non-2xx, malformed JSON, an unexpected response
shape -- and falls back to the regex layer's spans alone. Every entity below
is still detected when the LLM endpoint is down, slow, or misbehaving.

Check-digit validation is mandatory rather than cosmetic. A bare nine-digit
run is an order number as often as a phone, and a sixteen-digit run is a
reference code as often as a card. Validating the control digit is what keeps
precision usable without lowering recall.

Separator policy: no pattern accepts the dot as a digit separator. In Spanish
the dot is the thousands separator, so a pattern that takes it lays claim to
every figure it meets -- ``912.345.678 euros`` was being read as a phone, and
a phone carries no check digit to catch it. National IDs and cards are not
written with dots often enough to be worth a looser pattern either, so a
dotted DNI is left to the LLM layer, which is already where a person's name
lives. The floor this module guarantees is partial by construction; that is
the trade, not an oversight.

That also settles four-group dotted numbers such as ``91.234.56.78``: they
are addresses, unambiguously.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import httpx

__all__ = [
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "LLM_BASE_URL_ENV_VAR",
    "LLM_MODEL_ENV_VAR",
    "STRUCTURED_TYPES",
    "Detector",
    "EntityType",
    "LLMClient",
    "LLMDetectionError",
    "Span",
    "clear_llm_cache",
    "detect",
    "detect_llm",
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
    """Every entity kind either layer can emit.

    ``EMAIL`` through ``IP`` are provable with a check digit by the
    deterministic layer; ``PERSON`` and ``ADDRESS`` cannot be, and are
    produced only by the LLM layer. An indirect reference to a person (e.g.
    "the son of the Acme administrator") is not a separate member -- it is
    still a ``PERSON`` match, just on the phrase that identifies them.
    """

    EMAIL = "EMAIL"
    DNI = "DNI"
    NIE = "NIE"
    IBAN = "IBAN"
    PHONE = "PHONE"
    CARD = "CARD"
    IP = "IP"
    PERSON = "PERSON"
    ADDRESS = "ADDRESS"
    #: A company / organization name (a customer's business, a counterparty).
    #: Like ``PERSON`` and ``ADDRESS`` it is produced only by the LLM layer.
    ORG = "ORG"


class Detector(StrEnum):
    """Which layer produced a span. Recorded in the audit log, never dropped."""

    REGEX = "REGEX"
    LLM = "LLM"


#: The seven check-digit-provable types the deterministic layer guarantees.
#: Deliberately *not* ``frozenset(EntityType)`` -- ``PERSON``/``ADDRESS`` are
#: real ``EntityType`` members but the regex layer can never produce them, so
#: the 100% recall gate (bound to this set) must not silently start expecting
#: it to.
STRUCTURED_TYPES: Final = frozenset(
    {
        EntityType.EMAIL,
        EntityType.DNI,
        EntityType.NIE,
        EntityType.IBAN,
        EntityType.PHONE,
        EntityType.CARD,
        EntityType.IP,
    }
)


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
# Spaces and hyphens only. The dot is deliberately absent so a direct caller
# cannot disagree with the detector: no pattern proposes a dotted candidate,
# so no validator should accept one. See the separator policy in the docstring.
_SEPARATORS: Final = re.compile(r"[\s\-]")


def _compact(value: str) -> str:
    """Drop the spaces and hyphens humans sprinkle through a value."""
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
_DNI = re.compile(r"(?<![0-9A-Za-z])(?P<value>[0-9]{8}[ \-]?[A-Za-z])(?![0-9A-Za-z])")
_NIE = re.compile(r"(?<![0-9A-Za-z])(?P<value>[XYZxyz][ \-]?[0-9]{7}[ \-]?[A-Za-z])(?![0-9A-Za-z])")
_IBAN = re.compile(
    r"(?<![0-9A-Za-z])(?P<value>[A-Za-z]{2}[0-9]{2}(?:[ \-]?[0-9A-Za-z]{4}){3,7})(?![0-9A-Za-z])"
)
# Nine digits opening 6, 7 or 9, in ANY grouping. Fixing the grouping at 3-3-3
# missed `91 234 56 78`, which is simply how a Madrid landline is written.
# The extra lookbehind rejects digits glued to the tail of a longer
# alphanumeric identifier (e.g. the `622755116` in a hex temp-dir name such as
# `9f483fe622755116`), which is an ID fragment, not a phone number. Four or
# more alphanumerics immediately before is the cut-off: `tel612345678` style
# typos are still caught, `tel:`/`tlf `/`+34 ` forms always are.
_PHONE_ES = re.compile(r"(?<![0-9+])(?<![A-Za-z0-9]{4})(?P<value>[679](?:[ \-]?[0-9]){8})(?![0-9])")
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


# --------------------------------------------------------------------------
# LLM layer
# --------------------------------------------------------------------------

#: Environment variable the base URL of an OpenAI-compatible chat-completions
#: endpoint is read from by callers building an `LLMClient` -- never a
#: hardcoded host or port. The model may run on a separate, more powerful
#: machine on the same LAN (LM Studio's use case for this project), not
#: necessarily `localhost`.
LLM_BASE_URL_ENV_VAR: Final = "VEIL_LLM_BASE_URL"

#: Environment variable the model name is read from. LM Studio (unlike
#: Ollama) has no usable default -- it must match whatever model the host
#: actually has loaded.
LLM_MODEL_ENV_VAR: Final = "VEIL_LLM_MODEL"

#: Single attempt, no retry: a debugging session must not stall on every
#: cache-miss block if the endpoint is genuinely down, but a LAN-local host
#: under light load should comfortably finish well inside this.
DEFAULT_LLM_TIMEOUT_SECONDS: Final = 10.0

_ENTITY_TYPE_NAMES: Final = tuple(entity_type.value for entity_type in EntityType)

#: Per-type structural/format rules, added 2026-09-08
#: after a live LM Studio verification measured 0% DNI
#: recall / 0.087 NIE precision: the model reliably found the right span but
#: mislabeled its entity type whenever two types are structurally close, with
#: no rule in the prompt to go on. Each line states the one distinguishing
#: fact a chat model needs -- not a full grammar -- to stop guessing between
#: near-identical shapes.
_STRUCTURAL_RULES: Final = (
    "Structural rules to tell similar-looking types apart:\n"
    "- DNI: exactly 8 digits followed by 1 control letter, with NO leading "
    'letter (e.g. "12345678Z").\n'
    "- NIE: starts with X, Y or Z, then 7 digits, then 1 control letter "
    '(e.g. "X1234567L"). The leading X/Y/Z is what makes it a NIE and not a '
    "DNI -- a DNI never starts with a letter.\n"
    "- IBAN: starts with 2 letters (a country code) followed by 2 digits, "
    'then up to 30 further alphanumeric characters (e.g. "ES91 2100 0418 '
    '4502 0005 1332").\n'
    "- PHONE: a plain run of digits (9 for a Spanish number, more for an "
    'international one with a "+" or "00" prefix), with no country-code '
    "letters and no control letter -- unlike DNI/NIE/IBAN, a phone number "
    'carries no letters at all (aside from an optional leading "+").\n'
    "- CARD: 12 to 19 digits only, no letters, usually longer than a phone "
    "number and grouped in 4s.\n"
    "- IP: exactly four dot-separated numbers, each between 0 and 255 "
    '(e.g. "192.168.1.15") -- never more or fewer than four groups, and '
    "never a number over 255 in any group."
)

_SYSTEM_PROMPT: Final = (
    "You are a PII span detector for a debugging tool. Given a block of "
    "text, find every instance of these entity types: "
    + ", ".join(_ENTITY_TYPE_NAMES)
    + ". "
    + _STRUCTURAL_RULES
    + " Respond with ONLY a JSON object of the exact shape "
    '{"spans": [{"value": "<exact substring copied from the text>", '
    '"entity_type": "<one of the listed types>"}, ...]} -- no prose, no '
    "markdown code fences, no explanation, nothing before or after the JSON. "
    'If nothing matches, respond with exactly {"spans": []}. Never rewrite, '
    "paraphrase, translate, summarize, or correct the text -- every `value` "
    "must be copied character-for-character exactly as it appears in the "
    "input, so it can be located by an exact substring match. Treat an "
    "indirect reference that identifies a specific person (for example "
    '"the son of the Acme administrator") as a PERSON match on the phrase '
    "that identifies them, not a separate category. A first-person "
    'self-introduction -- "Soy Alfonso", "Me llamo Ana Garcia", "My name is '
    'John Smith", "This is Maria speaking" -- names the speaker just as much '
    "as a third-person mention names someone else: match the name in these "
    "just as you would if someone else said it about them. Never skip a name "
    "merely because the person speaking is naming themselves. Match only the "
    'name itself, never a leading verb or introductory phrase -- in "Soy '
    'Alfonso" the match is "Alfonso", not "Soy Alfonso". When a person has '
    "multiple name parts (a given name plus one or more surnames), return "
    "them together as ONE span covering the full name, never one span per "
    'word -- in "Alfonso Garcia" the match is "Alfonso Garcia", not '
    '"Alfonso" and "Garcia" separately. '
    "ORG is the name of a company, business, association or other "
    'organization that is a customer or counterparty (for example "Acme '
    'Consulting S.L.", "Familia Aguado S.A."): match the full name including '
    "its legal suffix (S.L., S.A., S.Com., & Asociados...) as ONE span. Do "
    "NOT report as PERSON or ORG: software products, tools, programming "
    "languages, AI models or assistants (Claude, Claude Code, Anthropic...), "
    "technology vendors, or generic role words -- only organizations that "
    "identify a real-world customer or business relationship."
)


#: System prompt for ``LLMClient.confirm_person_candidates`` (added
#: 2026-09-09). Deliberately a *separate*, much smaller task from
#: ``_SYSTEM_PROMPT``'s full-block span extraction: live evidence
#: showed the main extraction call can correctly
#: find every DNI/IBAN/phone/card in a dense block while still missing a
#: person's name sitting in the same block, at temperature 0 -- so a retry
#: of the *same* request would deterministically reproduce the same miss.
#: Classifying a short, isolated fragment ("is this substring a person's
#: name, yes or no") is a much easier task than extracting spans from a
#: whole noisy document, and is only ever invoked for fragments a cheap
#: structural regex already flagged as name-shaped and the main call left
#: uncovered -- see ``_NAME_CANDIDATE_RE``/``_confirm_missed_person_candidates``.
_PERSON_CONFIRMATION_SYSTEM_PROMPT: Final = (
    "You will be given a numbered list of short text fragments pulled out of "
    "a larger document. For each fragment, decide whether it is a PERSON's "
    "full name -- a human being's given name plus one or more surnames -- as "
    "opposed to a company name, place name, product name, department, or any "
    "other proper noun that happens to be capitalized. Respond with ONLY a "
    'JSON object of the exact shape {"persons": [<the numbers of every '
    "fragment that IS a person's name>]} -- no prose, no markdown code "
    "fences, no explanation, nothing before or after the JSON. Numbers only, "
    "never the fragment text itself. If none of the fragments are a person's "
    'name, respond with exactly {"persons": []}.'
)


#: Proper nouns the LLM layer tends to flag as PERSON/ORG but that are never
#: customer data: Anthropic's own product and model names, which sit in the
#: boilerplate of every Claude Code request. Compared case-insensitively and
#: only against a span's *whole* text, so "Claude Rodriguez" is unaffected.
#: Kept deliberately narrow (Anthropic-controlled names only): a broader
#: vendor list would start hiding a real customer that happens to share a
#: name with a well-known company.
_NON_PII_NAMES: Final = frozenset(
    {
        "claude",
        "claude code",
        "claude agent sdk",
        "claude api",
        "claude desktop",
        "claude opus",
        "claude sonnet",
        "claude haiku",
        "anthropic",
        "opus",
        "sonnet",
        "haiku",
    }
)

#: Entity types ``_NON_PII_NAMES`` applies to.
_NAME_LIKE_TYPES: Final = frozenset({EntityType.PERSON, EntityType.ORG})


def _is_known_non_pii_name(text: str) -> bool:
    return " ".join(text.split()).casefold() in _NON_PII_NAMES


class LLMDetectionError(Exception):
    """Raised when the LLM layer's response cannot be trusted as spans.

    Covers a non-2xx status, a body that is not JSON, or JSON that does not
    match the expected ``{"spans": [...]}`` shape. Always caught by
    ``detect()``, which falls back to the regex layer's spans -- this
    exception should never reach a caller of ``detect()`` itself.
    """


class LLMClient:
    """Client for an OpenAI-compatible ``/v1/chat/completions`` endpoint.

    Written against the schema both Ollama and LM Studio serve, not any
    Ollama-native-only API -- this is what makes swapping hosts a config
    change, not a code change. ``base_url``/``model`` must be supplied by the
    caller (read from ``LLM_BASE_URL_ENV_VAR``/``LLM_MODEL_ENV_VAR`` at the
    edges that build one); this class never reads the environment or
    hardcodes a default host itself.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.Client(base_url=self._base_url, timeout=httpx.Timeout(timeout_seconds))

    @property
    def identity(self) -> str:
        """``base_url``/``model`` this client targets -- folded into the content-block
        cache key so two clients pointed at different hosts/models never share a
        cached result for the same text (see ``_cache_key``).
        """
        return f"{self._base_url}|{self._model}"

    def complete_spans(self, text: str) -> list[dict[str, Any]]:
        """POST one chat-completion request; return the raw parsed ``spans`` list.

        Deliberately does not set ``response_format`` -- live-tested
        (2026-09-08) against a real LM Studio instance running a
        llama.cpp-backed model: that server 400s on
        ``{"type": "json_object"}`` (it accepts only ``json_schema`` or
        ``text``), while the exact same request with no ``response_format``
        at all gets back precisely the requested ``{"spans": [...]}`` shape
        as a plain string in ``choices[0].message.content`` -- the system
        prompt's own instruction is sufficient, and omitting the field is
        more portable across OpenAI-compatible servers than depending on one
        server's structured-output dialect. Raises ``LLMDetectionError``
        (malformed response) or an ``httpx`` exception (connection/timeout/
        non-2xx) on any failure -- never falls back to anything itself.
        ``detect_llm`` validates and offsets each entry; callers further up
        (``detect``) catch every exception from this method and fail closed
        to the regex layer's spans.
        """
        response = self._client.post(
            "/v1/chat/completions",
            json={
                "model": self._model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Text:\n{text}"},
                ],
            },
        )
        response.raise_for_status()
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            spans = parsed["spans"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMDetectionError(f"unexpected response shape: {exc}") from exc
        if not isinstance(spans, list):
            raise LLMDetectionError(f"'spans' must be a list, got {type(spans).__name__}")
        return spans

    def confirm_person_candidates(self, candidates: list[str]) -> set[int]:
        """Ask which of ``candidates`` (1-indexed) is a person's full name.

        A separate, deliberately tiny classification call -- see
        ``_PERSON_CONFIRMATION_SYSTEM_PROMPT`` for why this is not just a
        retry of ``complete_spans``. Returns the set of 1-based indices into
        ``candidates`` the model confirmed as a person's name; never raises
        for an out-of-range or non-integer entry in the response, those are
        silently dropped, since a malformed *confirmation* is not a reason to
        fail the whole detection pass (callers already treat this step as
        best-effort). Raises ``LLMDetectionError`` (malformed response) or an
        ``httpx`` exception (connection/timeout/non-2xx) exactly like
        ``complete_spans`` on a failure that prevents reading the response at
        all -- callers must catch and skip the confirmation step, never
        propagate.
        """
        numbered = "\n".join(f"{i}. {candidate}" for i, candidate in enumerate(candidates, 1))
        response = self._client.post(
            "/v1/chat/completions",
            json={
                "model": self._model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _PERSON_CONFIRMATION_SYSTEM_PROMPT},
                    {"role": "user", "content": numbered},
                ],
            },
        )
        response.raise_for_status()
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            persons = parsed["persons"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMDetectionError(f"unexpected response shape: {exc}") from exc
        if not isinstance(persons, list):
            raise LLMDetectionError(f"'persons' must be a list, got {type(persons).__name__}")
        return {i for i in persons if isinstance(i, int) and 1 <= i <= len(candidates)}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


#: Content-block cache: content hash -> the spans `detect_llm` found for it
#: last time. Scoped to this process's lifetime -- the gateway's target use
#: case is one long-running process per debugging session, so a query result
#: that recurs across turns is analyzed by the LLM once, not every turn.
#: Never caches a failure, so a transient outage can still succeed next call.
_llm_span_cache: dict[str, list[Span]] = {}


def _cache_key(text: str, client_identity: str) -> str:
    return hashlib.sha256(f"{client_identity}|{text}".encode()).hexdigest()


def _locate_next(text: str, value: str, taken_starts: set[int]) -> int | None:
    """Find an occurrence of ``value`` in ``text`` not already in ``taken_starts``.

    The model returns matched values, not trustworthy offsets -- it cannot be
    trusted to count characters correctly -- so each value is located here
    instead. ``taken_starts`` must be scoped to this *same* value (see
    ``detect_llm``): two identical values in one response correctly claim
    two different occurrences of themselves rather than colliding on the
    first one, but two *different* values that happen to start at the same
    offset (e.g. "Calle Mayor" and "Calle Mayor 5") must never block each
    other -- ``merge_spans`` is what resolves that overlap, not this
    function. A value the model hallucinated (never appears verbatim)
    returns ``None`` and is skipped by the caller, never fabricated at a
    default offset.
    """
    start = text.find(value)
    while start != -1:
        if start not in taken_starts:
            return start
        start = text.find(value, start + 1)
    return None


def detect_llm(text: str, client: LLMClient) -> list[Span]:
    """Ask ``client`` for spans over ``text``, validated and offset into it.

    Raises ``LLMDetectionError`` or an ``httpx`` exception on any malformed
    or failed response -- callers must catch and fail closed (``detect``
    does this). Every raised message describes only the *shape* of the bad
    entry (its type, or which field was wrong) -- never its content, since a
    malformed entry's content can itself be the raw PII this whole module
    exists to keep out of any log. Results are cached by content hash (see
    ``_llm_span_cache``), so a repeated block within this process is not
    re-sent to the model.
    """
    key = _cache_key(text, client.identity)
    cached = _llm_span_cache.get(key)
    if cached is not None:
        return cached

    raw_spans = client.complete_spans(text)
    valid_types = {entity_type.value for entity_type in EntityType}
    # Scoped per distinct value, not shared across every value in the
    # response: two different values that happen to start at the same
    # offset (a plausible model output for overlapping PERSON/ADDRESS
    # granularities) must not block each other from being located --
    # merge_spans() is what resolves that overlap, once both candidates
    # exist.
    taken_starts_by_value: dict[str, set[int]] = {}
    found: list[Span] = []
    for item in raw_spans:
        if not isinstance(item, dict):
            raise LLMDetectionError(f"span entry is not an object (got {type(item).__name__})")
        value = item.get("value")
        entity_type_raw = item.get("entity_type")
        if not isinstance(value, str) or not value:
            raise LLMDetectionError(
                f"span entry's 'value' must be a non-empty string (got {type(value).__name__})"
            )
        if entity_type_raw not in valid_types:
            raise LLMDetectionError(
                f"span entry has an unrecognized entity_type (got {type(entity_type_raw).__name__})"
            )
        taken_starts = taken_starts_by_value.setdefault(value, set())
        start = _locate_next(text, value, taken_starts)
        if start is None:
            continue
        taken_starts.add(start)
        entity_type = EntityType(entity_type_raw)
        if entity_type in _NAME_LIKE_TYPES and _is_known_non_pii_name(value):
            continue
        found.append(
            Span(
                start=start,
                end=start + len(value),
                entity_type=entity_type,
                text=value,
                detector=Detector.LLM,
            )
        )
    _llm_span_cache[key] = found
    return found


#: Cheap structural heuristic for "this might be a person's full name":
#: 2 to 4 consecutive capitalized words. Has no idea what a name actually
#: is -- most of what it matches in real text is a company, place, or
#: department name, not a person (e.g. "Consultoria Guillen", "Calle
#: Mayor") -- so it must never mint a span on its own; it only decides what
#: is worth the one extra confirmation call ``_confirm_missed_person_candidates``
#: makes. See that function and ``_PERSON_CONFIRMATION_SYSTEM_PROMPT`` for
#: why a cheap-but-unreliable filter feeding an authoritative LLM check is
#: safe here in a way a regex that minted PERSON tokens directly would not
#: be.
_NAME_WORD: Final = r"[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü]+"
_NAME_CANDIDATE_RE: Final = re.compile(rf"\b{_NAME_WORD}(?:\s{_NAME_WORD}){{1,3}}\b")

#: Cache mirroring ``_llm_span_cache``, but for
#: ``_confirm_missed_person_candidates``'s own confirmed spans -- keyed the
#: same way (content hash + client identity) so a repeat ``detect()`` call
#: for text this process has already classified (e.g. the gateway's own
#: extra, scoped call for display-span offsets, right after the
#: substitution pass already ran) is a cache hit, not a second confirmation
#: round trip. Never caches a failure (see ``detect()``), same rationale as
#: ``_llm_span_cache``.
_person_confirmation_cache: dict[str, list[Span]] = {}


def _confirm_missed_person_candidates(
    text: str, spans: list[Span], client: LLMClient
) -> list[Span]:
    """Best-effort second pass for a person's name the main LLM call missed.

    Live evidence (2026-09-09) showed the main
    ``detect_llm`` call can correctly find every DNI/IBAN/phone/card in a
    dense block while still missing a person's name in that same block, at
    temperature 0 -- so simply retrying ``detect_llm`` would deterministically
    reproduce the same miss. This instead finds every ``_NAME_CANDIDATE_RE``
    match not already covered by a span the main call(s) already returned,
    and -- only if at least one such candidate exists -- makes one isolated
    classification call (``LLMClient.confirm_person_candidates``) asking
    which of those specific fragments is actually a person's name. Only a
    fragment that call confirms becomes a real ``PERSON`` span; the
    heuristic itself never does. Returns ``[]`` with no network call at all
    when every candidate is already covered, the common case.

    Any failure here (connection, timeout, malformed response) is the
    caller's (``detect()``'s) responsibility to catch -- this function
    raises rather than swallowing, so a caller can log and skip exactly like
    the main LLM leg does.
    """
    key = _cache_key(text, client.identity)
    cached = _person_confirmation_cache.get(key)
    if cached is not None:
        return cached

    occurrences = [
        (match.start(), match.end(), match.group(0))
        for match in _NAME_CANDIDATE_RE.finditer(text)
        if not any(match.start() < span.end and span.start < match.end() for span in spans)
        and not _is_known_non_pii_name(match.group(0))
    ]
    if not occurrences:
        _person_confirmation_cache[key] = []
        return []

    # Ask about each distinct fragment once, in first-seen order, even if it
    # recurs at several offsets -- one confirmation covers every occurrence.
    candidates: list[str] = []
    seen: set[str] = set()
    for _, _, value in occurrences:
        if value not in seen:
            seen.add(value)
            candidates.append(value)

    confirmed_indices = client.confirm_person_candidates(candidates)
    confirmed_values = {candidates[i - 1] for i in confirmed_indices}
    found = (
        [
            Span(
                start=start,
                end=end,
                entity_type=EntityType.PERSON,
                text=value,
                detector=Detector.LLM,
            )
            for start, end, value in occurrences
            if value in confirmed_values
        ]
        if confirmed_values
        else []
    )
    _person_confirmation_cache[key] = found
    return found


def clear_llm_cache() -> None:
    """Drop every cached LLM result. Exposed for tests and process-lifetime resets."""
    _llm_span_cache.clear()
    _person_confirmation_cache.clear()


def detect(
    text: str,
    *,
    use_regex: bool = True,
    use_llm: bool = True,
    llm_client: LLMClient | None = None,
) -> list[Span]:
    """Union the regex and LLM layers over ``text``, per the enabled flags.

    ``use_llm`` has no effect unless ``llm_client`` is also supplied -- this
    is the "switchable off" entry point: a caller with no client (e.g.
    ``VEIL_LLM_BASE_URL`` unset) gets exactly today's regex-only behavior,
    with no attempt at a network call. When a client is supplied, any
    failure on that leg -- connection refused, timeout, non-2xx, malformed
    JSON -- is caught here and logged (no PII/secret), falling back to
    whatever ``use_regex`` alone produced rather than propagating.

    When the main LLM call succeeds, a second, best-effort step
    (``_confirm_missed_person_candidates``) looks for a name-shaped fragment
    it missed and, only for that narrow case, makes one further isolated
    confirmation call before returning. A failure on that second step is
    caught and logged the same way as the main LLM leg, falling back to
    whatever was already found rather than propagating -- this step can only
    ever add spans, never remove ones the main call already returned.
    """
    spans: list[Span] = []
    if use_regex:
        spans.extend(detect_regex(text))
    if use_llm and llm_client is not None:
        try:
            spans.extend(detect_llm(text, llm_client))
        except Exception as exc:
            print(
                f"veil detector: LLM layer failed ({type(exc).__name__}: {exc}), "
                "falling back to regex-only spans for this block",
                file=sys.stderr,
            )
        else:
            try:
                spans.extend(_confirm_missed_person_candidates(text, spans, llm_client))
            except Exception as exc:
                print(
                    f"veil detector: person-candidate confirmation failed "
                    f"({type(exc).__name__}: {exc}), continuing without it",
                    file=sys.stderr,
                )
    return merge_spans(spans)
