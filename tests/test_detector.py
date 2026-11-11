"""Detection layer: check digits, offsets, overlap policy, and the LLM layer.

The corrupted-value cases matter more than the valid ones. A validator that
always returns ``True`` scores perfect recall against the manifest and is the
most dangerous failure this suite can catch.

The LLM-layer tests run against a real local HTTP server standing in for LM
Studio (no `httpx` transport mocking), matching this codebase's existing
"never assume, verify the real behavior" testing style for the gateway
adapter -- these tests prove `detect_llm`/`detect`'s own contract logic
(offset recovery, fail-closed, caching). A real LM Studio instance was also
live-tested against this code (2026-09-08), which
is what surfaced `test_request_omits_response_format` below.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from veil_pii_middleware.core.detector import (
    STRUCTURED_TYPES,
    Detector,
    EntityType,
    LLMClient,
    LLMDetectionError,
    Span,
    clear_llm_cache,
    detect,
    detect_llm,
    detect_regex,
    merge_spans,
    valid_card,
    valid_cif,
    valid_dni,
    valid_gps,
    valid_iban,
    valid_ipv6,
    valid_nie,
    valid_nss,
    valid_secret,
    valid_spanish_phone,
)

#: serve_forever() checks for shutdown at this interval; the 0.5 s default made
#: every server fixture take half a second to tear down.
_POLL_INTERVAL = 0.02

# Published, widely documented control values. If one of these fails, the
# implementation is wrong -- not the expectation.
VALID_DNI = "12345678Z"
VALID_NIE = "X1234567L"
VALID_IBAN = "ES9121000418450200051332"
VALID_CARD = "4111111111111111"
VALID_CIF = "B12345674"
VALID_NSS = "28/12345678/40"


# --------------------------------------------------------------------------
# Check digits: accept the valid value, reject the corrupted one
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("validator", "good", "bad"),
    [
        (valid_dni, VALID_DNI, "12345678A"),
        (valid_dni, "00000000T", "00000000X"),
        (valid_nie, VALID_NIE, "X1234567M"),
        (valid_nie, "Z0000000M", "Z0000000T"),
        (valid_iban, VALID_IBAN, "ES9121000418450200051333"),
        (valid_iban, "GB82WEST12345698765432", "GB82WEST12345698765433"),
        (valid_card, VALID_CARD, "4111111111111112"),
        (valid_card, "5500005555555559", "5500005555555558"),
    ],
)
def test_check_digit_accepts_valid_and_rejects_corrupted(
    validator: object, good: str, bad: str
) -> None:
    assert callable(validator)
    assert validator(good) is True
    assert validator(bad) is False


@pytest.mark.parametrize(
    "value",
    ["", "1234567Z", "123456789Z", "abcdefghZ", "12345678", "ES91", "not-an-iban"],
)
def test_check_digits_reject_malformed_input(value: str) -> None:
    assert valid_dni(value) is False
    assert valid_nie(value) is False
    assert valid_iban(value) is False
    assert valid_card(value) is False


def test_separators_are_ignored_by_validators() -> None:
    assert valid_dni("12345678-Z") is True
    assert valid_iban("ES91 2100 0418 4502 0005 1332") is True
    assert valid_card("4111 1111 1111 1111") is True


def test_spanish_phone_prefix_and_leading_digit() -> None:
    assert valid_spanish_phone("612345678") is True
    assert valid_spanish_phone("+34 612 345 678") is True
    assert valid_spanish_phone("0034912345678") is True
    # 8xx is a Spanish range, but not one this layer claims.
    assert valid_spanish_phone("812345678") is False
    assert valid_spanish_phone("61234567") is False


# --------------------------------------------------------------------------
# Detection: offsets, rejection, entity coverage
# --------------------------------------------------------------------------

CORPUS = (
    "Customer Customer1@Project.com opened the ticket. Their DNI is "
    f"{VALID_DNI} and the administrator's NIE is {VALID_NIE}. We charge "
    f"{VALID_IBAN} to card {VALID_CARD}. Company CIF {VALID_CIF}, NSS "
    f"{VALID_NSS}, plate 1234 BCD, MAC 00:1A:2B:3C:4D:5E, password=Hunter2Secret "
    "and GPS location 40.416775, -3.703790. Phone +34 612 345 678, "
    "server 192.168.1.15."
)


def test_every_span_offset_matches_its_text() -> None:
    for span in detect_regex(CORPUS):
        assert CORPUS[span.start : span.end] == span.text


def test_corpus_covers_every_structured_type() -> None:
    found = {span.entity_type for span in detect_regex(CORPUS)}
    assert found == STRUCTURED_TYPES


def test_structured_types_excludes_llm_only_entities() -> None:
    """PERSON/ADDRESS are real EntityType members but not regex-provable.

    STRUCTURED_TYPES must stay the fixed set the recall gate is bound to --
    not `frozenset(EntityType)`, which would silently pull these two in.
    """
    assert {
        EntityType.EMAIL,
        EntityType.DNI,
        EntityType.NIE,
        EntityType.IBAN,
        EntityType.PHONE,
        EntityType.CARD,
        EntityType.IP,
        EntityType.SECRET,
        EntityType.CIF,
        EntityType.NSS,
        EntityType.PLATE,
        EntityType.MAC,
        EntityType.GPS,
    } == STRUCTURED_TYPES
    assert EntityType.PERSON not in STRUCTURED_TYPES
    assert EntityType.ADDRESS not in STRUCTURED_TYPES


def test_spans_never_overlap() -> None:
    spans = detect_regex(CORPUS)
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier.end <= later.start


def test_spans_are_ordered_by_start_offset() -> None:
    spans = detect_regex(CORPUS)
    assert [span.start for span in spans] == sorted(span.start for span in spans)


def test_every_span_is_attributed_to_the_regex_layer() -> None:
    assert all(span.detector is Detector.REGEX for span in detect_regex(CORPUS))


@pytest.mark.parametrize(
    "text",
    [
        "The DNI 12345678A is not valid.",
        "The NIE X1234567M is not valid.",
        "The account ES9121000418450200051333 is not valid.",
        "The card 4111111111111112 is not valid.",
    ],
)
def test_failed_check_digit_yields_no_span(text: str) -> None:
    rejected = {EntityType.DNI, EntityType.NIE, EntityType.IBAN, EntityType.CARD}
    assert [span for span in detect_regex(text) if span.entity_type in rejected] == []


def test_plain_prose_yields_nothing() -> None:
    assert detect_regex("The order was delivered to the warehouse without incident.") == []


def test_version_string_is_not_an_ip() -> None:
    spans = detect_regex("Updated the client to version 1.2.3")
    assert [s for s in spans if s.entity_type is EntityType.IP] == []


@pytest.mark.parametrize(
    "text",
    [
        "Failure at 192.168.1.15.",
        "Failure at 192.168.1.15",
        "Failure at 192.168.1.15, retrying",
        "(192.168.1.15)",
    ],
)
def test_ip_is_found_regardless_of_trailing_punctuation(text: str) -> None:
    """Regression: a trailing full stop used to swallow the whole match.

    Log lines put addresses at the end of a sentence more often than not, so
    this was a silent miss in exactly the highest-volume case.
    """
    spans = [s for s in detect_regex(text) if s.entity_type is EntityType.IP]
    assert [s.text for s in spans] == ["192.168.1.15"]


@pytest.mark.parametrize(
    ("text", "ip"),
    [
        ("version 1.2.3 build 4.5.6.7", "4.5.6.7"),
        ("build 10.0.0.5", "10.0.0.5"),
    ],
)
def test_four_part_version_after_a_keyword_is_still_an_ip(text: str, ip: str) -> None:
    """Known, accepted false positive (decision 2 in docs/review-findings.md).

    A four-part build number is indistinguishable from an IPv4 address. A
    context filter on "version"/"build" would drop the first case but leak
    "build 10.0.0.5", which is a real address; over-tokenizing a version
    leaks nothing, so the filter is deliberately not applied.
    """
    spans = [s for s in detect_regex(text) if s.entity_type is EntityType.IP]
    assert [s.text for s in spans] == [ip]


def test_longer_dotted_number_is_not_read_as_an_ip() -> None:
    spans = detect_regex("build 192.168.1.15.7 internal")
    assert [s for s in spans if s.entity_type is EntityType.IP] == []


def test_zero_padded_octets_are_still_an_ip() -> None:
    """Log exporters emit them; refusing to recognise one forwards it in clear."""
    spans = [
        s for s in detect_regex("IP 192.168.001.001 detected") if s.entity_type is EntityType.IP
    ]
    assert [s.text for s in spans] == ["192.168.001.001"]


def test_octet_above_255_is_not_an_ip() -> None:
    assert [s for s in detect_regex("ref 300.400.500.600") if s.entity_type is EntityType.IP] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Phone 912345678", "912345678"),
        ("Phone 912 345 678", "912 345 678"),
        # 2-3-2-2 is simply how a Madrid landline is printed. Hard-coding the
        # 3-3-3 grouping made it invisible.
        ("Phone 91 234 56 78", "91 234 56 78"),
        ("Phone 912 34 56 78", "912 34 56 78"),
        ("Phone 91-234-56-78", "91-234-56-78"),
    ],
)
def test_phone_is_found_in_any_grouping(text: str, expected: str) -> None:
    spans = [s for s in detect_regex(text) if s.entity_type is EntityType.PHONE]
    assert [s.text for s in spans] == [expected]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Phone +34 612 345 678", "+34 612 345 678"),
        ("Phone +34 91 234 56 78", "+34 91 234 56 78"),
        # Any country, not a hard-coded +34.
        ("Phone +44 20 7946 0958", "+44 20 7946 0958"),
        ("Phone +1 415 555 2671", "+1 415 555 2671"),
        ("Phone 0034 612345678", "0034 612345678"),
    ],
)
def test_international_phone_keeps_its_country_code(text: str, expected: str) -> None:
    """The span must cover the prefix; dropping it would tokenize half a number."""
    spans = [s for s in detect_regex(text) if s.entity_type is EntityType.PHONE]
    assert [s.text for s in spans] == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "The amount comes to 912.345.678 euros",
        "Stock of 912.345.678 units",
        "Reference 612.345.678 of the order",
    ],
)
def test_spanish_thousands_separator_is_not_a_phone(text: str) -> None:
    """The dot is the thousands separator in Spanish.

    Accepting it as a phone separator claimed every nine-digit figure opening
    6, 7 or 9 — turning an invoice total into a token and breaking the very
    debugging workflow the product exists for. A phone has no check digit, so
    nothing downstream would have caught it.
    """
    assert [s for s in detect_regex(text) if s.entity_type is EntityType.PHONE] == []


def test_zero_padded_account_number_is_not_a_phone() -> None:
    """`00` alone is a weak signal; the legacy prefix needs one more digit."""
    assert detect_regex("Account 0012345678 at the bank") == []


def test_dot_separated_card_is_not_claimed() -> None:
    """Cards are not written with dots, and Luhn alone is a weak filter."""
    spans = [
        s for s in detect_regex("Card 4111.1111.1111.1111") if s.entity_type is EntityType.CARD
    ]
    assert spans == []


@pytest.mark.parametrize(
    ("text", "expected", "entity"),
    [
        ("DNI 12345678-Z", "12345678-Z", EntityType.DNI),
        ("DNI 12345678 Z", "12345678 Z", EntityType.DNI),
        ("DNI 12345678Z", "12345678Z", EntityType.DNI),
        ("NIE X1234567L", "X1234567L", EntityType.NIE),
        ("NIE X-1234567-L", "X-1234567-L", EntityType.NIE),
    ],
)
def test_national_id_is_found_with_spaces_or_hyphens(
    text: str, expected: str, entity: EntityType
) -> None:
    spans = [s for s in detect_regex(text) if s.entity_type is entity]
    assert [s.text for s in spans] == [expected]


@pytest.mark.parametrize("text", ["DNI 12.345.678-Z", "NIE X1.234.567-L"])
def test_dotted_national_id_is_left_to_the_llm_layer(text: str) -> None:
    """A deliberate gap, not an oversight.

    No pattern accepts the dot, because in Spanish it is the thousands
    separator. The dotted form is rare enough that the looser pattern is not
    worth it, so it falls to the probabilistic layer — the same place a
    person's name already lives. The floor this module guarantees is partial
    by construction.
    """
    rejected = {EntityType.DNI, EntityType.NIE}
    assert [s for s in detect_regex(text) if s.entity_type in rejected] == []


def test_validators_agree_with_the_patterns_about_dots() -> None:
    """A direct caller must not disagree with what the detector will accept."""
    assert valid_dni("12345678-Z") is True
    assert valid_dni("12.345.678-Z") is False


def test_dotted_address_is_not_claimed_as_a_card() -> None:
    """A four-group dotted number within 0-255 is an address, not a short PAN."""
    spans = detect_regex("Source 192.168.001.001 logged")
    assert [(s.entity_type, s.text) for s in spans] == [(EntityType.IP, "192.168.001.001")]


def test_iban_beats_the_digit_runs_inside_it() -> None:
    spans = detect_regex(f"Account {VALID_IBAN} confirmed")
    assert len(spans) == 1
    assert spans[0].entity_type is EntityType.IBAN
    assert spans[0].text == VALID_IBAN


# --------------------------------------------------------------------------
# merge_spans, exercised without the patterns
# --------------------------------------------------------------------------


def span(start: int, end: int, detector: Detector = Detector.REGEX) -> Span:
    return Span(
        start=start,
        end=end,
        entity_type=EntityType.EMAIL,
        text="x" * (end - start),
        detector=detector,
    )


def test_merge_keeps_disjoint_spans() -> None:
    assert merge_spans([span(0, 3), span(5, 9)]) == [span(0, 3), span(5, 9)]


def test_merge_prefers_the_longer_span() -> None:
    assert merge_spans([span(2, 5), span(0, 10)]) == [span(0, 10)]


def test_merge_trims_a_soft_span_that_overlaps_a_regex_span() -> None:
    llm = span(0, 4, Detector.LLM)
    regex = span(2, 6, Detector.REGEX)
    assert merge_spans([llm, regex]) == [span(0, 2, Detector.LLM), regex]


def test_merge_gives_regex_precedence_over_a_longer_soft_span() -> None:
    llm = span(0, 10, Detector.LLM)
    regex = span(6, 8, Detector.REGEX)
    assert merge_spans([llm, regex]) == [
        span(0, 6, Detector.LLM),
        regex,
        span(8, 10, Detector.LLM),
    ]


def _soft(text: str, value: str, entity_type: EntityType, detector: Detector) -> Span:
    start = text.index(value)
    return Span(start, start + len(value), entity_type, value, detector)


def _merged(text: str, *soft: Span) -> list[tuple[EntityType, str, Detector]]:
    spans = merge_spans([*detect_regex(text), *soft])
    assert all(text[s.start : s.end] == s.text for s in spans)
    return [(s.entity_type, s.text, s.detector) for s in spans]


def test_merge_keeps_the_name_part_of_a_person_span_over_a_phone() -> None:
    text = "call Juan 612 345 678"
    person = _soft(text, "Juan 612", EntityType.PERSON, Detector.LLM)

    assert _merged(text, person) == [
        (EntityType.PERSON, "Juan", Detector.LLM),
        (EntityType.PHONE, "612 345 678", Detector.REGEX),
    ]


def test_merge_keeps_the_uncovered_part_of_a_shorter_soft_span() -> None:
    text = "Ana Maria Lopez Garcia"
    org = _soft(text, "Ana Maria", EntityType.ORG, Detector.LLM)
    person = _soft(text, "Maria Lopez Garcia", EntityType.PERSON, Detector.LLM)

    assert _merged(text, org, person) == [
        (EntityType.ORG, "Ana", Detector.LLM),
        (EntityType.PERSON, "Maria Lopez Garcia", Detector.LLM),
    ]


def test_merge_splits_an_encoder_org_around_a_plate() -> None:
    text = "Courier van 5219 MDX"
    org = _soft(text, text, EntityType.ORG, Detector.ENCODER)

    assert _merged(text, org) == [
        (EntityType.ORG, "Courier van", Detector.ENCODER),
        (EntityType.PLATE, "5219 MDX", Detector.REGEX),
    ]


def test_merge_splits_an_llm_secret_around_a_regex_secret() -> None:
    text = "password=g7bHSF33PO52FL"
    llm_secret = _soft(text, text, EntityType.SECRET, Detector.LLM)

    assert _merged(text, llm_secret) == [
        (EntityType.SECRET, "password", Detector.LLM),
        (EntityType.SECRET, "g7bHSF33PO52FL", Detector.REGEX),
    ]


def test_merge_drops_a_soft_span_inside_a_regex_span() -> None:
    text = "mail maria.lopez@example.com"
    person = _soft(text, "maria.lopez", EntityType.PERSON, Detector.LLM)

    assert _merged(text, person) == [(EntityType.EMAIL, "maria.lopez@example.com", Detector.REGEX)]


def test_merge_splits_a_soft_span_around_a_regex_span_inside_it() -> None:
    text = "office at 10.0.0.5 Calle Mayor"
    address = _soft(text, "office at 10.0.0.5 Calle Mayor", EntityType.ADDRESS, Detector.LLM)

    assert _merged(text, address) == [
        (EntityType.ADDRESS, "office at", Detector.LLM),
        (EntityType.IP, "10.0.0.5", Detector.REGEX),
        (EntityType.ADDRESS, "Calle Mayor", Detector.LLM),
    ]


def test_merge_drops_leftover_punctuation_of_a_trimmed_span() -> None:
    text = "tel (612 345 678)"
    phone = _soft(text, "(612 345 678)", EntityType.PHONE, Detector.LLM)

    assert _merged(text, phone) == [(EntityType.PHONE, "612 345 678", Detector.REGEX)]


def test_merge_keeps_card_digits_a_greedy_phone_overlaps() -> None:
    text = "tel +34 612 345 678 4111 1111 1111 1111"

    spans = merge_spans(detect_regex(text))

    covered = {i for s in spans for i in range(s.start, s.end)}
    card_start = text.index("4111")
    card_digits = [i for i in range(card_start, len(text)) if text[i].isdigit()]
    assert all(i in covered for i in card_digits)


def test_merge_returns_spans_ordered_by_start() -> None:
    assert [s.start for s in merge_spans([span(20, 24), span(0, 4), span(10, 14)])] == [0, 10, 20]


def test_merge_is_idempotent() -> None:
    once = merge_spans(
        [
            span(0, 10),
            span(2, 5),
            span(12, 16),
            span(8, 14, Detector.LLM),
            span(15, 20, Detector.ENCODER),
        ]
    )
    assert merge_spans(once) == once


def test_adjacent_spans_do_not_count_as_overlapping() -> None:
    assert merge_spans([span(0, 4), span(4, 8)]) == [span(0, 4), span(4, 8)]


# --------------------------------------------------------------------------
# Span invariants
# --------------------------------------------------------------------------


def test_span_rejects_offsets_that_disagree_with_its_text() -> None:
    with pytest.raises(ValueError, match="offsets cover"):
        Span(start=0, end=5, entity_type=EntityType.EMAIL, text="abc")


def test_span_rejects_an_empty_range() -> None:
    with pytest.raises(ValueError, match="greater than start"):
        Span(start=3, end=3, entity_type=EntityType.EMAIL, text="")


def test_span_is_hashable_for_the_content_block_cache() -> None:
    assert len({span(0, 4), span(0, 4), span(5, 9)}) == 2


# --------------------------------------------------------------------------
# LLM layer: a real local HTTP server standing in for LM Studio
# --------------------------------------------------------------------------


class _FakeLLMState:
    """Configurable response for the fake LM Studio-shaped server below."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        #: The chat-completion "content" field -- a JSON *string*, exactly
        #: like a real model's text output, not a JSON object.
        self.completion_content: str = json.dumps({"spans": []})
        #: When set, overrides completion_content entirely and is written as
        #: the raw response body -- for shapes no valid completion_content
        #: could ever produce (e.g. a body with no "choices" key at all).
        self.raw_body: bytes | None = None
        self.delay_seconds: float = 0.0


