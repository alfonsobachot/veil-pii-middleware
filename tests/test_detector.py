"""Detection layer: check digits, offsets, overlap policy, and the LLM layer.

The corrupted-value cases matter more than the valid ones. A validator that
always returns ``True`` scores perfect recall against the manifest and is the
most dangerous failure this suite can catch.

The LLM-layer tests run against a real local HTTP server standing in for LM
Studio (no `httpx` transport mocking), matching this codebase's existing
"never assume, verify the real behavior" testing style for the gateway
adapter -- these tests prove `detect_llm`/`detect`'s own contract logic
(offset recovery, fail-closed, caching). A real LM Studio instance was also
live-tested against this code (2026-09-08, see `.harness/handoff.md`), which
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
    valid_dni,
    valid_iban,
    valid_nie,
    valid_spanish_phone,
)

# Published, widely documented control values. If one of these fails, the
# implementation is wrong -- not the expectation.
VALID_DNI = "12345678Z"
VALID_NIE = "X1234567L"
VALID_IBAN = "ES9121000418450200051332"
VALID_CARD = "4111111111111111"


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
    "El cliente Customer1@Project.com abrio la incidencia. Su DNI es "
    f"{VALID_DNI} y el NIE del administrador es {VALID_NIE}. Cobramos en "
    f"{VALID_IBAN} con la tarjeta {VALID_CARD}. Telefono +34 612 345 678, "
    "servidor 192.168.1.15."
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
        "El DNI 12345678A no es valido.",
        "El NIE X1234567M no es valido.",
        "La cuenta ES9121000418450200051333 no es valida.",
        "La tarjeta 4111111111111112 no es valida.",
    ],
)
def test_failed_check_digit_yields_no_span(text: str) -> None:
    rejected = {EntityType.DNI, EntityType.NIE, EntityType.IBAN, EntityType.CARD}
    assert [span for span in detect_regex(text) if span.entity_type in rejected] == []


def test_plain_prose_yields_nothing() -> None:
    assert detect_regex("El pedido se entrego sin incidencias en el almacen.") == []


def test_version_string_is_not_an_ip() -> None:
    spans = detect_regex("Actualizado a la version 1.2.3 del cliente")
    assert [s for s in spans if s.entity_type is EntityType.IP] == []


@pytest.mark.parametrize(
    "text",
    [
        "Fallo en 192.168.1.15.",
        "Fallo en 192.168.1.15",
        "Fallo en 192.168.1.15, reintentando",
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


def test_longer_dotted_number_is_not_read_as_an_ip() -> None:
    spans = detect_regex("build 192.168.1.15.7 interna")
    assert [s for s in spans if s.entity_type is EntityType.IP] == []


def test_zero_padded_octets_are_still_an_ip() -> None:
    """Log exporters emit them; refusing to recognise one forwards it in clear."""
    spans = [
        s for s in detect_regex("IP 192.168.001.001 detectada") if s.entity_type is EntityType.IP
    ]
    assert [s.text for s in spans] == ["192.168.001.001"]


def test_octet_above_255_is_not_an_ip() -> None:
    assert [s for s in detect_regex("ref 300.400.500.600") if s.entity_type is EntityType.IP] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Telefono 912345678", "912345678"),
        ("Telefono 912 345 678", "912 345 678"),
        # 2-3-2-2 is simply how a Madrid landline is printed. Hard-coding the
        # 3-3-3 grouping made it invisible.
        ("Telefono 91 234 56 78", "91 234 56 78"),
        ("Telefono 912 34 56 78", "912 34 56 78"),
        ("Telefono 91-234-56-78", "91-234-56-78"),
    ],
)
def test_phone_is_found_in_any_grouping(text: str, expected: str) -> None:
    spans = [s for s in detect_regex(text) if s.entity_type is EntityType.PHONE]
    assert [s.text for s in spans] == [expected]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Telefono +34 612 345 678", "+34 612 345 678"),
        ("Telefono +34 91 234 56 78", "+34 91 234 56 78"),
        # Any country, not a hard-coded +34.
        ("Telefono +44 20 7946 0958", "+44 20 7946 0958"),
        ("Telefono +1 415 555 2671", "+1 415 555 2671"),
        ("Telefono 0034 612345678", "0034 612345678"),
    ],
)
def test_international_phone_keeps_its_country_code(text: str, expected: str) -> None:
    """The span must cover the prefix; dropping it would tokenize half a number."""
    spans = [s for s in detect_regex(text) if s.entity_type is EntityType.PHONE]
    assert [s.text for s in spans] == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "El importe asciende a 912.345.678 euros",
        "Stock de 912.345.678 unidades",
        "Referencia 612.345.678 del pedido",
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
    assert detect_regex("Cuenta 0012345678 del banco") == []


def test_dot_separated_card_is_not_claimed() -> None:
    """Cards are not written with dots, and Luhn alone is a weak filter."""
    spans = [
        s for s in detect_regex("Tarjeta 4111.1111.1111.1111") if s.entity_type is EntityType.CARD
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
    spans = detect_regex("Origen 192.168.001.001 registrado")
    assert [(s.entity_type, s.text) for s in spans] == [(EntityType.IP, "192.168.001.001")]


def test_iban_beats_the_digit_runs_inside_it() -> None:
    spans = detect_regex(f"Cuenta {VALID_IBAN} confirmada")
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


def test_merge_breaks_length_ties_in_favour_of_regex() -> None:
    llm = span(0, 4, Detector.LLM)
    regex = span(2, 6, Detector.REGEX)
    assert merge_spans([llm, regex]) == [regex]


def test_merge_returns_spans_ordered_by_start() -> None:
    assert [s.start for s in merge_spans([span(20, 24), span(0, 4), span(10, 14)])] == [0, 10, 20]


def test_merge_is_idempotent() -> None:
    once = merge_spans([span(0, 10), span(2, 5), span(12, 16)])
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state, base_url
    finally:
        server.shutdown()


@pytest.fixture(autouse=True)
def _reset_llm_cache() -> Iterator[None]:
    clear_llm_cache()
    yield
    clear_llm_cache()


class TestDetectLLM:
    def test_happy_path_finds_person_and_address(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        text = "Contacta a Ana Garcia en Calle Mayor 5, Madrid."
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
            detect_llm("hola", client)

        system_prompt = str(state.requests[0]["messages"][0]["content"])
        for entity_type in EntityType:
            assert entity_type.value in system_prompt

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
            detect_llm("hola", client)

        assert "response_format" not in state.requests[0]

    def test_duplicate_value_claims_separate_occurrences(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm
        text = "Ana Garcia llamo. Luego Ana Garcia colgo."
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
            {"spans": [{"value": "Nombre Que No Existe", "entity_type": "PERSON"}]}
        )
        with LLMClient(base_url, "test-model", timeout_seconds=2.0) as client:
            spans = detect_llm("texto sin ese nombre", client)

        assert spans == []

    def test_unknown_entity_type_raises(self, fake_llm: tuple[_FakeLLMState, str]) -> None:
        state, base_url = fake_llm
        state.completion_content = json.dumps({"spans": [{"value": "x", "entity_type": "BOGUS"}]})
        with (
            LLMClient(base_url, "test-model", timeout_seconds=2.0) as client,
            pytest.raises(LLMDetectionError),
        ):
            detect_llm("x appears here", client)

    def test_flat_string_span_entry_raises_without_leaking_its_content(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        """A model returning `{"spans": ["value", ...]}` instead of objects is a
        realistic instruction-following failure. The raw string -- which could be
        the exact PII value the model was asked to redact -- must never appear in
        the raised exception's message, since that message is later logged.
        """
        state, base_url = fake_llm
        secret = "secreto-de-cliente@example.com"
        state.completion_content = json.dumps({"spans": [secret]})
        with (
            LLMClient(base_url, "test-model", timeout_seconds=2.0) as client,
            pytest.raises(LLMDetectionError) as exc_info,
        ):
            detect_llm(f"correo {secret}", client)

        assert secret not in str(exc_info.value)

    def test_bad_value_field_raises_without_leaking_other_fields(
        self, fake_llm: tuple[_FakeLLMState, str]
    ) -> None:
        """An entry with a non-string 'value' must not echo whatever else it carries."""
        state, base_url = fake_llm
        secret = "otro-secreto@example.com"
        state.completion_content = json.dumps(
            {"spans": [{"value": 12345, "entity_type": "PERSON", "note": secret}]}
        )
        with (
            LLMClient(base_url, "test-model", timeout_seconds=2.0) as client,
            pytest.raises(LLMDetectionError) as exc_info,
        ):
            detect_llm(f"correo {secret}", client)

        assert secret not in str(exc_info.value)

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
        text = "Vive en Calle Mayor 5, Madrid."
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
        assert len(state.requests) == 2


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
        text = f"DNI {VALID_DNI} y correo cliente@example.com"
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
        secret_email = "secreto-de-cliente@example.com"
        with LLMClient(_closed_port_url(), "test-model", timeout_seconds=1.0) as client:
            detect(f"correo {secret_email}", llm_client=client)

        captured = capsys.readouterr()
        assert secret_email not in captured.err
        assert secret_email not in captured.out
