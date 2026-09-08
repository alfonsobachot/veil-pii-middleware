"""Reverse-HTTP-proxy gateway: whole-payload tokenization at ``ANTHROPIC_BASE_URL``.

Listens locally and stands in for ``https://api.anthropic.com``. For every
request: reads the full body, and if it is JSON, tokenizes every string leaf
via ``core/tokenizer.py::tokenize()`` against a per-process
``core/vault.py::Vault`` before forwarding it upstream with an
otherwise-untouched method/path/headers (including auth).

The response is rewritten on the way back so a token the model echoes (e.g.
``PII_EMAIL_0001``) resolves to its real value before it reaches the client
(mission-8; mission-7 relayed the response untouched as an explicit,
documented scope cut). Two response shapes are handled, both via
``core/tokenizer.py`` -- this module never re-implements detection or
substitution, only wires ``core/`` into the transport:

- **Streaming (``text/event-stream``)**: each ``content_block_delta`` event's
  text-bearing field is fed through a ``core.tokenizer.IncrementalDetokenizer``
  keyed by ``content_block_index``, live-verified against the real Anthropic
  API (mission-8 spike) to confirm the event/delta shapes below and that an
  SSE ``data:`` line is always one complete JSON event -- a token can only
  split across the *text* of two such events, never mid-JSON. Any leftover
  buffered text is flushed into a synthetic extra delta event right before
  that block's ``content_block_stop``, so nothing is ever silently dropped.
- **Buffered JSON (anything else that parses as JSON)**: detokenized in one
  shot via ``core.tokenizer.detokenize_lenient()``.
- **Anything else** (non-JSON, non-SSE): relayed byte-for-byte untouched,
  same as mission-7's original behavior -- there is nothing to safely
  detokenize about content with no recognized token-bearing structure.

A token that fails to resolve (e.g. minted under a different gateway
process/session) is replaced with ``core.tokenizer.UNRESOLVED_TOKEN_PLACEHOLDER``
rather than raising or corrupting the stream -- a resolve failure here leaks
no PII either way (the token itself is not PII), so this is a display
choice, not the fail-closed doctrine that governs the *outbound* leg below.

Fails closed on the request leg, matching every other adapter's doctrine: a
request body that is not valid JSON, or a tokenization/vault failure, aborts
the request with an error response of the gateway's own rather than
forwarding the original bytes upstream unscanned.

Keyed per request by the real Claude Code ``session_id``, read from the
``X-Claude-Code-Session-Id`` request header (confirmed present on every
outbound Claude Code request by mission-7's live spike) -- the same id
``PreToolUse``/``PostToolUse`` already use from their own hook payload's
``session_id``. This is what lets a token minted by the gateway resolve
under a hook, and vice versa (mission-9/BRIEF item 10c): before this, the
gateway minted its own ``gateway-<uuid>`` once per process, unresolvable by
either hook. Each request already opens its own ``Vault(...)`` (see
``_tokenize_body``, ``_relay_sse_response``, ``_relay_buffered_response``),
so keying by a per-request header value needed no change to ``Vault``'s
lifetime -- only to which ``session_id`` string those existing calls pass
in.

If the header is missing, empty, or whitespace-only -- unconfirmed whether
every ``ANTHROPIC_BASE_URL``-speaking client sends it, not just Claude Code
-- the gateway falls back to a UUID generated once at process startup
(``_new_session_id()``), logging a stderr warning. This is not the
fail-closed doctrine that governs the outbound tokenization leg below: no
raw PII is at stake either way, only cross-adapter correlation for that one
request. Two concurrent gateway processes still do not share a token
vocabulary with each other in the no-header/fallback case; that remains an
accepted scope limitation.

Also serves BRIEF SS5's observability panel under the reserved
``/veil-panel`` path prefix, intercepted before the proxy pass-through
above: a static, unbuilt frontend page; an SSE tail of the shared
``core/audit.py`` JSONL (written by this process and by the hook/`/veil`
adapters, so the panel sees every adapter's activity); a *separate*,
in-memory-only SSE stream of pipeline-stage-transition events
(``PIPELINE_STAGES``) this process publishes for its own request/response
cycles -- structurally distinct from the audit log because it carries the
actual request/response text BRIEF's four panes need to show, which the
audit log deliberately never does; and small JSON endpoints for the vault
inspector (list, and an explicit reveal-one-token action) and `demo/eval.py`
metrics. None of this adds authentication beyond the gateway's existing
``127.0.0.1``-only bind.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit

import httpx

from veil_pii_middleware.core.audit import AuditLog
from veil_pii_middleware.core.detector import Span
from veil_pii_middleware.core.tokenizer import (
    IncrementalDetokenizer,
    JSONValue,
    detokenize_lenient,
    tokenize,
)
from veil_pii_middleware.core.vault import TokenNotFoundError, Vault, VaultIntegrityError
from veil_pii_middleware.demo import eval as eval_module

__all__ = ["SESSION_ID_HEADER", "make_handler", "main"]

#: Same fixed, git-ignored vault location every adapter uses.
VAULT_DB_PATH = Path(".veil") / "vault.db"

#: Same fixed, git-ignored audit log every adapter (gateway, hooks, `/veil`)
#: appends to -- one shared file is what lets the panel's audit-log SSE tail
#: show every adapter's activity, not just the gateway's own.
AUDIT_LOG_PATH = Path(".veil") / "audit.jsonl"

#: Reserved path prefix for the observability panel's own HTTP surface
#: (BRIEF SS5) -- checked before the unconditional upstream-proxy pass-through,
#: so a panel request never reaches the real API and a real API path can
#: never collide with it (no Anthropic endpoint starts with this segment).
_PANEL_PREFIX: Final = "/veil-panel"

#: How often the audit-log SSE tail polls for new lines when caught up to
#: end-of-file. Short enough to feel live in a demo, long enough not to spin.
_AUDIT_TAIL_POLL_SECONDS: Final = 0.25

#: Pipeline-stage names published on the in-memory SSE stream, in the order
#: BRIEF SS5's requested flow diagram lists them. Structurally separate from
#: AuditLog's JSONL: these carry the actual request/response text (needed to
#: render BRIEF's four panes) and are never persisted -- see the mission's
#: Intake Grill decision on why the two streams do not share a schema.
PIPELINE_STAGES: Final = (
    "received",
    "tokenized",
    "forwarded",
    "response_received",
    "detokenized",
    "displayed",
)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


class PipelineEventBus:
    """In-memory pub/sub for pipeline-stage-transition events.

    One instance per gateway process. A subscriber (one per open
    ``/veil-panel/pipeline-stream`` connection) gets its own bounded queue;
    a slow consumer has its oldest pending event dropped rather than
    blocking the request thread that publishes -- this is a best-effort live
    view, not a delivery-guaranteed log (the audit log is that, and is a
    separate stream entirely).
    """

    def __init__(self, maxsize: int = 500) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    subscriber.get_nowait()
                with contextlib.suppress(queue.Full):
                    subscriber.put_nowait(event)


def _publish_stage(
    bus: PipelineEventBus,
    request_id: str,
    stage: str,
    text: str,
    *,
    spans: list[Span] | None = None,
) -> None:
    """Publish one pipeline-stage event. ``spans`` is only meaningful for ``"received"``.

    Entity type/detector are the only span metadata a stage event carries --
    the offsets index into this same event's own ``text``, so nothing about
    the value itself is duplicated beyond what ``text`` already contains.
    """
    event: dict[str, Any] = {
        "request_id": request_id,
        "stage": stage,
        "timestamp": datetime.now(UTC).isoformat(),
        "text": text,
    }
    if spans is not None:
        event["spans"] = [
            {
                "start": span.start,
                "end": span.end,
                "entity_type": span.entity_type.value,
                "detector": span.detector.value,
            }
            for span in spans
        ]
    bus.publish(event)


#: Directory containing the static, unbuilt observability panel page (BRIEF
#: SS5) -- plain HTML/CSS/vanilla-loaded React, no bundler, served directly
#: by this process so there is no CORS to fight (see module docstring).
_FRONTEND_DIR: Final = Path(__file__).resolve().parent.parent.parent / "frontend"
_PANEL_HTML_PATH: Final = _FRONTEND_DIR / "panel.html"


def _query_param(path: str, name: str) -> str | None:
    query = urlsplit(path).query
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == name:
            return unquote(value)
    return None


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _write_html(handler: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _write_sse_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()


#: Overridable only for tests, which stand in a fake upstream in place of
#: the real Anthropic API -- never meant to be pointed anywhere else in
#: normal use.
UPSTREAM_ENV_VAR = "VEIL_GATEWAY_UPSTREAM"
_DEFAULT_UPSTREAM = "https://api.anthropic.com"

_DEFAULT_PORT = 8787

# Headers that describe *this* hop's framing (connection lifecycle, body
# length/encoding) rather than the payload itself -- recomputed or dropped
# on both the request and response side rather than forwarded verbatim,
# since this gateway re-frames every body it relays (tokenized request,
# rechunked response) instead of passing the original wire bytes through.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}


def _new_session_id() -> str:
    return f"gateway-{uuid.uuid4()}"


#: Real Claude Code session id, sent on every outbound request (mission-7's
#: live spike). Reading it lets the gateway key its ``Vault`` calls the same
#: way ``PreToolUse``/``PostToolUse`` already key theirs from the hook
#: payload's ``session_id`` -- see the module docstring.
SESSION_ID_HEADER = "X-Claude-Code-Session-Id"


def _resolve_session_id(handler: BaseHTTPRequestHandler, fallback_session_id: str) -> str:
    """Return the session id to use for this request's ``Vault`` calls.

    Prefers ``SESSION_ID_HEADER`` if present and non-empty after stripping
    whitespace; otherwise falls back to ``fallback_session_id`` (the
    per-process UUID) and logs a stderr warning. Matches the hooks adapters'
    own ``session_id`` validation (non-empty ``str``, no further format
    check) -- this mission does not invent a stricter schema.
    """
    header_value = handler.headers.get(SESSION_ID_HEADER)
    if header_value is not None and header_value.strip():
        return header_value.strip()
    print(
        f"veil gateway: missing or empty {SESSION_ID_HEADER} header, falling back to"
        f" {fallback_session_id} -- this request's tokens will not be resolvable by"
        " PreToolUse/PostToolUse",
        file=sys.stderr,
    )
    return fallback_session_id


# core/vault.py's SQLite-backed counter is read-then-insert with no
# transaction spanning both statements -- fine for every other adapter,
# which only ever has one invocation touching a given session at a time,
# but ThreadingHTTPServer is the first place in this codebase that can run
# two requests for the *same* session concurrently. Without this lock, two
# simultaneous requests minting a token for the same entity type can both
# read the same next_seq and then collide inserting the same token for two
# different values (caught as a generic exception and failed closed, so no
# PII leak -- just an availability/flakiness bug under concurrency). Scoped
# to this process only; does not touch core/vault.py, which this mission's
# non-goals put out of scope.
_vault_lock = threading.Lock()


def _write_error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    # Shaped like an Anthropic API error object so an SDK client on the
    # other end has a recognizable error to surface, rather than an opaque
    # connection failure.
    body = json.dumps(
        {"type": "error", "error": {"type": "veil_gateway_error", "message": message}}
    ).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


#: Prefix of a system-prompt block Claude Code itself injects on every
#: request (confirmed live 2026-09-08, capturing the raw pipeline-stream
#: "received" event) -- never user content. The real Anthropic API rejects
#: the whole request if this block's value is altered in any way ("400:
#: x-anthropic-billing-header is a reserved keyword and may not be used in
#: the system prompt"). Live-observed: the LLM layer mistook the "cc_"
#: prefix in its `cc_version=...; cc_entrypoint=...;` value for a
#: credit-card indicator and tokenized both fields, breaking every
#: Claude-Code-originated request through this gateway with the LLM layer
#: enabled. This carries no PII (a CLI version string and entrypoint name),
#: so excluding it from detection entirely loses no real protection.
_ANTHROPIC_RESERVED_SYSTEM_PREFIX = "x-anthropic-billing-header:"


def _skip_reserved_anthropic_metadata(text: str) -> bool:
    return text.startswith(_ANTHROPIC_RESERVED_SYSTEM_PREFIX)


def _tokenize_body(
    raw_body: bytes, session_id: str, audit_log: AuditLog
) -> tuple[bytes, list[Span]]:
    """Tokenize a JSON request body. Raises on any failure -- callers must fail closed.

    Returns the tokenized body plus every span ``tokenize()`` found (before
    substitution shifted any offsets), so the caller can publish the
    "received" pipeline-stage event with detector-colored span metadata
    without running a second, duplicate detection pass.
    """
    parsed = json.loads(raw_body)
    spans: list[Span] = []
    VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _vault_lock, Vault(VAULT_DB_PATH, session_id) as vault:
        tokenized: JSONValue = tokenize(
            parsed,
            vault,
            audit_log=audit_log,
            adapter="gateway",
            span_sink=spans,
            skip=_skip_reserved_anthropic_metadata,
        )
    return json.dumps(tokenized).encode(), spans


#: content-type prefix that routes a response through the SSE rewrite path
#: rather than the buffered-JSON or untouched-relay paths.
_SSE_CONTENT_TYPE = "text/event-stream"

# Delta shapes confirmed live against the real Anthropic API (mission-8
# spike, see handoff.md) that carry freeform, potentially token-bearing
# display text, mapped to the JSON field holding that text. Substitution
# here is a plain text replacement, safe for `text_delta`/`thinking_delta`
# because the *outer* SSE envelope re-escapes the whole field via
# `json.dumps(obj)` regardless of what characters the resolved value
# contains. `signature_delta` -- also observed live -- is deliberately
# excluded: it carries a cryptographic signature over the model's internal
# state, never freeform text that could echo a token.
#
# `input_json_delta` (`partial_json`, tool-call arguments) is deliberately
# NOT included, despite being string-bearing: its value is a raw fragment of
# a *separate* JSON document that the client concatenates across deltas and
# re-parses, not display text. A plain-text substitution there is only safe
# if the resolved value never contains a `"` or `\` -- true for every entity
# type this codebase mints today (EMAIL/DNI/NIE/IBAN/PHONE/CARD/IP), but not
# guaranteed once a future free-text entity type lands, and this delta type
# was never actually observed in the live spike (no tool call was
# triggered) to validate the assumption against. Rewriting it correctly
# would need escaping-aware substitution scoped to that field specifically;
# deferred rather than shipped half-verified.
_TEXT_BEARING_DELTA_FIELDS = {
    "text_delta": "text",
    "thinking_delta": "thinking",
}


def _split_sse_event(raw_event: str) -> tuple[list[str], dict[str, Any] | None, int]:
    """Split one SSE event's raw text into lines, plus its parsed ``data:`` JSON.

    Returns ``(lines, parsed_data, data_line_index)``; ``parsed_data`` and
    ``data_line_index`` are ``None``/``-1`` if there is no ``data:`` line or
    it is not valid JSON -- callers must treat the event as opaque then.
    """
    lines = raw_event.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("data:"):
            try:
                return lines, json.loads(line[len("data:") :].strip()), i
            except json.JSONDecodeError:
                return lines, None, -1
    return lines, None, -1


class _SseResponseRewriter:
    """Resolves tokens in a live Anthropic SSE response, event by event.

    One instance per HTTP response. Keeps one ``IncrementalDetokenizer`` per
    ``content_block_index`` -- a token's text never spans two content
    blocks (confirmed live: each block starts its own delta sequence) -- and
    flushes a block's leftover buffered text into a synthetic delta event
    immediately before that block's ``content_block_stop``, so nothing a
    block was still holding back at its own end is ever silently dropped.
    """

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        self._detokenizers: dict[Any, IncrementalDetokenizer] = {}
        self._delta_kinds: dict[Any, str] = {}

    def rewrite(self, raw_event: str) -> list[str]:
        """Return the raw SSE event text(s) to forward for one input event.

        Normally a list of one (the input, rewritten in place if it carried
        a token-bearing delta); occasionally two, when a block's leftover
        buffered text must be flushed just before its ``content_block_stop``.
        """
        lines, obj, data_idx = _split_sse_event(raw_event)
        if not isinstance(obj, dict):
            return [raw_event]

        event_type = obj.get("type")
        if event_type == "content_block_delta":
            return [self._rewrite_delta(raw_event, lines, obj, data_idx)]
        if event_type == "content_block_stop":
            return self._flush_block(obj.get("index")) + [raw_event]
        return [raw_event]

    def _rewrite_delta(
        self, raw_event: str, lines: list[str], obj: dict[str, Any], data_idx: int
    ) -> str:
        index = obj.get("index")
        delta = obj.get("delta")
        delta_type = delta.get("type") if isinstance(delta, dict) else None
        if index is None or not isinstance(delta, dict) or not isinstance(delta_type, str):
            return raw_event
        field = _TEXT_BEARING_DELTA_FIELDS.get(delta_type)
        if field is None or not isinstance(delta.get(field), str):
            return raw_event

        detokenizer = self._detokenizers.setdefault(index, IncrementalDetokenizer(self._vault))
        self._delta_kinds[index] = delta_type
        delta[field] = detokenizer.feed(delta[field])
        lines[data_idx] = "data: " + json.dumps(obj)
        return "\n".join(lines)

    def _flush_block(self, index: Any) -> list[str]:
        if index is None or index not in self._detokenizers:
            return []
        leftover = self._detokenizers.pop(index).flush()
        # `_rewrite_delta` always sets `_delta_kinds[index]` in lockstep with
        # `_detokenizers[index]`, and the guard above already confirmed the
        # latter -- so this key is always present here.
        delta_type = self._delta_kinds.pop(index)
        if not leftover:
            return []
        field = _TEXT_BEARING_DELTA_FIELDS[delta_type]
        flush_event = {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": delta_type, field: leftover},
        }
        return ["event: content_block_delta\ndata: " + json.dumps(flush_event)]

    def finish(self) -> list[str]:
        """Flush every block still open at stream end (defensive; should not
        normally trigger -- a well-formed response closes every block it opens).
        """
        events = []
        for index in list(self._detokenizers):
            events.extend(self._flush_block(index))
        return events


def _write_chunk(handler: BaseHTTPRequestHandler, data: bytes) -> None:
    handler.wfile.write(f"{len(data):X}\r\n".encode())
    handler.wfile.write(data)
    handler.wfile.write(b"\r\n")


def _relay_sse_response(
    handler: BaseHTTPRequestHandler, upstream: httpx.Response, vault: Vault
) -> tuple[str, str]:
    """Relay an SSE response, returning ``(raw_text, rewritten_text)`` for the pipeline panel.

    Both are the whole stream joined back together -- not persisted anywhere,
    only published on the in-memory pipeline-stage event bus after this
    function returns (see the caller in ``GatewayHandler._handle``).
    """
    rewriter = _SseResponseRewriter(vault)
    pending = ""
    raw_events: list[str] = []
    out_events: list[str] = []
    for text_chunk in upstream.iter_text():
        pending += text_chunk
        while "\n\n" in pending:
            raw_event, pending = pending.split("\n\n", 1)
            raw_events.append(raw_event)
            for out_event in rewriter.rewrite(raw_event):
                out_events.append(out_event)
                _write_chunk(handler, out_event.encode() + b"\n\n")
    for out_event in rewriter.finish():
        out_events.append(out_event)
        _write_chunk(handler, out_event.encode() + b"\n\n")
    if pending.strip():
        # Genuinely malformed (no trailing blank line) -- forward whatever
        # text is left rather than silently dropping it.
        raw_events.append(pending)
        out_events.append(pending)
        _write_chunk(handler, pending.encode())
    handler.wfile.write(b"0\r\n\r\n")
    return "\n\n".join(raw_events), "\n\n".join(out_events)


def _relay_buffered_response(
    handler: BaseHTTPRequestHandler, upstream: httpx.Response, session_id: str
) -> tuple[str, str]:
    # Fully buffers the body into memory before writing anything, unlike
    # mission-7's original bounded, progressively-streamed relay -- a
    # trade-off accepted here because detokenizing requires the whole JSON
    # value up front, response bodies on this path (anything that isn't
    # `text/event-stream`) are the small, non-streaming Anthropic API
    # replies this gateway targets, and the byte-for-byte non-JSON case just
    # below could stream but is kept in the same function for one code path.
    #
    # Returns ``(raw_text, rewritten_text)`` for the pipeline panel -- see
    # ``_relay_sse_response``'s docstring.
    body = upstream.read()
    raw_text = _decode(body)
    # Every token this codebase ever mints starts with the literal "PII_"
    # (core/tokenizer.py's _TOKEN_PATTERN) -- if that substring is absent,
    # nothing here could possibly resolve, so this skips opening the vault
    # at all, the same way a GET with no request body never touches it.
    if b"PII_" not in body:
        _write_chunk(handler, body)
        handler.wfile.write(b"0\r\n\r\n")
        return raw_text, raw_text
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _write_chunk(handler, body)
        handler.wfile.write(b"0\r\n\r\n")
        return raw_text, raw_text
    VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with Vault(VAULT_DB_PATH, session_id) as vault:
        rewritten: JSONValue = detokenize_lenient(parsed, vault)
    out_bytes = json.dumps(rewritten).encode()
    _write_chunk(handler, out_bytes)
    handler.wfile.write(b"0\r\n\r\n")
    return raw_text, _decode(out_bytes)


def make_handler(
    fallback_session_id: str, client: httpx.Client, upstream_base: str
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to a fallback session id, HTTP client, and upstream base URL.

    ``fallback_session_id`` is used only when a request has no usable
    ``SESSION_ID_HEADER`` -- every request that does carry one is keyed by
    that header's value instead, resolved fresh per request by
    ``_resolve_session_id``.

    Also builds this process's shared ``AuditLog`` and ``PipelineEventBus``
    for the ``/veil-panel`` routes (see the module docstring) -- one of each
    per server instance, closed over by every request the returned handler
    class serves.
    """
    audit_log = AuditLog(AUDIT_LOG_PATH)
    pipeline_bus = PipelineEventBus()
    #: Single-entry mutable box for the most-recently-seen real session id,
    #: so the panel's vault views have a sensible default without the human
    #: having to paste one in -- this is a single-active-debugging-session
    #: demo tool, not a multi-tenant dashboard (see mission Non-goals).
    last_session_id: dict[str, str] = {}

    class GatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            # Tracks whether a status line has already been sent upstream's
            # way to this client. Once true, a failure must never call
            # _write_error -- send_response can only be called once per
            # response; calling it again mid-stream would splice a second
            # HTTP status line/header block into an already-in-flight
            # chunked body and corrupt the framing the client sees. Past
            # that point the only safe move on failure is to stop writing
            # and let the connection close, leaving a truncated body rather
            # than a corrupted one.
            response_started = False
            session_id = _resolve_session_id(self, fallback_session_id)
            last_session_id["value"] = session_id
            request_id = uuid.uuid4().hex
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(length) if length else b""

                forward_body: bytes | None = None
                if raw_body:
                    try:
                        forward_body, spans = _tokenize_body(raw_body, session_id, audit_log)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        _publish_stage(pipeline_bus, request_id, "received", _decode(raw_body))
                        _write_error(
                            self,
                            400,
                            "request body is not valid JSON; blocked rather than forwarded"
                            " unscanned",
                        )
                        return
                    except Exception:
                        _publish_stage(pipeline_bus, request_id, "received", _decode(raw_body))
                        _write_error(
                            self,
                            500,
                            "sanitization failed; request blocked rather than forwarded",
                        )
                        return
                    _publish_stage(
                        pipeline_bus, request_id, "received", _decode(raw_body), spans=spans
                    )
                    _publish_stage(pipeline_bus, request_id, "tokenized", _decode(forward_body))
                    _publish_stage(pipeline_bus, request_id, "forwarded", _decode(forward_body))

                forward_headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in _HOP_BY_HOP and k.lower() != "accept-encoding"
                }
                if forward_body is not None:
                    forward_headers["Content-Length"] = str(len(forward_body))
                # Overrides whatever the real client asked for (a real
                # Claude Code client's Accept-Encoding includes "br") with a
                # value guaranteed decodable by this venv's httpx: gzip/
                # deflate need no optional dependency, but brotli/zstd do
                # (neither is installed here), and httpx cannot decode an
                # encoding it lacks the codec for. Confirmed live: the real
                # Anthropic API honors this and returns gzip -- previously,
                # forwarding the client's own Accept-Encoding verbatim meant
                # the gateway relayed still-Brotli-compressed bytes to the
                # client as if they were plain text the first time this was
                # tested against the real API (mocked-upstream tests never
                # compress anything, so this never surfaced before). Every
                # relay path here still strips Content-Encoding from the
                # response it sends the client (see _HOP_BY_HOP), so this is
                # purely an internal gateway<->upstream implementation
                # detail with no change to what the client itself receives.
                forward_headers["Accept-Encoding"] = "gzip, deflate"

                url = upstream_base.rstrip("/") + self.path
                with client.stream(
                    self.command, url, content=forward_body, headers=forward_headers
                ) as upstream:
                    self.send_response(upstream.status_code)
                    for k, v in upstream.headers.items():
                        if k.lower() in _HOP_BY_HOP:
                            continue
                        self.send_header(k, v)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    response_started = True

                    content_type = upstream.headers.get("content-type", "")
                    if content_type.startswith(_SSE_CONTENT_TYPE):
                        VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                        with Vault(VAULT_DB_PATH, session_id) as vault:
                            raw_text, out_text = _relay_sse_response(self, upstream, vault)
                    else:
                        raw_text, out_text = _relay_buffered_response(self, upstream, session_id)
                    _publish_stage(pipeline_bus, request_id, "response_received", raw_text)
                    _publish_stage(pipeline_bus, request_id, "detokenized", out_text)
                    _publish_stage(pipeline_bus, request_id, "displayed", out_text)
            except Exception:
                if not response_started:
                    _write_error(self, 502, "upstream request failed")
                # else: the status line and possibly some chunks are
                # already on the wire -- nothing left to do but stop.

        def _handle_panel(self) -> bool:
            """Serve a ``/veil-panel/*`` route if this request is one; else do nothing.

            Returns ``True`` when handled (the caller must not also proxy),
            ``False`` for every other path -- checked first in ``do_GET``/
            ``do_POST`` so a panel route can never reach the upstream proxy
            and a real API path can never be intercepted here.

            Any path starting with ``_PANEL_PREFIX`` is claimed here even if
            it matches no known route/method combination below (e.g. a GET
            on the reveal endpoint, or a typo'd sub-path): it answers 404
            itself rather than falling through to the proxy, which would
            otherwise forward a nonsensical path to the real upstream API.
            """
            path = urlsplit(self.path).path
            if path == "/favicon.ico":
                # A browser tab open on the panel fetches this automatically.
                # Without this, it falls through to the proxy below and gets
                # silently forwarded to the real upstream API as a live
                # request -- polluting the pipeline-stream with an unrelated
                # response and wasting a round-trip. Never a real API path.
                self.send_response(204)
                self.end_headers()
                return True
            if path in (_PANEL_PREFIX, f"{_PANEL_PREFIX}/"):
                _write_html(self, 200, _PANEL_HTML_PATH.read_bytes())
                return True
            if path == f"{_PANEL_PREFIX}/audit-stream" and self.command == "GET":
                self._serve_audit_stream()
                return True
            if path == f"{_PANEL_PREFIX}/pipeline-stream" and self.command == "GET":
                self._serve_pipeline_stream()
                return True
            if path == f"{_PANEL_PREFIX}/vault" and self.command == "GET":
                self._serve_vault_list()
                return True
            if path == f"{_PANEL_PREFIX}/vault/reveal" and self.command == "POST":
                self._serve_vault_reveal()
                return True
            if path == f"{_PANEL_PREFIX}/metrics" and self.command == "GET":
                self._serve_metrics()
                return True
            if path.startswith(f"{_PANEL_PREFIX}/"):
                _write_json(self, 404, {"error": f"no panel route for {self.command} {path}"})
                return True
            return False

        def _default_session_id(self) -> str:
            return last_session_id.get("value", fallback_session_id)

        def _serve_audit_stream(self) -> None:
            _write_sse_headers(self)
            AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            AUDIT_LOG_PATH.touch(exist_ok=True)
            try:
                with AUDIT_LOG_PATH.open(encoding="utf-8") as handle:
                    handle.seek(0, os.SEEK_END)
                    while True:
                        line = handle.readline()
                        if not line:
                            time.sleep(_AUDIT_TAIL_POLL_SECONDS)
                            continue
                        _write_chunk(self, f"data: {line.strip()}\n\n".encode())
            except OSError:  # BrokenPipeError/ConnectionResetError are OSError subclasses
                return

        def _serve_pipeline_stream(self) -> None:
            _write_sse_headers(self)
            subscriber = pipeline_bus.subscribe()
            try:
                while True:
                    event = subscriber.get()
                    _write_chunk(self, f"data: {json.dumps(event)}\n\n".encode())
            except OSError:  # BrokenPipeError/ConnectionResetError are OSError subclasses
                return
            finally:
                pipeline_bus.unsubscribe(subscriber)

        def _serve_vault_list(self) -> None:
            session_id = _query_param(self.path, "session_id") or self._default_session_id()
            VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            with Vault(VAULT_DB_PATH, session_id) as vault:
                mappings = vault.list_mappings()
            _write_json(
                self,
                200,
                {
                    "session_id": session_id,
                    "mappings": [
                        {
                            "token": m.token,
                            "entity_type": m.entity_type,
                            "ciphertext_b64": m.ciphertext_b64,
                            "created_at": m.created_at,
                        }
                        for m in mappings
                    ],
                },
            )

        def _serve_vault_reveal(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw) if raw else {}
                token = payload["token"]
                if not isinstance(token, str) or not token:
                    raise ValueError("token must be a non-empty string")
            except Exception:
                _write_json(self, 400, {"error": 'request body must be {"token": "..."}'})
                return
            session_id = payload.get("session_id") or self._default_session_id()
            VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            try:
                with Vault(VAULT_DB_PATH, session_id) as vault:
                    value = vault.resolve(token)
            except TokenNotFoundError:
                _write_json(self, 404, {"error": f"token {token!r} not found for this session"})
                return
            except VaultIntegrityError:
                _write_json(self, 409, {"error": "vault integrity check failed for this token"})
                return
            # This is the one endpoint in the whole codebase whose entire job
            # is to return a decrypted value on request (BRIEF SS5's explicit
            # "explicit action" reveal) -- a forensic trail of *when* and
            # *which token* is the least this should leave behind. Never the
            # value itself, matching every other log line this project ever
            # writes.
            print(
                f"veil gateway: revealed token {token} for session {session_id}",
                file=sys.stderr,
            )
            _write_json(self, 200, {"token": token, "value": value})

        def _serve_metrics(self) -> None:
            try:
                report = eval_module.evaluate()
            except (FileNotFoundError, sqlite3.OperationalError) as exc:
                _write_json(self, 200, {"available": False, "reason": str(exc)})
                return
            _write_json(
                self,
                200,
                {
                    "available": True,
                    "by_type": {
                        name: {
                            "support": metrics.support,
                            "true_positives": metrics.true_positives,
                            "false_positives": metrics.false_positives,
                            "recall": metrics.recall,
                            "precision": metrics.precision,
                        }
                        for name, metrics in report.by_type.items()
                    },
                },
            )

        def do_GET(self) -> None:
            if self._handle_panel():
                return
            self._handle()

        def do_POST(self) -> None:
            if self._handle_panel():
                return
            self._handle()

        def do_PUT(self) -> None:
            if self._handle_panel():
                return
            self._handle()

        def do_DELETE(self) -> None:
            if self._handle_panel():
                return
            self._handle()

        def log_message(self, format: str, *args: object) -> None:
            pass

    return GatewayHandler


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    port = int(argv[0]) if argv else _DEFAULT_PORT
    upstream = os.environ.get(UPSTREAM_ENV_VAR, _DEFAULT_UPSTREAM)
    fallback_session_id = _new_session_id()

    client = httpx.Client(timeout=httpx.Timeout(300.0))
    handler_cls = make_handler(fallback_session_id, client, upstream)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    print(
        f"veil gateway: listening on http://127.0.0.1:{port} -> {upstream}"
        f" (fallback session {fallback_session_id}, used only when a request has no"
        f" {SESSION_ID_HEADER} header)",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
