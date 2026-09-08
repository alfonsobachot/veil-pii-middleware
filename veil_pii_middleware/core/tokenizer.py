"""Substitution and reversal of ``PII_<TYPE>_<NNNN>`` tokens.

Operates over plain text and nested structures (dicts, lists, JSON payloads),
replacing detected spans with tokens on the way out and restoring the original
values on the way back in.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from veil_pii_middleware.core.audit import AuditLog
from veil_pii_middleware.core.detector import (
    LLM_BASE_URL_ENV_VAR,
    LLM_MODEL_ENV_VAR,
    LLMClient,
    Span,
    detect,
)
from veil_pii_middleware.core.vault import TokenNotFoundError, Vault, VaultIntegrityError

__all__ = [
    "JSONValue",
    "TokenFormatOverflowError",
    "UNRESOLVED_TOKEN_PLACEHOLDER",
    "IncrementalDetokenizer",
    "detokenize",
    "detokenize_lenient",
    "tokenize",
]

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


def _default_llm_client() -> LLMClient | None:
    """Build an ``LLMClient`` from the environment, or ``None`` if unconfigured.

    Both ``LLM_BASE_URL_ENV_VAR`` and ``LLM_MODEL_ENV_VAR`` must be set and
    non-empty; otherwise the LLM layer is off, matching today's regex-only
    behavior exactly -- no partial attempt at a network call with half a
    configuration. The base URL is never defaulted to a fixed host/port.
    """
    base_url = os.environ.get(LLM_BASE_URL_ENV_VAR, "").strip()
    model = os.environ.get(LLM_MODEL_ENV_VAR, "").strip()
    if not base_url or not model:
        return None
    return LLMClient(base_url, model)


@dataclass(frozen=True, slots=True)
class _TokenizeContext:
    """Bundles ``tokenize()``'s optional side-channels for the recursive walk.

    Kept as one object rather than five positional parameters threaded
    through ``_tokenize``/``_tokenize_text`` -- both audit logging and span
    collection are independent, optional features layered onto the same
    substitution pass, and grouping them keeps that recursion readable.
    """

    llm_client: LLMClient | None
    audit_log: AuditLog | None
    adapter: str | None
    span_sink: list[Span] | None
    skip: Callable[[str], bool] | None
    exclude: Callable[[str], list[tuple[int, int]]] | None


def tokenize(
    value: JSONValue,
    vault: Vault,
    *,
    llm_client: LLMClient | None = None,
    audit_log: AuditLog | None = None,
    adapter: str | None = None,
    span_sink: list[Span] | None = None,
    skip: Callable[[str], bool] | None = None,
    exclude: Callable[[str], list[tuple[int, int]]] | None = None,
) -> JSONValue:
    """Replace every PII span ``detect()`` finds in ``value`` with a vault token.

    Recurses into dicts and lists, tokenizing only string leaves; keys and
    non-string scalars are returned unchanged. Does not mutate ``value``.

    ``llm_client`` is used for the whole call if supplied (recursion does not
    rebuild it per string leaf); when omitted, one is built once from the
    environment (``LLM_BASE_URL_ENV_VAR``/``LLM_MODEL_ENV_VAR``) -- ``None``
    if either is unset, in which case this call is regex-only, identical to
    before the LLM layer existed. A client built internally here is closed
    before returning; one supplied by the caller is left open for the caller
    to manage.

    ``audit_log``/``adapter`` are optional and independent of the LLM wiring:
    when ``audit_log`` is supplied, ``adapter`` must be a non-empty string,
    and one ``AuditLog.record(...)`` call is made per span substituted,
    across every string leaf ``value`` contains. Omitting ``audit_log`` (the
    default, and every pre-existing caller) writes nothing -- identical
    behavior to before this parameter existed.

    ``span_sink``, if supplied, has every span this call detects appended to
    it (in the order found, before substitution), across every string leaf --
    for a caller (the gateway's observability panel) that needs the same
    spans this call already computed, without running a second, duplicate
    detection pass. Omitting it (the default) costs nothing extra.

    ``skip``, if supplied, is called once per string leaf before any
    detection runs; when it returns ``True`` for a leaf, that leaf is
    returned completely unscanned -- no regex/LLM call, no audit entry, no
    span in ``span_sink``, no substitution. For excluding known
    protocol/metadata content a specific transport injects (e.g. a reserved
    system-prompt header the gateway adapter must pass through byte-for-byte
    for the upstream API to accept the request) -- never for excluding
    *user* content, which this module has no way to distinguish from
    metadata on its own and must keep scanning by default. The predicate
    itself carries no knowledge of any specific transport; only the caller
    supplying it does, keeping this module transport-agnostic.

    ``exclude``, if supplied, is called once per string leaf (only when that
    leaf was not already dropped entirely by ``skip``) and returns a list of
    ``(start, end)`` character ranges within that leaf's *own* text; any
    detected span overlapping one of these ranges is discarded before
    substitution -- unlike ``skip``, the rest of the leaf is still scanned
    and substituted normally. For excluding a known non-user substring that
    is *embedded inside* otherwise-real content in the same leaf (e.g. a
    transport-injected metadata block interleaved with the human's own
    typed text) -- ``skip`` cannot do this because it is all-or-nothing per
    leaf, and skipping the whole leaf would also stop scanning the real
    content next to it. Same transport-agnostic contract as ``skip``: this
    module has no opinion on what counts as excludable, only the caller does.
    """
    if audit_log is not None and not adapter:
        raise ValueError("adapter must be a non-empty string when audit_log is supplied")
    client = llm_client if llm_client is not None else _default_llm_client()
    context = _TokenizeContext(
        llm_client=client,
        audit_log=audit_log,
        adapter=adapter,
        span_sink=span_sink,
        skip=skip,
        exclude=exclude,
    )
    try:
        return _tokenize(value, vault, context)
    finally:
        if llm_client is None and client is not None:
            client.close()


def _tokenize(value: JSONValue, vault: Vault, context: _TokenizeContext) -> JSONValue:
    if isinstance(value, str):
        return _tokenize_text(value, vault, context)
    if isinstance(value, dict):
        return {key: _tokenize(item, vault, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_tokenize(item, vault, context) for item in value]
    return value


def _tokenize_text(text: str, vault: Vault, context: _TokenizeContext) -> str:
    if context.skip is not None and context.skip(text):
        return text
    # Routes through detect() rather than assuming regex is the only source
    # of spans -- this is where the LLM layer's spans union in, gated by
    # whether llm_client is set (see tokenize()'s docstring).
    spans = detect(text, llm_client=context.llm_client)
    if context.exclude is not None:
        excluded_ranges = context.exclude(text)
        if excluded_ranges:
            spans = [
                span
                for span in spans
                if not any(
                    span.start < r_end and span.end > r_start for r_start, r_end in excluded_ranges
                )
            ]
    if context.span_sink is not None:
        context.span_sink.extend(sorted(spans, key=lambda s: s.start))
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
        if context.audit_log is not None and context.adapter is not None:
            context.audit_log.record(
                token=token,
                entity_type=span.entity_type,
                detector=span.detector,
                offset=span.start,
                value=span.text,
                adapter=context.adapter,
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


#: Shown in place of a token that fails to resolve mid-stream (see
#: ``IncrementalDetokenizer``). Distinct from ``detokenize``'s fail-closed
#: behavior: a resolve failure on the response leg leaks no PII either way
#: (the token itself is not PII), so this is a display/legibility choice,
#: not a security one -- CLAUDE.md's fail-closed doctrine governs the
#: *outbound* leg (``tokenize``) and is unrelated to this constant.
UNRESOLVED_TOKEN_PLACEHOLDER = "[unresolved token]"

# A generous upper bound on how long an entity-type name inside a token can
# be before this is clearly not building toward a real token after all.
# Every entity type minted by this codebase today (EMAIL, PHONE, DNI, NIE,
# IBAN, CARD) is well under this; it exists only to stop an ordinary long
# run of capital letters (which is not, and will never become, a token)
# from being held back indefinitely -- the trailing ``[A-Z]`` run in
# ``_PENDING_TOKEN_TAIL`` below is otherwise unbounded.
_MAX_ENTITY_TYPE_LEN = 32

# Matches a suffix of a buffer that could still be the start of a
# `PII_<TYPE>_<NNNN>` token if more text arrives -- i.e. everything
# `_TOKEN_PATTERN` could eventually match, at every stage of being typed
# out one character at a time. Anchored to the end of the string; since
# `re.search` returns the leftmost (hence longest) successful match among
# these alternatives, this always finds the longest such pending suffix.
# A *complete* four-digit token at the very end of the buffer does NOT
# match here (`\d{1,3}$` cannot consume all four digits contiguously from
# the `PII_` anchor) -- correctly, since a complete token is not "pending",
# it is ready to resolve immediately.
_PENDING_TOKEN_TAIL = re.compile(
    r"P$|PI$|PII$|PII_$"
    rf"|PII_[A-Z]{{1,{_MAX_ENTITY_TYPE_LEN}}}$"
    rf"|PII_[A-Z]{{1,{_MAX_ENTITY_TYPE_LEN}}}_$"
    rf"|PII_[A-Z]{{1,{_MAX_ENTITY_TYPE_LEN}}}_\d{{1,3}}$"
)


def _safe_flush_boundary(buffer: str) -> int:
    """Return the index up to which ``buffer`` is safe to emit right now.

    Everything before the index is guaranteed to contain no partial token
    prefix; everything from the index onward must be retained because more
    text could still arrive and complete it into a real token.
    """
    match = _PENDING_TOKEN_TAIL.search(buffer)
    return match.start() if match else len(buffer)


def _resolve_or_placeholder(text: str, vault: Vault) -> str:
    """Like ``detokenize`` over a single string, but never raises.

    A token this vault's session cannot resolve is replaced with
    ``UNRESOLVED_TOKEN_PLACEHOLDER`` instead of propagating
    ``TokenNotFoundError``/``VaultIntegrityError`` -- appropriate here
    because this runs against a partial, already-in-flight response
    stream where raising would either corrupt the chunked framing or
    require silently truncating an otherwise-fine response (see
    ``IncrementalDetokenizer``).

    The two exceptions are not equally routine, though: ``TokenNotFoundError``
    is an expected, benign case (e.g. a token minted under a different
    gateway process/session), but ``VaultIntegrityError`` means a row's
    AEAD authentication failed -- CLAUDE.md's vault doctrine treats that as
    evidence of tampering or corruption, not a shrug. Silently collapsing
    both into the same placeholder with zero visibility would discard a
    security-relevant signal the vault was specifically built to surface, so
    the integrity failure alone is also logged to stderr (token id only,
    never plaintext or the ciphertext).
    """

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        try:
            return vault.resolve(token)
        except VaultIntegrityError:
            print(
                f"veil: vault integrity check failed for token {token} "
                "(possible tampering or corruption) -- substituting placeholder",
                file=sys.stderr,
            )
            return UNRESOLVED_TOKEN_PLACEHOLDER
        except TokenNotFoundError:
            return UNRESOLVED_TOKEN_PLACEHOLDER

    return _TOKEN_PATTERN.sub(_replace, text)


class IncrementalDetokenizer:
    """Chunk-boundary-safe ``detokenize`` for a single streaming text source.

    Anthropic's streaming responses can split a token's text across two
    separate deltas (confirmed live against the real API, mission-8); this
    class buffers only what might be an in-progress token and resolves
    everything else immediately, so a caller relaying a stream chunk by
    chunk never emits a partial or literal token.

    One instance per independent stream of text -- e.g. one Anthropic
    ``content_block_index``. A token's text never spans two content
    blocks, so an adapter must not share one instance across blocks.
    """

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        self._pending = ""

    def feed(self, chunk: str) -> str:
        """Feed newly-arrived text; return the portion now safe to emit."""
        buffer = self._pending + chunk
        boundary = _safe_flush_boundary(buffer)
        safe, self._pending = buffer[:boundary], buffer[boundary:]
        return _resolve_or_placeholder(safe, self._vault)

    def flush(self) -> str:
        """Call once at end of stream to emit whatever text is still held back.

        Whatever remains was never completed into a token (a real token would
        already have been resolved and emitted by ``feed``), so it is
        returned as plain text -- never the unresolved-token placeholder.
        """
        remaining, self._pending = self._pending, ""
        return remaining


def detokenize_lenient(value: JSONValue, vault: Vault) -> JSONValue:
    """Like ``detokenize``, but a token this vault cannot resolve becomes
    ``UNRESOLVED_TOKEN_PLACEHOLDER`` instead of raising.

    For a response the model already produced -- which must reach the client
    one way or another (e.g. a buffered, non-streaming gateway response) --
    discarding an otherwise-fine response over one bad token serves no
    purpose ``detokenize``'s fail-closed doctrine exists for: that doctrine
    stops raw PII from ever reaching an external API on the *outbound* leg,
    and a resolve failure here leaks no PII either way (the token itself is
    not PII).

    Recurses into dicts and lists exactly like ``detokenize``; keys and
    non-string scalars are returned unchanged. Does not mutate ``value``.
    """
    if isinstance(value, str):
        return _resolve_or_placeholder(value, vault)
    if isinstance(value, dict):
        return {key: detokenize_lenient(item, vault) for key, item in value.items()}
    if isinstance(value, list):
        return [detokenize_lenient(item, vault) for item in value]
    return value
