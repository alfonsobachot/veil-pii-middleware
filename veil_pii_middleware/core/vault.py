"""Encrypted SQLite store of token <-> value mappings, one set per session.

Values are held as AES-GCM ciphertext and located by HMAC-SHA256 of the
normalized value, so lookups never need plaintext. The same value always
yields the same token within a session.
"""
