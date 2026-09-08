"""Encrypted SQLite store of token <-> value mappings, one set per session.

Values are held as AES-GCM ciphertext and located by HMAC-SHA256 of the
normalized value, so lookups never need plaintext. The same value always
yields the same token within a session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from veil_pii_middleware.core.detector import EntityType

__all__ = [
    "MASTER_KEY_ENV_VAR",
    "InvalidMasterKeyError",
    "MappingInfo",
    "TokenNotFoundError",
    "Vault",
    "VaultIntegrityError",
]


@dataclass(frozen=True, slots=True)
class MappingInfo:
    """One row of the vault, for the observability frontend's vault inspector.

    Deliberately carries no plaintext and no nonce -- only what BRIEF SS5's
    vault-inspector pane displays by default (token, type, ciphertext,
    timestamp). Revealing the value behind a token is a separate, explicit
    action (``Vault.resolve``), never implied by listing.
    """

    token: str
    entity_type: str
    ciphertext_b64: str
    created_at: int
    session_id: str


#: Environment variable carrying the master key: base64-encoded, 32 raw bytes
#: (256 bits) for AES-256-GCM. Never read from the repository or a default.
MASTER_KEY_ENV_VAR = "VEIL_VAULT_KEY"

_KEY_LENGTH_BYTES = 32
_NONCE_LENGTH_BYTES = 12


class InvalidMasterKeyError(ValueError):
    """Raised when the master key is missing or malformed. Never carries the key."""


class TokenNotFoundError(KeyError):
    """Raised when a token has no mapping in this vault's session."""


class VaultIntegrityError(ValueError):
    """Raised when a mapping's ciphertext fails AEAD authentication.

    Means the row's ciphertext/nonce/AAD no longer match what was encrypted
    under this token — e.g. rows were swapped or the database was tampered
    with outside the vault. Never carries plaintext.
    """