def _make_llm_handler(state: _FakeLLMState) -> type[BaseHTTPRequestHandler]:
    class FakeLLMHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            state.requests.append(json.loads(body))
            if state.delay_seconds:
                time.sleep(state.delay_seconds)
            if state.raw_body is not None:
                payload = state.raw_body
            else:
                payload = json.dumps(
                    {"choices": [{"message": {"content": state.completion_content}}]}
                ).encode()
            try:
                self.send_response(state.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                # The client (a timeout test) has already given up and closed
                # its socket by the time a deliberately delayed response is
                # ready to write -- expected in that scenario, not a bug.
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    return FakeLLMHandler


def _closed_port_url() -> str:
    """A ``http://127.0.0.1:<port>`` with nothing listening -- guaranteed connection-refused."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Suppresses the traceback ``socketserver`` prints for an ``OSError``.

    The timeout tests deliberately have the client give up and close its
    socket while this server is still mid-response (or waiting to read the
    next keep-alive request); the resulting connection-reset/broken-pipe on
    this side is expected, not a bug, and would otherwise spam the test log.
    """

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


@pytest.fixture
def fake_llm() -> Iterator[tuple[_FakeLLMState, str]]:
    state = _FakeLLMState()
    server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _make_llm_handler(state))
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": _POLL_INTERVAL}, daemon=True
    )
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state, base_url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _reset_llm_cache() -> Iterator[None]:
    clear_llm_cache()
    yield
    clear_llm_cache()


