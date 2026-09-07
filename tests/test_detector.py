"""Deterministic detection layer: check digits, offsets and overlap policy.

The corrupted-value cases matter more than the valid ones. A validator that
always returns ``True`` scores perfect recall against the manifest and is the
most dangerous failure this suite can catch.
"""

import pytest
from veil_pii_middleware.core.detector import (
    Detector,
    EntityType,
    Span,
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
    assert found == set(EntityType)


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