def _load_master_key(raw: str | None) -> bytes:
    if raw is None or raw == "":
        raise InvalidMasterKeyError(
            f"{MASTER_KEY_ENV_VAR} is not set. Set it to a base64-encoded "
            f"{_KEY_LENGTH_BYTES}-byte key before opening the vault."
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise InvalidMasterKeyError(f"{MASTER_KEY_ENV_VAR} is not valid base64.") from exc
    if len(key) != _KEY_LENGTH_BYTES:
        raise InvalidMasterKeyError(
            f"{MASTER_KEY_ENV_VAR} must decode to {_KEY_LENGTH_BYTES} bytes, got {len(key)}."
        )
    return key


def _normalize(value: str, entity_type: EntityType) -> str:
    """Canonicalize a value so equivalent-but-differently-formatted inputs collapse.

    Case and incidental whitespace/punctuation must not fragment a single
    real-world value across multiple tokens, since that breaks the
    correlation guarantee the vault exists to provide.
    """
    stripped = value.strip()
    if entity_type is EntityType.EMAIL:
        return stripped.lower()
    if entity_type in (EntityType.DNI, EntityType.NIE, EntityType.IBAN):
        return re.sub(r"[\s-]", "", stripped).upper()
    if entity_type in (EntityType.PHONE, EntityType.CARD):
        return re.sub(r"[\s()-]", "", stripped)
    return stripped


class Vault:
    """Per-session encrypted token <-> value store backed by SQLite.

    Bound to one ``session_id`` at construction; a process handling multiple
    sessions opens multiple ``Vault`` instances over the same ``db_path``.
    """

    def __init__(
        self,
        db_path: str | Path,
        session_id: str,
        *,
        master_key: bytes | None = None,
    ) -> None:
        self._session_id = session_id
        self._key = (
            master_key
            if master_key is not None
            else _load_master_key(os.environ.get(MASTER_KEY_ENV_VAR))
        )
        if len(self._key) != _KEY_LENGTH_BYTES:
            raise InvalidMasterKeyError(
                f"master_key must be {_KEY_LENGTH_BYTES} bytes, got {len(self._key)}."
            )
        # Independent subkeys for AES-GCM and HMAC, derived via HKDF rather
        # than reusing one master key across two different primitives — no
        # known break today, but domain separation means a future weakness
        # in one primitive can't be leveraged against the other.
        self._aead = AESGCM(self._derive_subkey(b"veil-vault-aesgcm"))
        self._hmac_key = self._derive_subkey(b"veil-vault-hmac")
        self._conn = sqlite3.connect(db_path)
        self._create_schema()

    def _derive_subkey(self, info: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(), length=_KEY_LENGTH_BYTES, salt=None, info=info
        ).derive(self._key)

    def _create_schema(self) -> None:
        # PRIMARY KEY is (session_id, token) rather than a bare `token` column:
        # two different sessions sharing one db_path each start their own
        # per-entity-type counter at 1, so `PII_EMAIL_0001` is only unique
        # within a session, never across the whole table.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mappings (
                token       TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                lookup_hmac BLOB NOT NULL,
                ciphertext  BLOB NOT NULL,
                nonce       BLOB NOT NULL,
                session_id  TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                PRIMARY KEY (session_id, token)
            )
            """
        )
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_lookup
            ON mappings(session_id, lookup_hmac)
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS counters (
                session_id  TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                next_seq    INTEGER NOT NULL,
                PRIMARY KEY (session_id, entity_type)
            )
            """
        )
        self._conn.commit()

    def _lookup_hmac(self, normalized: str, entity_type: EntityType) -> bytes:
        # entity_type is mixed into the HMAC input so a raw value shared by
        # two different entity types never collides on the same lookup row.
        message = f"{entity_type.value}:{normalized}".encode()
        return hmac.new(self._hmac_key, message, hashlib.sha256).digest()

    def _next_seq(self, entity_type: EntityType) -> int:
        cur = self._conn.execute(
            "SELECT next_seq FROM counters WHERE session_id = ? AND entity_type = ?",
            (self._session_id, entity_type.value),
        )
        row = cur.fetchone()
        seq = int(row[0]) if row is not None else 1
        self._conn.execute(
            """
            INSERT INTO counters (session_id, entity_type, next_seq)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id, entity_type) DO UPDATE SET next_seq = excluded.next_seq
            """,
            (self._session_id, entity_type.value, seq + 1),
        )
        return seq

    def _aad(self, entity_type: EntityType, token: str) -> bytes:
        # Binds the ciphertext to the row it belongs to: session, entity
        # type and token. Without this, AES-GCM authenticates only the
        # value bytes, so swapping (ciphertext, nonce) between two rows in
        # the mappings table would decrypt "successfully" and silently
        # return one customer's value under another customer's token.
        return f"{self._session_id}:{entity_type.value}:{token}".encode()

    def get_or_create_token(self, value: str, entity_type: EntityType) -> str:
        """Return the stable token for ``value``, creating one if needed.

        The same normalized value always returns the same token within this
        vault's session; plaintext is never written to any column.
        """
        normalized = _normalize(value, entity_type)
        lookup_hmac = self._lookup_hmac(normalized, entity_type)

        cur = self._conn.execute(
            "SELECT token FROM mappings WHERE session_id = ? AND lookup_hmac = ?",
            (self._session_id, lookup_hmac),
        )
        row = cur.fetchone()
        if row is not None:
            return str(row[0])

        seq = self._next_seq(entity_type)
        token = f"PII_{entity_type.value}_{seq:04d}"
        nonce = os.urandom(_NONCE_LENGTH_BYTES)
        ciphertext = self._aead.encrypt(nonce, value.encode(), self._aad(entity_type, token))

        self._conn.execute(
            """
            INSERT INTO mappings
                (token, entity_type, lookup_hmac, ciphertext, nonce, session_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                entity_type.value,
                lookup_hmac,
                ciphertext,
                nonce,
                self._session_id,
                int(time.time()),
            ),
        )
        self._conn.commit()
        return token

    def resolve(self, token: str) -> str:
        """Return the original plaintext value behind ``token``.

        Raises ``TokenNotFoundError`` if the token does not exist in this
        vault's session — including a token that belongs to a different
        session, which must never resolve here. Raises
        ``VaultIntegrityError`` if the stored ciphertext fails AEAD
        authentication against this row's identity (session, entity type,
        token) — evidence of a swapped or tampered row, never surfaced with
        any plaintext.
        """
        cur = self._conn.execute(
            """
            SELECT ciphertext, nonce, entity_type FROM mappings
            WHERE session_id = ? AND token = ?
            """,
            (self._session_id, token),
        )
        row = cur.fetchone()
        if row is None:
            raise TokenNotFoundError(token)
        ciphertext: bytes = bytes(row[0])
        nonce: bytes = bytes(row[1])
        entity_type = EntityType(row[2])
        try:
            plaintext = self._aead.decrypt(nonce, ciphertext, self._aad(entity_type, token))
        except InvalidTag as exc:
            raise VaultIntegrityError(
                f"ciphertext for token {token!r} failed authentication"
            ) from exc
        return plaintext.decode()

    def list_mappings(self) -> list[MappingInfo]:
        """Return every mapping in this vault's session, newest first.

        Read-only, and never decrypts -- ``ciphertext_b64`` is the encrypted
        bytes as stored, unusable without the master key. This is the only
        query the vault inspector's default (non-reveal) view is allowed to
        use; ``resolve()`` remains the sole path to a decrypted value.
        """
        cur = self._conn.execute(
            """
            SELECT token, entity_type, ciphertext, created_at FROM mappings
            WHERE session_id = ?
            ORDER BY created_at DESC, token DESC
            """,
            (self._session_id,),
        )
        return [
            MappingInfo(
                token=str(row[0]),
                entity_type=str(row[1]),
                ciphertext_b64=base64.b64encode(bytes(row[2])).decode(),
                created_at=int(row[3]),
                session_id=self._session_id,
            )
            for row in cur.fetchall()
        ]

    def list_all_mappings(self) -> list[MappingInfo]:
        """Return every mapping in this ``db_path``, across every session, newest first.

        Same read-only, never-decrypts contract as ``list_mappings()`` -- the
        only difference is the ``WHERE session_id = ?`` filter is dropped.
        The session scoping on ``mappings`` is an application convention
        (token stability per session), not a database-level partition, so
        any open connection to this ``db_path`` can already see every row;
        this just doesn't filter them out. Exists because the observability
        panel's vault inspector must show tokens minted under *any* session
        that ever hit this gateway process's ``db_path`` -- not only
        whichever ``session_id`` happens to be "current" (the gateway's
        fallback id is a fresh UUID per process restart, so without this a
        restart made every earlier session's tokens silently invisible in
        the panel, even though the rows were still sitting in the file).
        """
        cur = self._conn.execute(
            """
            SELECT token, entity_type, ciphertext, created_at, session_id FROM mappings
            ORDER BY created_at DESC, token DESC
            """
        )
        return [
            MappingInfo(
                token=str(row[0]),
                entity_type=str(row[1]),
                ciphertext_b64=base64.b64encode(bytes(row[2])).decode(),
                created_at=int(row[3]),
                session_id=str(row[4]),
            )
            for row in cur.fetchall()
        ]

    def purge_expired(self, ttl_seconds: int, *, now: int | None = None) -> int:
        """Delete mappings older than ``ttl_seconds``. Returns the count deleted.

        Purged sequence numbers are never reassigned: the ``counters`` table
        is untouched, so the next value of the same entity type in this
        session always gets a strictly higher ``NNNN``.
        """
        cutoff = (now if now is not None else int(time.time())) - ttl_seconds
        cur = self._conn.execute(
            "DELETE FROM mappings WHERE session_id = ? AND created_at < ?",
            (self._session_id, cutoff),
        )
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Vault:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
