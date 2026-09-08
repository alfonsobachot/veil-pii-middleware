"""Tests for veil_pii_middleware.core.tokenizer.

Covers single- and multi-entity substitution, exact round-trip, nested
structure recursion, same-value-same-token consistency end-to-end, and
fail-closed behavior on a token that cannot be resolved.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from veil_pii_middleware.core.detector import (
    LLM_BASE_URL_ENV_VAR,
    LLM_MODEL_ENV_VAR,
    EntityType,
    LLMClient,
    clear_llm_cache,
)
from veil_pii_middleware.core.tokenizer import (
    UNRESOLVED_TOKEN_PLACEHOLDER,
    IncrementalDetokenizer,
    JSONValue,
    TokenFormatOverflowError,
    detokenize,
    detokenize_lenient,
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


class TestIncrementalDetokenizer:
    """Chunk-boundary-safe response-side detokenization (mission-8).

    ``feed()`` is called once per incoming text chunk; ``flush()`` once at
    end of stream. Every scenario is checked against the token produced by
    the token minted for ``VALID_EMAIL`` above: ``PII_EMAIL_0001``.
    """

    def _token_for(self, vault: Vault) -> str:
        token = vault.get_or_create_token(VALID_EMAIL, EntityType.EMAIL)
        assert token == "PII_EMAIL_0001"
        return token

    def test_whole_token_in_one_call_resolves_immediately(self, vault: Vault) -> None:
        token = self._token_for(vault)
        d = IncrementalDetokenizer(vault)

        out = d.feed(f"the email is {token}, thanks")
        assert out == f"the email is {VALID_EMAIL}, thanks"
        assert d.flush() == ""

    def test_split_at_every_offset_matches_unsplit_result(self, tmp_path: Path) -> None:
        text_template = "contact {token} today"
        expected = f"contact {VALID_EMAIL} today"

        for split_at in range(1, len(text_template.format(token="PII_EMAIL_0001"))):
            with Vault(tmp_path / f"v{split_at}.db", "s", master_key=_TEST_KEY) as v:
                token = v.get_or_create_token(VALID_EMAIL, EntityType.EMAIL)
                text = text_template.format(token=token)
                d = IncrementalDetokenizer(v)

                first, second = text[:split_at], text[split_at:]
                out = d.feed(first) + d.feed(second) + d.flush()
                assert out == expected, f"split at {split_at} failed: {out!r}"

    def test_split_across_three_calls(self, vault: Vault) -> None:
        token = self._token_for(vault)
        text = f"contact {token} today"
        third = len(text) // 3
        d = IncrementalDetokenizer(vault)

        out = (
            d.feed(text[:third])
            + d.feed(text[third : 2 * third])
            + d.feed(text[2 * third :])
            + d.flush()
        )
        assert out == f"contact {VALID_EMAIL} today"

    def test_split_one_character_at_a_time(self, vault: Vault) -> None:
        token = self._token_for(vault)
        text = f"contact {token} today"
        d = IncrementalDetokenizer(vault)

        out = "".join(d.feed(ch) for ch in text) + d.flush()
        assert out == f"contact {VALID_EMAIL} today"

    def test_ordinary_long_uppercase_text_is_not_buffered_indefinitely(self, vault: Vault) -> None:
        d = IncrementalDetokenizer(vault)
        chunk = "THIS IS ALL CAPS " * 50

        out = d.feed(chunk)

        # A run of plain uppercase text with no "PII_" anchor never matches
        # any alternative of the pending-tail pattern, so it must all flush
        # immediately rather than accumulate in the internal buffer -- if
        # anything were still held back, flush() would return it here.
        assert out == chunk
        assert d.flush() == ""

    def test_unresolved_token_gets_placeholder_not_raised(self, vault: Vault) -> None:
        d = IncrementalDetokenizer(vault)

        out = d.feed("see PII_EMAIL_9999 here")

        assert out == f"see {UNRESOLVED_TOKEN_PLACEHOLDER} here"

    def test_integrity_failure_gets_placeholder_and_is_logged_distinctly(
        self, vault: Vault, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # Same class of tampering as test_vault.py's
        # TestAadIntegrity::test_swapped_ciphertext_between_rows_fails_authentication,
        # but checked here to confirm the response-side placeholder path
        # specifically distinguishes this from a routine unresolved token.
        token_a = vault.get_or_create_token(VALID_EMAIL, EntityType.EMAIL)
        token_b = vault.get_or_create_token(VALID_DNI, EntityType.DNI)
        row_a = vault._conn.execute(
            "SELECT ciphertext, nonce FROM mappings WHERE token = ?", (token_a,)
        ).fetchone()
        vault._conn.execute(
            "UPDATE mappings SET ciphertext = ?, nonce = ? WHERE token = ?",
            (row_a[0], row_a[1], token_b),
        )
        vault._conn.commit()

        d = IncrementalDetokenizer(vault)
        out = d.feed(f"see {token_b} here")

        assert out == f"see {UNRESOLVED_TOKEN_PLACEHOLDER} here"
        _, err = capfd.readouterr()
        assert "integrity" in err.lower()
        assert token_b in err
        assert VALID_EMAIL not in err
        assert VALID_DNI not in err

    def test_unresolved_token_placeholder_survives_a_split(self, vault: Vault) -> None:
        text = "see PII_EMAIL_9999 here"
        d = IncrementalDetokenizer(vault)

        out = d.feed(text[:10]) + d.feed(text[10:]) + d.flush()

        assert out == f"see {UNRESOLVED_TOKEN_PLACEHOLDER} here"

    def test_end_of_stream_flush_of_a_near_miss_is_plain_text_not_placeholder(
        self, vault: Vault
    ) -> None:
        d = IncrementalDetokenizer(vault)

        out = d.feed("contact PII_EM")
        tail = d.flush()

        assert out == "contact "
        assert tail == "PII_EM"
        assert UNRESOLVED_TOKEN_PLACEHOLDER not in tail

    def test_flush_of_incomplete_digit_run_is_plain_text(self, vault: Vault) -> None:
        # Only two of the four digits ever arrive -- never a real token.
        d = IncrementalDetokenizer(vault)

        out = d.feed("contact PII_EMAIL_00")
        tail = d.flush()

        assert out == "contact "
        assert tail == "PII_EMAIL_00"

    def test_two_tokens_in_one_stream_each_resolve(self, vault: Vault) -> None:
        email_token = self._token_for(vault)
        dni_token = vault.get_or_create_token(VALID_DNI, EntityType.DNI)
        text = f"{email_token} and {dni_token}"
        d = IncrementalDetokenizer(vault)

        mid = len(text) // 2
        out = d.feed(text[:mid]) + d.feed(text[mid:]) + d.flush()

        assert out == f"{VALID_EMAIL} and {VALID_DNI}"

    def test_independent_instances_do_not_share_buffered_state(self, vault: Vault) -> None:
        token = self._token_for(vault)
        first_half = token[:6]

        block_a = IncrementalDetokenizer(vault)
        block_b = IncrementalDetokenizer(vault)

        # Block B never receives the second half -- a token must not span
        # two independent content blocks (Tooling Assessment).
        out_a = block_a.feed(first_half)
        out_b = block_b.feed("unrelated text, no token here")

        assert out_a == ""
        assert out_b == "unrelated text, no token here"
        assert block_a.flush() == first_half


class TestDetokenizeLenient:
    def test_known_token_resolves_like_detokenize(self, vault: Vault) -> None:
        tokenized = tokenize(VALID_EMAIL, vault)
        assert detokenize_lenient(tokenized, vault) == VALID_EMAIL

    def test_unknown_token_gets_placeholder_instead_of_raising(self, vault: Vault) -> None:
        assert detokenize_lenient("see PII_EMAIL_9999 here", vault) == (
            f"see {UNRESOLVED_TOKEN_PLACEHOLDER} here"
        )

    def test_nested_structure_does_not_abort_on_a_bad_token(self, vault: Vault) -> None:
        good = tokenize(VALID_EMAIL, vault)
        payload: JSONValue = {"good": good, "bad": "PII_EMAIL_9999", "plain": 42}

        result = detokenize_lenient(payload, vault)

        assert isinstance(result, dict)
        assert result["good"] == VALID_EMAIL
        assert result["bad"] == UNRESOLVED_TOKEN_PLACEHOLDER
        assert result["plain"] == 42


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


# --------------------------------------------------------------------------
# LLM-layer wiring: VEIL_LLM_BASE_URL/VEIL_LLM_MODEL gate an env-built client
# --------------------------------------------------------------------------


class _FakeLLMState:
    def __init__(self) -> None:
        self.request_count = 0
        self.status = 200
        self.completion_content: str = json.dumps({"spans": []})


def _make_llm_handler(state: _FakeLLMState) -> type[BaseHTTPRequestHandler]:
    class FakeLLMHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)
            state.request_count += 1
            payload = json.dumps(
                {"choices": [{"message": {"content": state.completion_content}}]}
            ).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return FakeLLMHandler


@pytest.fixture
def fake_llm_server() -> Iterator[tuple[_FakeLLMState, str]]:
    state = _FakeLLMState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_llm_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state, base_url
    finally:
        server.shutdown()
        clear_llm_cache()


class TestLLMLayerWiring:
    def test_env_unset_reproduces_regex_only_behavior_exactly(
        self, vault: Vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(LLM_BASE_URL_ENV_VAR, raising=False)
        monkeypatch.delenv(LLM_MODEL_ENV_VAR, raising=False)
        text = f"Ana Garcia, correo {VALID_EMAIL}"

        result = tokenize(text, vault)

        assert isinstance(result, str)
        assert VALID_EMAIL not in result
        assert "PII_EMAIL_0001" in result
        # No PERSON entity type exists in the deterministic layer's output,
        # so an unconfigured LLM layer must leave the name untouched.
        assert "Ana Garcia" in result

    def test_explicit_llm_client_tokenizes_a_person_span(
        self, vault: Vault, fake_llm_server: tuple[_FakeLLMState, str]
    ) -> None:
        state, base_url = fake_llm_server
        state.completion_content = json.dumps(
            {"spans": [{"value": "Ana Garcia", "entity_type": "PERSON"}]}
        )
        text = f"Ana Garcia, correo {VALID_EMAIL}"
        client = LLMClient(base_url, "test-model", timeout_seconds=2.0)

        try:
            result = tokenize(text, vault, llm_client=client)
        finally:
            client.close()

        assert isinstance(result, str)
        assert "Ana Garcia" not in result
        assert "PII_PERSON_0001" in result
        assert "PII_EMAIL_0001" in result

        restored = detokenize(result, vault)
        assert restored == text

    def test_env_configured_client_is_used_and_closed_automatically(
        self,
        vault: Vault,
        fake_llm_server: tuple[_FakeLLMState, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state, base_url = fake_llm_server
        state.completion_content = json.dumps(
            {"spans": [{"value": "Ana Garcia", "entity_type": "PERSON"}]}
        )
        monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, base_url)
        monkeypatch.setenv(LLM_MODEL_ENV_VAR, "test-model")

        result = tokenize("Ana Garcia", vault)

        assert result == "PII_PERSON_0001"
        assert state.request_count == 1

    def test_llm_non_2xx_response_still_tokenizes_structured_entities(
        self,
        vault: Vault,
        fake_llm_server: tuple[_FakeLLMState, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A misbehaving LLM endpoint must not stop the regex layer's own guarantee.

        This is BRIEF acceptance criterion 4 exercised at the level a real
        adapter actually calls: `tokenize()` itself, not just `detect()`.
        """
        state, base_url = fake_llm_server
        state.status = 500
        monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, base_url)
        monkeypatch.setenv(LLM_MODEL_ENV_VAR, "test-model")
        text = f"Ana Garcia, correo {VALID_EMAIL}"

        result = tokenize(text, vault)

        assert isinstance(result, str)
        assert VALID_EMAIL not in result
        assert "PII_EMAIL_0001" in result
        # No exception propagated and the call still completed -- the LLM
        # layer's failure did not block the structured entity from being
        # tokenized, matching detect()'s fail-closed contract.
