"""Tests for veil_pii_middleware.core.tokenizer.

Covers single- and multi-entity substitution, exact round-trip, nested
structure recursion, same-value-same-token consistency end-to-end, and
fail-closed behavior on a token that cannot be resolved.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from veil_pii_middleware.core.detector import EntityType
from veil_pii_middleware.core.tokenizer import (
    JSONValue,
    TokenFormatOverflowError,
    detokenize,
    tokenize,
)
from veil_pii_middleware.core.vault import TokenNotFoundError, Vault

_TEST_KEY = os.urandom(32)

VALID_DNI = "12345678Z"
VALID_IBAN = "ES9121000418450200051332"
VALID_CARD = "4111111111111111"
VALID_IP = "192.168.1.15"
VALID_PHONE = "+34 612 345 678"
VALID_EMAIL = "alice@example.com"


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Vault]:
    with Vault(tmp_path / "vault.db", "session-1", master_key=_TEST_KEY) as v:
        yield v


class TestSingleValueSubstitution:
    def test_email_replaced_with_token(self, vault: Vault) -> None:
        text = f"contact {VALID_EMAIL} for details"
        result = tokenize(text, vault)

        assert isinstance(result, str)
        assert VALID_EMAIL not in result
        assert result.startswith("contact PII_EMAIL_0001 for details")

    def test_non_pii_text_untouched(self, vault: Vault) -> None:
        text = "no personal data in this sentence at all"
        assert tokenize(text, vault) == text


class TestMultiTypeSubstitution:
    def test_three_entity_types_in_one_call(self, vault: Vault) -> None:
        text = f"Cliente {VALID_EMAIL}, DNI {VALID_DNI}, telefono {VALID_PHONE}, resto intacto."
        result = tokenize(text, vault)
        assert isinstance(result, str)

        assert "PII_EMAIL_0001" in result
        assert "PII_DNI_0001" in result
        assert "PII_PHONE_0001" in result
        assert VALID_EMAIL not in result
        assert VALID_DNI not in result
        assert "resto intacto." in result

    def test_surrounding_text_preserved(self, vault: Vault) -> None:
        text = f"HEAD: {VALID_EMAIL} MID: {VALID_DNI} TAIL"
        result = tokenize(text, vault)
        assert isinstance(result, str)

        assert result.startswith("HEAD: ")
        assert " MID: " in result
        assert result.endswith(" TAIL")


class TestExactRoundTrip:
    @pytest.mark.parametrize(
        "text",
        [
            f"contact {VALID_EMAIL} please",
            f"{VALID_EMAIL} at the very start",
            f"ends with the email {VALID_EMAIL}",
            f"multi: {VALID_EMAIL}, {VALID_DNI}, {VALID_IBAN}, {VALID_CARD}, {VALID_IP}",
            "sentence with absolutely no PII whatsoever",
            "",
        ],
    )
    def test_tokenize_then_detokenize_restores_original(self, vault: Vault, text: str) -> None:
        tokenized = tokenize(text, vault)
        assert isinstance(tokenized, str)
        restored = detokenize(tokenized, vault)
        assert restored == text


class TestNestedStructures:
    def test_dict_with_lists_and_subdicts(self, vault: Vault) -> None:
        payload: JSONValue = {
            "customer_email": VALID_EMAIL,
            "active": True,
            "score": 3.5,
            "notes": None,
            "tickets": [
                {"id": 1, "reporter_dni": VALID_DNI},
                {"id": 2, "reporter_dni": VALID_DNI},
            ],
        }

        tokenized = tokenize(payload, vault)
        assert isinstance(tokenized, dict)

        assert tokenized["customer_email"] == "PII_EMAIL_0001"
        assert tokenized["active"] is True
        assert tokenized["score"] == 3.5
        assert tokenized["notes"] is None

        tickets = tokenized["tickets"]
        assert isinstance(tickets, list)
        first_ticket = tickets[0]
        assert isinstance(first_ticket, dict)
        assert first_ticket["id"] == 1
        assert first_ticket["reporter_dni"] == "PII_DNI_0001"
        second_ticket = tickets[1]
        assert isinstance(second_ticket, dict)
        assert second_ticket["reporter_dni"] == "PII_DNI_0001"

        restored = detokenize(tokenized, vault)
        assert restored == payload

    def test_keys_and_original_structure_untouched(self, vault: Vault) -> None:
        payload: JSONValue = {"a": {"b": [VALID_EMAIL, "plain text", 42]}}
        tokenized = tokenize(payload, vault)
        assert isinstance(tokenized, dict)

        assert set(tokenized.keys()) == {"a"}
        inner = tokenized["a"]
        assert isinstance(inner, dict)
        assert set(inner.keys()) == {"b"}
        inner_list = inner["b"]
        assert isinstance(inner_list, list)
        assert inner_list[1] == "plain text"
        assert inner_list[2] == 42

    def test_does_not_mutate_input_in_place(self, vault: Vault) -> None:
        original: JSONValue = {"email": VALID_EMAIL, "items": [VALID_DNI]}
        snapshot: JSONValue = {"email": VALID_EMAIL, "items": [VALID_DNI]}

        tokenize(original, vault)

        assert original == snapshot


class TestRepeatedValueSameToken:
    def test_same_value_twice_in_text_yields_same_token(self, vault: Vault) -> None:
        text = f"first {VALID_EMAIL}, again {VALID_EMAIL}"
        result = tokenize(text, vault)
        assert isinstance(result, str)

        assert result.count("PII_EMAIL_0001") == 2
        assert "PII_EMAIL_0002" not in result

    def test_same_value_across_structure_leaves_yields_same_token(self, vault: Vault) -> None:
        payload: JSONValue = {"a": VALID_EMAIL, "b": [VALID_EMAIL, {"c": VALID_EMAIL}]}
        tokenized = tokenize(payload, vault)
        assert isinstance(tokenized, dict)

        assert tokenized["a"] == "PII_EMAIL_0001"
        b = tokenized["b"]
        assert isinstance(b, list)
        assert b[0] == "PII_EMAIL_0001"
        sub = b[1]
        assert isinstance(sub, dict)
        assert sub["c"] == "PII_EMAIL_0001"


class TestUnknownTokenFailsClosed:
    def test_unknown_token_raises(self, vault: Vault) -> None:
        with pytest.raises(TokenNotFoundError):
            detokenize("see PII_EMAIL_9999 here", vault)

    def test_foreign_session_token_raises(self, tmp_path: Path, vault: Vault) -> None:
        db_path = tmp_path / "vault.db"
        with Vault(db_path, "other-session", master_key=_TEST_KEY) as other_vault:
            foreign_token = other_vault.get_or_create_token(VALID_EMAIL, EntityType.EMAIL)

        with pytest.raises(TokenNotFoundError):
            detokenize(foreign_token, vault)

    def test_nested_structure_aborts_on_first_bad_token(self, vault: Vault) -> None:
        tokenized_good = tokenize(VALID_EMAIL, vault)
        payload: JSONValue = {"good": tokenized_good, "bad": "PII_EMAIL_9999"}

        with pytest.raises(TokenNotFoundError):
            detokenize(payload, vault)

    def test_exception_never_carries_plaintext(self, vault: Vault) -> None:
        with pytest.raises(TokenNotFoundError) as exc_info:
            detokenize("PII_EMAIL_9999", vault)

        assert VALID_EMAIL not in str(exc_info.value)


class TestTokenFormatOverflow:
    def test_sequence_past_9999_raises_before_emitting_ambiguous_token(self, vault: Vault) -> None:
        # Force this session's EMAIL counter past the fixed four-digit width,
        # the way it would look after 9999 prior EMAIL tokens in one session.
        vault._conn.execute(
            "INSERT INTO counters (session_id, entity_type, next_seq) VALUES (?, ?, ?)",
            ("session-1", EntityType.EMAIL.value, 10000),
        )
        vault._conn.commit()

        with pytest.raises(TokenFormatOverflowError):
            tokenize(f"contact {VALID_EMAIL}", vault)


class TestOffsetCorrectnessUnderMultipleSpans:
    def test_non_pii_characters_preserved_at_expected_positions(self, vault: Vault) -> None:
        text = f"A={VALID_IP} B={VALID_EMAIL} C={VALID_DNI} D=end"
        result = tokenize(text, vault)
        assert isinstance(result, str)

        assert result.startswith("A=")
        assert " B=" in result
        assert " C=" in result
        assert result.endswith(" D=end")

        restored = detokenize(result, vault)
        assert restored == text
