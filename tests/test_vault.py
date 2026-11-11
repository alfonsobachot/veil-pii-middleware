"""Tests for veil_pii_middleware.core.vault.

Covers round-trip crypto, HMAC normalization consistency, cross-entity-type
non-collision, nonce uniqueness, fail-closed key handling, TTL purge, and the
monotonic-non-reuse guarantee on token sequence numbers.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from veil_pii_middleware.core.detector import EntityType
from veil_pii_middleware.core.vault import (
    MASTER_KEY_ENV_VAR,
    InvalidMasterKeyError,
    TokenNotFoundError,
    Vault,
    VaultIntegrityError,
)

_TEST_KEY = os.urandom(32)


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Vault]:
    with Vault(tmp_path / "vault.db", "session-1", master_key=_TEST_KEY) as v:
        yield v


def _backdate(vault_instance: Vault, token: str, created_at: int) -> None:
    vault_instance._conn.execute(
        "UPDATE mappings SET created_at = ? WHERE token = ?", (created_at, token)
    )
    vault_instance._conn.commit()


class TestRoundTrip:
    def test_resolve_returns_original_value(self, vault: Vault) -> None:
        token = vault.get_or_create_token("alice@example.com", EntityType.EMAIL)
        assert vault.resolve(token) == "alice@example.com"

    def test_token_format(self, vault: Vault) -> None:
        token = vault.get_or_create_token("alice@example.com", EntityType.EMAIL)
        assert token == "PII_EMAIL_0001"

    def test_unknown_token_raises(self, vault: Vault) -> None:
        with pytest.raises(TokenNotFoundError):
            vault.resolve("PII_EMAIL_9999")


class TestHmacNormalizationConsistency:
    @pytest.mark.parametrize(
        ("entity_type", "value_a", "value_b"),
        [
            (EntityType.EMAIL, "Alice@Example.com", " alice@example.com "),
            (EntityType.DNI, "12345678Z", "12345678 Z"),
            (EntityType.NIE, "x1234567l", "X1234567L"),
            (EntityType.IBAN, "ES9121000418450200051332", "ES91 2100 0418 4502 0005 1332"),
            (EntityType.PHONE, "+34612345678", "+34 612-345 678"),
            (EntityType.CARD, "4111111111111111", "4111-1111-1111-1111"),
            (EntityType.IP, "192.168.1.1", " 192.168.1.1 "),
        ],
    )
    def test_equivalent_values_share_token(
        self, vault: Vault, entity_type: EntityType, value_a: str, value_b: str
    ) -> None:
        token_a = vault.get_or_create_token(value_a, entity_type)
        token_b = vault.get_or_create_token(value_b, entity_type)
        assert token_a == token_b


class TestCrossTypeNonCollision:
    def test_same_raw_value_different_types_get_different_tokens(self, vault: Vault) -> None:
        raw = "912345678"
        token_phone = vault.get_or_create_token(raw, EntityType.PHONE)
        token_card = vault.get_or_create_token(raw, EntityType.CARD)
        assert token_phone != token_card
        assert vault.resolve(token_phone) == raw
        assert vault.resolve(token_card) == raw


class TestNonceUniqueness:
    def test_distinct_values_get_distinct_nonces_and_ciphertext(
        self, vault: Vault, tmp_path: Path
    ) -> None:
        vault.get_or_create_token("one@example.com", EntityType.EMAIL)
        vault.get_or_create_token("two@example.com", EntityType.EMAIL)

        rows = vault._conn.execute("SELECT nonce, ciphertext FROM mappings").fetchall()
        assert len(rows) == 2
        nonces = [row[0] for row in rows]
        ciphertexts = [row[1] for row in rows]
        assert nonces[0] != nonces[1]
        assert ciphertexts[0] != ciphertexts[1]


class TestAadIntegrity:
    def test_swapped_ciphertext_between_rows_fails_authentication(self, vault: Vault) -> None:
        token_a = vault.get_or_create_token("alice@example.com", EntityType.EMAIL)
        token_b = vault.get_or_create_token("bob@example.com", EntityType.EMAIL)

        row_a = vault._conn.execute(
            "SELECT ciphertext, nonce FROM mappings WHERE token = ?", (token_a,)
        ).fetchone()

        # Swap row A's (ciphertext, nonce) onto row B's identity. Without an
        # AAD binding the token to its own ciphertext, this decrypts
        # "successfully" and silently returns alice's value under bob's
        # token instead of failing.
        vault._conn.execute(
            "UPDATE mappings SET ciphertext = ?, nonce = ? WHERE token = ?",
            (row_a[0], row_a[1], token_b),
        )
        vault._conn.commit()

        with pytest.raises(VaultIntegrityError):
            vault.resolve(token_b)


class TestFailClosed:
    def test_missing_env_var_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)
        db_path = tmp_path / "vault.db"
        with pytest.raises(InvalidMasterKeyError):
            Vault(db_path, "session-1")
        assert not db_path.exists()

    def test_empty_env_var_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MASTER_KEY_ENV_VAR, "")
        db_path = tmp_path / "vault.db"
        with pytest.raises(InvalidMasterKeyError):
            Vault(db_path, "session-1")
        assert not db_path.exists()

    def test_non_base64_env_var_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MASTER_KEY_ENV_VAR, "not-valid-base64!!!")
        db_path = tmp_path / "vault.db"
        with pytest.raises(InvalidMasterKeyError):
            Vault(db_path, "session-1")
        assert not db_path.exists()

    def test_wrong_length_key_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        short_key_b64 = base64.b64encode(os.urandom(16)).decode()
        monkeypatch.setenv(MASTER_KEY_ENV_VAR, short_key_b64)
        db_path = tmp_path / "vault.db"
        with pytest.raises(InvalidMasterKeyError):
            Vault(db_path, "session-1")
        assert not db_path.exists()

    def test_valid_env_var_key_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MASTER_KEY_ENV_VAR, base64.b64encode(_TEST_KEY).decode())
        with Vault(tmp_path / "vault.db", "session-1") as v:
            token = v.get_or_create_token("alice@example.com", EntityType.EMAIL)
            assert v.resolve(token) == "alice@example.com"


class TestNoPlaintextAtRest:
    def test_plaintext_value_absent_from_db_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "vault.db"
        secret_value = "very-secret-national-id-12345678Z"
        with Vault(db_path, "session-1", master_key=_TEST_KEY) as v:
            v.get_or_create_token(secret_value, EntityType.DNI)

        raw_bytes = db_path.read_bytes()
        assert secret_value.encode() not in raw_bytes


class TestTtlPurge:
    def test_purge_deletes_old_and_keeps_new(self, vault: Vault) -> None:
        old_token = vault.get_or_create_token("old@example.com", EntityType.EMAIL)
        new_token = vault.get_or_create_token("new@example.com", EntityType.EMAIL)

        now = 1_000_000
        _backdate(vault, old_token, now - 500)
        _backdate(vault, new_token, now - 10)

        deleted = vault.purge_expired(ttl_seconds=100, now=now)

        assert deleted == 1
        with pytest.raises(TokenNotFoundError):
            vault.resolve(old_token)
        assert vault.resolve(new_token) == "new@example.com"

    def test_purge_returns_zero_when_nothing_expired(self, vault: Vault) -> None:
        token = vault.get_or_create_token("keep@example.com", EntityType.EMAIL)
        now = 1_000_000
        _backdate(vault, token, now - 10)

        deleted = vault.purge_expired(ttl_seconds=1000, now=now)

        assert deleted == 0
        assert vault.resolve(token) == "keep@example.com"


class TestMonotonicNonReuse:
    def test_sequence_number_never_reused_after_purge(self, vault: Vault) -> None:
        first_token = vault.get_or_create_token("first@example.com", EntityType.EMAIL)
        assert first_token == "PII_EMAIL_0001"

        now = 1_000_000
        _backdate(vault, first_token, now - 500)
        vault.purge_expired(ttl_seconds=100, now=now)

        second_token = vault.get_or_create_token("second@example.com", EntityType.EMAIL)
        assert second_token == "PII_EMAIL_0002"


class TestSessionIsolation:
    def test_token_from_other_session_does_not_resolve(self, tmp_path: Path) -> None:
        db_path = tmp_path / "vault.db"
        with Vault(db_path, "session-a", master_key=_TEST_KEY) as vault_a:
            token = vault_a.get_or_create_token("alice@example.com", EntityType.EMAIL)

        with (
            Vault(db_path, "session-b", master_key=_TEST_KEY) as vault_b,
            pytest.raises(TokenNotFoundError),
        ):
            vault_b.resolve(token)

    def test_equal_values_in_different_sessions_can_get_same_token_text(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "vault.db"
        with Vault(db_path, "session-a", master_key=_TEST_KEY) as vault_a:
            token_a = vault_a.get_or_create_token("alice@example.com", EntityType.EMAIL)

        with Vault(db_path, "session-b", master_key=_TEST_KEY) as vault_b:
            token_b = vault_b.get_or_create_token("alice@example.com", EntityType.EMAIL)

        assert token_a == token_b == "PII_EMAIL_0001"

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT COUNT(*) FROM mappings WHERE token = ?", (token_a,)).fetchone()
        assert rows[0] == 2
        conn.close()


class TestConcurrentTokenCreation:
    def test_parallel_vaults_on_one_db_never_collide(self, tmp_path: Path) -> None:
        """Two parallel hooks used to hit `UNIQUE constraint failed` on the same db."""
        db_path = tmp_path / "vault.db"
        key = os.urandom(32)
        shared = [f"shared{i}@example.com" for i in range(15)]
        barrier = threading.Barrier(4)
        results: dict[int, dict[str, str]] = {}
        errors: list[BaseException] = []

        def worker(worker_id: int) -> None:
            try:
                with Vault(db_path, "session-1", master_key=key) as vault:
                    own = [f"w{worker_id}-{i}@example.com" for i in range(15)]
                    values = [v for pair in zip(shared, own, strict=True) for v in pair]
                    barrier.wait()
                    results[worker_id] = {
                        v: vault.get_or_create_token(v, EntityType.EMAIL) for v in values
                    }
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        for value in shared:
            assert len({results[w][value] for w in range(4)}) == 1
        by_token: dict[str, str] = {}
        for mapping in results.values():
            for value, token in mapping.items():
                assert by_token.setdefault(token, value) == value
        assert len(by_token) == len(shared) + 4 * 15