class TestDetectLLM:
    def test_happy_path_finds_person_and_address(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        text = "Contact Ana Garcia at Calle Mayor 5, Madrid."
        state.completion_content = json.dumps(
            {
                "spans": [
                    {"value": "Ana Garcia", "entity_type": "PERSON"},
                    {"value": "Calle Mayor 5", "entity_type": "ADDRESS"},
                ]
            }
        )
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect_llm(text, client)

        found = {(s.entity_type, s.text) for s in spans}
        assert (EntityType.PERSON, "Ana Garcia") in found
        assert (EntityType.ADDRESS, "Calle Mayor 5") in found
        assert all(s.detector is Detector.LLM for s in spans)
        for s in spans:
            assert text[s.start : s.end] == s.text

    def test_prompt_requests_every_entity_type(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            detect_llm("hello", client)

        system_prompt = str(state.requests[0]["messages"][0]["content"])
        for entity_type in EntityType:
            assert entity_type.value in system_prompt

    def test_prompt_states_dni_vs_nie_leading_letter_rule(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        """A live LM Studio run measured 0% DNI

        recall / 0.087 NIE precision because the prompt gave no structural
        rule to tell the two apart. The fix adds that rule to the prompt --
        this only proves the rule is present in the text sent to the model,
        not that a real model now applies it correctly (that was checked
        separately by a live before/after comparison).
        """
        state, base_url = fake_llm
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            detect_llm("hello", client)

        system_prompt = str(state.requests[0]["messages"][0]["content"]).upper()
        assert "NO LEADING" in system_prompt or "NEVER STARTS WITH A LETTER" in system_prompt
        assert "X, Y OR Z" in system_prompt

    def test_prompt_states_iban_phone_card_ip_structural_rules(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            detect_llm("hello", client)

        system_prompt = str(state.requests[0]["messages"][0]["content"])
        assert "0 and 255" in system_prompt
        assert "12 to 19 digits" in system_prompt

    def test_request_omits_response_format(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        """Regression: a real LM Studio instance 400s on response_format=json_object.

        Live-tested 2026-09-08 against a real LM Studio/llama.cpp-backed
        model: that server only accepts `response_format.type` of
        `json_schema` or `text`, rejecting `json_object` outright, while the
        exact same request with the field omitted entirely returns the
        correct spans JSON. Must never be reintroduced.
        """
        state, base_url = fake_llm
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            detect_llm("hello", client)

        assert "response_format" not in state.requests[0]

    def test_duplicate_value_claims_separate_occurrences(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        text = "Ana Garcia called. Then Ana Garcia hung up."
        state.completion_content = json.dumps(
            {
                "spans": [
                    {"value": "Ana Garcia", "entity_type": "PERSON"},
                    {"value": "Ana Garcia", "entity_type": "PERSON"},
                ]
            }
        )
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect_llm(text, client)

        starts = sorted(s.start for s in spans)
        assert starts == [text.index("Ana Garcia"), text.rindex("Ana Garcia")]

    def test_hallucinated_value_is_skipped_not_fabricated(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        state.completion_content = json.dumps(
            {"spans": [{"value": "Name That Does Not Exist", "entity_type": "PERSON"}]}
        )
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect_llm("text without that name", client)

        assert spans == []

    def test_unknown_entity_type_is_kept_as_other(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        """An invented label still marks an identifying value: keep it, don't fail the block."""
        state, base_url = fake_llm
        state.completion_content = json.dumps({"spans": [{"value": "x", "entity_type": "BOGUS"}]})
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect_llm("x appears here", client)

        assert [(s.entity_type, s.text) for s in spans] == [(EntityType.OTHER, "x")]

    def test_non_string_entity_type_entry_is_skipped(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        state.completion_content = json.dumps(
            {
                "spans": [
                    {"value": "x", "entity_type": 7},
                    {"value": "Ana Garcia", "entity_type": "PERSON"},
                ]
            }
        )
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect_llm("x and Ana Garcia", client)

        assert [(s.entity_type, s.text) for s in spans] == [(EntityType.PERSON, "Ana Garcia")]

    def test_structured_span_failing_its_check_digit_is_dropped(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        """The model accepts invalid control digits; the regex validator has the last word."""
        state, base_url = fake_llm
        state.completion_content = json.dumps(
            {"spans": [{"value": "12345678A", "entity_type": "DNI"}]}
        )
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            assert detect_llm("DNI 12345678A on file", client) == []

    def test_flat_string_span_entry_is_skipped_without_leaking_its_content(
        self, fake_llm: tuple[_FakeLLMState, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A model returning `{"spans": ["value", ...]}` instead of objects is a
        realistic instruction-following failure. The bad entry is skipped, the
        good ones are kept, and its raw string -- possibly the exact PII value
        -- never reaches stderr.
        """
        state, base_url = fake_llm
        secret = "customer-secret@example.com"
        state.completion_content = json.dumps(
            {"spans": [secret, {"value": "Ana Garcia", "entity_type": "PERSON"}]}
        )
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect_llm(f"email {secret} for Ana Garcia", client)

        assert [s.text for s in spans] == ["Ana Garcia"]
        captured = capsys.readouterr()
        assert secret not in captured.err + captured.out

    @pytest.mark.parametrize("bad_value", [12345, "", None])
    def test_bad_value_entry_is_skipped_without_leaking_other_fields(
        self,
        fake_llm: tuple[_FakeLLMState, str],
        capsys: pytest.CaptureFixture[str],
        bad_value: object,
    ) -> None:
        """An entry with a bad 'value' is skipped and must not echo what else it carries."""
        state, base_url = fake_llm
        secret = "other-secret@example.com"
        state.completion_content = json.dumps(
            {
                "spans": [
                    {"value": bad_value, "entity_type": "PERSON", "note": secret},
                    {"value": "Ana Garcia", "entity_type": None},
                    {"value": "Calle Mayor 5", "entity_type": "ADDRESS"},
                ]
            }
        )
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect_llm(f"email {secret}, Ana Garcia, Calle Mayor 5", client)

        assert [(s.entity_type, s.text) for s in spans] == [(EntityType.ADDRESS, "Calle Mayor 5")]
        captured = capsys.readouterr()
        assert secret not in captured.err + captured.out

    @pytest.mark.parametrize(
        "reply",
        [
            '<think>return {"spans": []} if none</think>'
            '{"spans": [{"value": "Ana Garcia", "entity_type": "PERSON"}]}',
            'Here: {"spans": [{"value": "Ana Garcia", "entity_type": "PERSON"}]} Note: {x}',
            '```json\n{"spans": [{"value": "Ana Garcia", "entity_type": "PERSON"}]}\n```',
        ],
    )
    def test_chatty_reply_still_yields_its_spans(
        self, fake_llm: tuple[_FakeLLMState, str], reply: str
    ) -> None:
        state, base_url = fake_llm
        state.completion_content = reply
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect_llm("call Ana Garcia", client)

        assert [(s.entity_type, s.text) for s in spans] == [(EntityType.PERSON, "Ana Garcia")]

    def test_different_values_sharing_a_start_offset_are_both_kept(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        """Two different values overlapping at the same start must not block each other.

        A model commonly returns overlapping granularities of the same mention
        (e.g. a short and a longer form of the same address); ``merge_spans``
        (exercised via ``detect()``, not here) is what should resolve the
        overlap -- ``detect_llm`` itself must locate and return both candidates.
        """
        state, base_url = fake_llm
        text = "Lives at Calle Mayor 5, Madrid."
        state.completion_content = json.dumps(
            {
                "spans": [
                    {"value": "Calle Mayor", "entity_type": "ADDRESS"},
                    {"value": "Calle Mayor 5", "entity_type": "ADDRESS"},
                ]
            }
        )
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect_llm(text, client)

        found = {s.text for s in spans}
        assert found == {"Calle Mayor", "Calle Mayor 5"}

    def test_non_json_completion_content_raises(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        state.completion_content = "not json at all"
        with (
            LLMClient(base_url, "test-model", timeout_seconds=2.0) as client,
            pytest.raises(LLMDetectionError),
        ):
            detect_llm("text", client)

    def test_missing_choices_key_raises(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        state.raw_body = b'{"unexpected": true}'
        with (
            LLMClient(base_url, "test-model", timeout_seconds=2.0) as client,
            pytest.raises(LLMDetectionError),
        ):
            detect_llm("text", client)

    def test_spans_not_a_list_raises(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        state.completion_content = json.dumps({"spans": "not-a-list"})
        with (
            LLMClient(base_url, "test-model", timeout_seconds=2.0) as client,
            pytest.raises(LLMDetectionError),
        ):
            detect_llm("text", client)

    def test_non_2xx_status_raises(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        state.status = 500
        with (
            LLMClient(base_url, "test-model", timeout_seconds=2.0) as client,
            pytest.raises(httpx.HTTPStatusError),
        ):
            detect_llm("text", client)

    def test_dead_endpoint_raises(self) -> None:
        with (
            LLMClient(_closed_port_url(), "test-model", timeout_seconds=2.0) as client,
            pytest.raises(httpx.HTTPError),
        ):
            detect_llm("text", client)

    def test_slow_endpoint_times_out(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        state.delay_seconds = 0.5
        with (
            LLMClient(base_url, "test-model", timeout_seconds=0.1) as client,
            pytest.raises(httpx.TimeoutException),
        ):
            detect_llm("text", client)

    def test_repeated_call_with_same_text_hits_the_cache(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            detect_llm("same text twice", client)
            detect_llm("same text twice", client)

        assert len(state.requests) == 1

    def test_different_text_misses_the_cache(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            detect_llm("text one", client)
            detect_llm("text two", client)

        assert len(state.requests) == 2

    def test_failed_call_is_not_cached_so_a_later_call_can_recover(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        state.status = 500
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            with pytest.raises(httpx.HTTPStatusError):
                detect_llm("recoverable text", client)
            state.status = 200
            spans = detect_llm("recoverable text", client)

        assert spans == []
        # The failing call is retried once on a 5xx (Ollama reloading a model),
        # then the recovered call goes through: 2 + 1 requests.
        assert len(state.requests) == 3


class TestDetectUnion:
    def test_no_client_means_llm_off_no_network_attempted(self) -> None:
        # "Ana Garcia" alone is not provable by any regex pattern; with no
        # client, detect() must not attempt a call at all (nothing to
        # connect to would otherwise be defined) and simply returns nothing.
        assert detect("Ana Garcia", llm_client=None) == []

    def test_regex_only_flag_ignores_a_supplied_client(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            detect(f"DNI {VALID_DNI}", use_llm=False, llm_client=client)

        assert state.requests == []

    def test_union_combines_regex_and_llm_spans(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        text = f"Ana Garcia, DNI {VALID_DNI}"
        state.completion_content = json.dumps(
            {"spans": [{"value": "Ana Garcia", "entity_type": "PERSON"}]}
        )
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect(text, llm_client=client)

        kinds = {(s.entity_type, s.detector) for s in spans}
        assert (EntityType.PERSON, Detector.LLM) in kinds
        assert (EntityType.DNI, Detector.REGEX) in kinds

    def test_fail_closed_on_dead_endpoint_still_returns_regex_spans(self) -> None:
        text = f"DNI {VALID_DNI} and email customer@example.com"
        with LLMClient(_closed_port_url(), "test-model", timeout_seconds=1.0) as client:
            spans = detect(text, llm_client=client)

        kinds = {s.entity_type for s in spans}
        assert EntityType.DNI in kinds
        assert EntityType.EMAIL in kinds

    def test_fail_closed_on_timeout_still_returns_regex_spans(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        state.delay_seconds = 0.5
        text = f"DNI {VALID_DNI}"
        with LLMClient(base_url, "test-model", timeout_seconds=0.1) as client:
            spans = detect(text, llm_client=client)

        assert {s.entity_type for s in spans} == {EntityType.DNI}

    def test_fail_closed_on_malformed_json_still_returns_regex_spans(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        state.completion_content = "not json"
        text = f"DNI {VALID_DNI}"
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect(text, llm_client=client)

        assert {s.entity_type for s in spans} == {EntityType.DNI}

    def test_fail_closed_on_non_2xx_still_returns_regex_spans(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        state.status = 500
        text = f"DNI {VALID_DNI}"
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect(text, llm_client=client)

        assert {s.entity_type for s in spans} == {EntityType.DNI}

    def test_fail_closed_path_logs_no_pii(self, capsys: pytest.CaptureFixture[str]) -> None:
        secret_email = "customer-secret@example.com"
        with LLMClient(_closed_port_url(), "test-model", timeout_seconds=1.0) as client:
            detect(f"email {secret_email}", llm_client=client)

        captured = capsys.readouterr()
        assert secret_email not in captured.err
        assert secret_email not in captured.out


# --- phone glued to an identifier, ORG, and non-PII proper nouns -------------


@pytest.mark.parametrize(
    "text",
    [
        r"C:\Temp\claude\9f483fe622755116\0bb05033",
        "session id abcd622755116 resumed",
    ],
)
def test_phone_digits_at_the_tail_of_an_identifier_are_not_a_phone(text: str) -> None:
    """A hex temp-dir name ended in nine digits opening 6 and was minted as a PHONE."""
    assert [s for s in detect_regex(text) if s.entity_type is EntityType.PHONE] == []


@pytest.mark.parametrize(
    "text",
    ["Tel: 622755116", "tlf 622755116", "(622755116)", "tel:622755116", "+34 622 755 116"],
)
def test_phone_with_a_normal_separator_is_still_found(text: str) -> None:
    assert [s for s in detect_regex(text) if s.entity_type is EntityType.PHONE]


class _StubLLM(LLMClient):
    """LLMClient whose model reply is canned; never opens a connection."""

    def __init__(self, spans: list[dict[str, Any]]) -> None:
        super().__init__("http://stub.invalid", "stub-model")
        self._canned = spans

    def complete_spans(self, text: str) -> list[dict[str, Any]]:
        return self._canned


def test_llm_org_span_is_returned_as_org() -> None:
    clear_llm_cache()
    text = "Company: Familia Aguado S.A. (customer)"
    client = _StubLLM([{"value": "Familia Aguado S.A.", "entity_type": "ORG"}])

    spans = detect_llm(text, client)

    assert [(s.entity_type, s.text) for s in spans] == [(EntityType.ORG, "Familia Aguado S.A.")]
    assert EntityType.ORG not in STRUCTURED_TYPES


class _RecordingLLM(_StubLLM):
    """``_StubLLM`` that also records the text the model was sent."""

    def __init__(self, spans: list[dict[str, Any]]) -> None:
        super().__init__(spans)
        self.sent: list[str] = []

    def complete_spans(self, text: str) -> list[dict[str, Any]]:
        self.sent.append(text)
        return super().complete_spans(text)


def test_vault_tokens_are_masked_before_the_model_sees_the_block() -> None:
    """Tokens already in a block made qwen3 4b return no spans at all for it."""
    clear_llm_cache()
    text = "{'email': 'PII_EMAIL_0001', 'date_of_birth': '1986-03-14', 'phone': 'PII_PHONE_0002'}"
    client = _RecordingLLM(
        [
            {"value": "1986-03-14", "entity_type": "DOB"},
            {"value": "[redacted]", "entity_type": "EMAIL"},
        ]
    )

    spans = detect_llm(text, client)

    assert client.sent == [
        "{'email': '[redacted]', 'date_of_birth': '1986-03-14', 'phone': '[redacted]'}"
    ]
    assert [(s.entity_type, s.text, text[s.start : s.end]) for s in spans] == [
        (EntityType.DOB, "1986-03-14", "1986-03-14")
    ]


def test_crlf_is_sent_to_the_model_as_lf_and_offsets_stay_in_the_original() -> None:
    """A CRLF query result made qwen3 4b miss a company it found with LF."""
    clear_llm_cache()
    text = "Exit code 1\r\n(21, 'Transportes Ribera Norte S.L.', 'Valladolid')\r\n"
    client = _RecordingLLM([{"value": "Transportes Ribera Norte S.L.", "entity_type": "ORG"}])

    spans = detect_llm(text, client)

    assert client.sent == [text.replace("\r\n", "\n")]
    assert [text[s.start : s.end] for s in spans] == ["Transportes Ribera Norte S.L."]


@pytest.mark.parametrize("name", ["Claude Code", "claude code", "Anthropic", "Claude"])
@pytest.mark.parametrize("entity_type", ["PERSON", "ORG"])
def test_known_non_pii_names_are_dropped_from_llm_spans(name: str, entity_type: str) -> None:
    clear_llm_cache()
    client = _StubLLM([{"value": name, "entity_type": entity_type}])

    assert detect_llm(f"You are {name}, a coding assistant.", client) == []


def test_non_pii_name_filter_only_matches_the_whole_span() -> None:
    clear_llm_cache()
    client = _StubLLM([{"value": "Claude Rodriguez", "entity_type": "PERSON"}])

    spans = detect_llm("Customer Claude Rodriguez", client)

    assert [s.text for s in spans] == ["Claude Rodriguez"]


# --- rule-based additions: SECRET, CIF, NSS, PLATE, MAC, GPS, IPv6 ------------

_GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("validator", "good", "bad"),
    [
        (valid_cif, VALID_CIF, "B12345675"),
        (valid_nss, VALID_NSS, "28/12345678/41"),
        (valid_ipv6, "2001:db8::8a2e:370:7334", "12:30:45"),
        (valid_gps, "40.416775, -3.703790", "95.000000, -3.703790"),
        (valid_secret, "S3cr3tPass", "********"),
    ],
)
def test_new_validators_accept_valid_and_reject_corrupted(
    validator: object, good: str, bad: str
) -> None:
    assert callable(validator)
    assert validator(good) is True
    assert validator(bad) is False


@pytest.mark.parametrize(
    ("text", "expected", "entity"),
    [
        (
            "Deploy failed: AWS key AKIAIOSFODNN7EXAMPLE was rejected.",
            "AKIAIOSFODNN7EXAMPLE",
            EntityType.SECRET,
        ),
        (f"CI leaked {_GITHUB_TOKEN} in the build log", _GITHUB_TOKEN, EntityType.SECRET),
        (f"Request header carried {_JWT} before the 401", _JWT, EntityType.SECRET),
        (f"Found in repo:\n{_PRIVATE_KEY}\nrotate it", _PRIVATE_KEY, EntityType.SECRET),
        ("settings.yaml has api_key: 'q9X2mL7vR4tZ' committed", "q9X2mL7vR4tZ", EntityType.SECRET),
        (f"Invoice issued to CIF {VALID_CIF} last month.", VALID_CIF, EntityType.CIF),
        (f"Employee NSS {VALID_NSS} is on file.", VALID_NSS, EntityType.NSS),
        ("Vehicle 1234 BCD was towed from the depot.", "1234 BCD", EntityType.PLATE),
        ("Vehicle 1234-BCD was towed from the depot.", "1234-BCD", EntityType.PLATE),
        ("Device 00:1A:2B:3C:4D:5E went offline.", "00:1A:2B:3C:4D:5E", EntityType.MAC),
        ("Device 00-1a-2b-3c-4d-5e went offline.", "00-1a-2b-3c-4d-5e", EntityType.MAC),
        (
            "Last known location: 40.416775, -3.703790 at the depot",
            "40.416775, -3.703790",
            EntityType.GPS,
        ),
        ("Driver reported GPS 40.416775,-3.703790.", "40.416775,-3.703790", EntityType.GPS),
        (
            "Connection from 2001:db8::8a2e:370:7334 refused",
            "2001:db8::8a2e:370:7334",
            EntityType.IP,
        ),
        ("Firewall blocked 2001:db8::1.", "2001:db8::1", EntityType.IP),
        (
            "Link-local peer fe80::1ff:fe23:4567:890a dropped",
            "fe80::1ff:fe23:4567:890a",
            EntityType.IP,
        ),
    ],
)
def test_rule_based_entity_is_found_with_its_exact_span(
    text: str, expected: str, entity: EntityType
) -> None:
    spans = detect_regex(text)
    assert [(s.entity_type, s.text) for s in spans] == [(entity, expected)]
    for s in spans:
        assert text[s.start : s.end] == s.text


@pytest.mark.parametrize(
    ("text", "entity"),
    [
        ("Invoice issued to CIF B12345675 last month.", EntityType.CIF),
        ("Employee NSS 28/12345678/41 is on file.", EntityType.NSS),
    ],
)
def test_rule_based_failed_check_digit_yields_no_span(text: str, entity: EntityType) -> None:
    assert [s for s in detect_regex(text) if s.entity_type is entity] == []


@pytest.mark.parametrize(
    "text",
    [
        "Job finished at 12:30:45 without errors.",
        "Use std::vector instead of a raw array.",
        "Request id 550e8400-e29b-41d4-a716-446655440000 timed out.",
        "Convergence ratio 3.14159, 2.71828 between runs.",
        "password=********",
        "password: <redacted>",
        "Batch 1234 AEI processed overnight.",
        "Release 2024 BCD-55 shipped.",
    ],
)
def test_rule_based_lookalikes_are_not_detected(text: str) -> None:
    assert detect_regex(text) == []


def test_secret_in_connection_string_beats_the_overlapping_email() -> None:
    """Without the SECRET-over-EMAIL rule, `S3cr3tPass@db.internal` wins as an EMAIL."""
    text = "postgres://billing:S3cr3tPass@db.internal:5432/app"
    spans = detect_regex(text)
    assert [(s.entity_type, s.text) for s in spans] == [(EntityType.SECRET, "S3cr3tPass")]
    assert all(s.entity_type is not EntityType.EMAIL for s in spans)


def test_nss_beats_card_on_an_exact_tie() -> None:
    """A 12-digit run passing both mod-97 and Luhn is reported as NSS, not CARD."""
    value = "281000000824"
    assert valid_nss(value) is True
    assert valid_card(value) is True
    spans = detect_regex(f"Payroll record {value} updated")
    assert [(s.entity_type, s.text) for s in spans] == [(EntityType.NSS, value)]


@pytest.mark.parametrize(
    "text",
    [
        "Pay ES9121000418450200051332 with care",
        "Pay ES91 2100 0418 4502 0005 1332 with care",
        "IBAN ES9121000418450200051332 from your account",
    ],
)
def test_iban_followed_by_a_four_letter_word_is_still_found(text: str) -> None:
    """The greedy group pattern swallowed a trailing word and the IBAN leaked."""
    spans = [s for s in detect_regex(text) if s.entity_type is EntityType.IBAN]
    assert len(spans) == 1
    assert valid_iban(spans[0].text)
    assert spans[0].text.replace(" ", "").endswith("1332")


@pytest.mark.parametrize(
    "text",
    [
        "cvv 4111 1111 1111 1111 12/28",
        "tarjeta 4111 1111 1111 1111 123",
        "ref 12 4111 1111 1111 1111",
        "4111111111111111 123",
    ],
)
def test_card_glued_to_expiry_cvv_or_reference_is_detected(text: str) -> None:
    cards = [span for span in detect_regex(text) if span.entity_type is EntityType.CARD]

    assert [_compact_digits(span.text) for span in cards] == [VALID_CARD]
    assert all(text[span.start : span.end] == span.text for span in cards)


def test_two_cards_in_one_digit_run_are_both_detected() -> None:
    text = "4111 1111 1111 1111 5500 0000 0000 0004"

    cards = [span.text for span in detect_regex(text) if span.entity_type is EntityType.CARD]

    assert cards == ["4111 1111 1111 1111", "5500 0000 0000 0004"]


def _compact_digits(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


@pytest.mark.parametrize(
    ("text", "iban"),
    [
        ("pay DE89 3704 0044 0532 0130 00 now", "DE89 3704 0044 0532 0130 00"),
        ("pay DE89370400440532013000 now", "DE89370400440532013000"),
        ("pay GB29 NWBK 6016 1331 9268 19 now", "GB29 NWBK 6016 1331 9268 19"),
        ("pay GB29NWBK60161331926819 now", "GB29NWBK60161331926819"),
        ("pay FR14 2004 1010 0505 0001 3M02 606 now", "FR14 2004 1010 0505 0001 3M02 606"),
        ("pay FR1420041010050500013M02606 now", "FR1420041010050500013M02606"),
        ("pay NL91 ABNA 0417 1643 00 now", "NL91 ABNA 0417 1643 00"),
        ("pay NL91ABNA0417164300 now", "NL91ABNA0417164300"),
        ("ES91 2100 0418 4502 0005 1332 de Juan", "ES91 2100 0418 4502 0005 1332"),
    ],
)
def test_iban_of_any_length_is_detected_whole(text: str, iban: str) -> None:
    spans = detect_regex(text)

    assert [(span.entity_type, span.text) for span in spans] == [(EntityType.IBAN, iban)]


def test_iban_prefix_passing_mod97_by_chance_is_not_detected() -> None:
    """Trimming a bad 24-char Spanish IBAN reached a 16-char prefix that passes
    mod-97; Spanish IBANs are always 24 chars, so the prefix must be rejected."""
    assert valid_iban("ES43865889470313") is False
    spans = detect_regex("Transfer to ES4386588947031358349966 bounced, check digits")
    assert [s for s in spans if s.entity_type is EntityType.IBAN] == []


@pytest.mark.parametrize(
    "value",
    ["ES9121000418450200051332", "NO9386011117947", "MT84MALT011000012345MTLCAST001S"],
)
def test_iban_at_its_registered_length_still_validates(value: str) -> None:
    assert valid_iban(value) is True


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("DB_PASSWORD=hunter2222", "hunter2222"),
        ("POSTGRES_PASSWORD=Sup3rS3cret!", "Sup3rS3cret!"),
        (
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        ),
        ("access_token=abcdef123456789", "abcdef123456789"),
        ("token: abcdef123456789", "abcdef123456789"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
    ],
)
def test_common_secret_shapes_are_detected(text: str, secret: str) -> None:
    spans = detect_regex(text)

    assert [(span.entity_type, span.text) for span in spans] == [(EntityType.SECRET, secret)]


@pytest.mark.parametrize(
    "text",
    [
        "retry=3",
        "timeout=30s",
        "key=value",
        "password=<redacted>",
        "tokens=1234567",
        "token_count=123456",
    ],
)
def test_config_lookalikes_are_not_secrets(text: str) -> None:
    assert detect_regex(text) == []


class _FakeEncoder:
    identity = "fake"

    def __init__(self, spans: list[tuple[str, EntityType]]) -> None:
        self._spans = spans

    def detect_spans(self, text: str) -> list[Span]:
        out = []
        for value, entity_type in self._spans:
            start = text.index(value)
            out.append(Span(start, start + len(value), entity_type, value, Detector.ENCODER))
        return out


def test_encoder_spans_skip_known_non_pii_names() -> None:
    text = "Ask Claude Code to call Ana Garcia"
    encoder = _FakeEncoder([("Claude Code", EntityType.ORG), ("Ana Garcia", EntityType.PERSON)])

    spans = detect(text, encoder=encoder)

    assert [(s.entity_type, s.text) for s in spans] == [(EntityType.PERSON, "Ana Garcia")]
