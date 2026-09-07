"""Substitution and reversal of ``PII_<TYPE>_<NNNN>`` tokens.

Operates over plain text and nested structures (dicts, lists, JSON payloads),
replacing detected spans with tokens on the way out and restoring the original
values on the way back in.
"""

from __future__ import annotations

import re
from typing import TypeAlias

from veil_pii_middleware.core.detector import detect_regex, merge_spans
from veil_pii_middleware.core.vault import Vault

__all__ = ["JSONValue", "TokenFormatOverflowError", "detokenize", "tokenize"]

#: A JSON-shaped value: a scalar, or a dict/list of the same. Only ``str``
#: leaves are ever tokenized/detokenized; every other shape passes through
#: untouched, recursing into dicts and lists without touching keys.
JSONValue: TypeAlias = str | int | float | bool | None | dict[str, "JSONValue"] | list["JSONValue"]

# Entity-type-agnostic on purpose: matching by shape and letting
# ``Vault.resolve`` decide whether a token was actually issued keeps this
# forward-compatible with entity types a future LLM layer introduces (e.g.
# ``PERSON``), with no change needed here. Anchored to exactly four digits:
# ``Vault.get_or_create_token`` formats its sequence number with ``:04d``,
# which zero-*pads* to four digits but does not cap the count, so a session
# that mints a 10000th token of one entity type produces a five-digit
# token. An unanchored-width pattern here would then match only the first
# four digits of that token and could resolve to a *different*, already-
# issued token's value -- silently returning the wrong customer's PII
# instead of failing. ``tokenize`` below refuses to emit such a token in
# the first place, so this pattern's four-digit assumption stays a true
# invariant of everything this module ever writes into text.
_TOKEN_PATTERN = re.compile(r"PII_[A-Z]+_\d{4}")


class TokenFormatOverflowError(ValueError):
    """Raised when a minted token no longer fits the fixed ``PII_<TYPE>_NNNN`` width.

    ``detokenize``'s token-matching regex assumes exactly four digits; a
    wider sequence number would be ambiguous to parse back out of free text
    and could silently resolve to a different token's value instead of
    failing. Carries the token string (not a PII value) for diagnosis.
    """


def tokenize(value: JSONValue, vault: Vault) -> JSONValue:
    """Replace every PII span ``detect_regex`` finds in ``value`` with a vault token.

    Recurses into dicts and lists, tokenizing only string leaves; keys and
    non-string scalars are returned unchanged. Does not mutate ``value``.
    """
    if isinstance(value, str):
        return _tokenize_text(value, vault)
    if isinstance(value, dict):
        return {key: tokenize(item, vault) for key, item in value.items()}
    if isinstance(value, list):
        return [tokenize(item, vault) for item in value]
    return value


def _tokenize_text(text: str, vault: Vault) -> str:
    # detect_regex() already merges its own candidates, but this still routes
    # through merge_spans() explicitly: that is where a future LLM-layer span
    # list unions in, and this function must not assume regex is the only
    # source of spans.
    spans = merge_spans(detect_regex(text))
    # Right-to-left by start offset: substituting a token of a different
    # length than the original span must not invalidate the offsets of spans
    # still waiting to be replaced.
    for span in sorted(spans, key=lambda s: s.start, reverse=True):
        token = vault.get_or_create_token(span.text, span.entity_type)
        if not _TOKEN_PATTERN.fullmatch(token):
            raise TokenFormatOverflowError(
                f"token {token!r} no longer fits the fixed four-digit "
                f"PII_{span.entity_type.value}_NNNN format -- this session has "
                f"minted too many {span.entity_type.value} tokens to stay "
                "unambiguous for detokenize"
            )
        text = text[: span.start] + token + text[span.end :]
    return text


def detokenize(value: JSONValue, vault: Vault) -> JSONValue:
    """Replace every ``PII_<TYPE>_<NNNN>`` substring in ``value`` with its real value.

    Recurses into dicts and lists, detokenizing only string leaves; keys and
    non-string scalars are returned unchanged. Does not mutate ``value``. A
    token this vault's session cannot resolve (unknown, malformed, or minted
    under a different session) fails closed: ``Vault.resolve`` raises
    (``TokenNotFoundError`` or ``VaultIntegrityError``) and that exception
    propagates here uncaught, aborting the whole call rather than leaving the
    literal token text in the output.
    """
    if isinstance(value, str):
        return _TOKEN_PATTERN.sub(lambda match: vault.resolve(match.group(0)), value)
    if isinstance(value, dict):
        return {key: detokenize(item, vault) for key, item in value.items()}
    if isinstance(value, list):
        return [detokenize(item, vault) for item in value]
    return value
