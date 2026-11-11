"""Reverse-HTTP-proxy gateway: whole-payload tokenization at ``ANTHROPIC_BASE_URL``.

Listens locally and stands in for ``https://api.anthropic.com``. For every
request: reads the full body, and if it is JSON, tokenizes the text in its
``messages`` (``_tokenize_messages``: ids, signatures, thinking and base64
pass through) via ``core/tokenizer.py::tokenize()`` against a per-process
``core/vault.py::Vault`` before forwarding it upstream with an
otherwise-untouched method/path/headers (including auth).

The response is rewritten on the way back so a token the model echoes (e.g.
``PII_EMAIL_0001``) resolves to its real value before it reaches the client
(an earlier version relayed the response untouched as an explicit,
documented scope cut). Two response shapes are handled, both via
``core/tokenizer.py`` -- this module never re-implements detection or
substitution, only wires ``core/`` into the transport:

- **Streaming (``text/event-stream``)**: each ``content_block_delta`` event's
  text-bearing field is fed through a ``core.tokenizer.IncrementalDetokenizer``
  keyed by ``content_block_index``, live-verified against the real Anthropic
  API to confirm the event/delta shapes below and that an
  SSE ``data:`` line is always one complete JSON event -- a token can only
  split across the *text* of two such events, never mid-JSON. Any leftover
  buffered text is flushed into a synthetic extra delta event right before
  that block's ``content_block_stop``, so nothing is ever silently dropped.
- **Buffered JSON (anything else that parses as JSON)**: detokenized in one
  shot via ``core.tokenizer.detokenize_lenient()``.
- **Anything else** (non-JSON, non-SSE): relayed byte-for-byte untouched,
  same as the gateway's original behavior -- there is nothing to safely
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
outbound Claude Code request in live testing) -- the same id
``PreToolUse``/``PostToolUse`` already use from their own hook payload's
``session_id``. This is what lets a token minted by the gateway resolve
under a hook, and vice versa: before this, the
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

Also serves the observability panel under the reserved
``/veil-panel`` path prefix, intercepted before the proxy pass-through
above: a static, unbuilt frontend page; an SSE tail of the shared
``core/audit.py`` JSONL (written by this process and by the hook/`/veil`
adapters, so the panel sees every adapter's activity); a *separate*,
in-memory-only SSE stream of pipeline-stage-transition events
(``PIPELINE_STAGES``) this process publishes for its own request/response
cycles -- structurally distinct from the audit log because it carries
human-readable request/response *display* text (the last user
turn's own text, and the model's reconstructed reply text -- never the
whole raw JSON document or SSE wire framing) that the panel's four panes need
to show, which the audit log deliberately never does. That stream keeps a
bounded ring buffer of the last ``PIPELINE_HISTORY_LIMIT`` cycles
(``PipelineEventBus``) so a panel tab opened against an already-running
gateway still sees recent history rather than nothing; and small JSON
endpoints for the vault inspector (list, and an explicit reveal-one-token
action), a quick regex-only `demo/eval.py` metrics report, and a
regex/LLM/union metrics *comparison* persisted to
``METRICS_COMPARISON_PATH`` (expensive to compute, so it only ever runs on
an explicit POST, and survives a gateway restart). None of this adds
authentication beyond the gateway's existing ``127.0.0.1``-only bind.

It additionally groups pipeline-stage events into *turns*: a
tool_result-only continuation cycle (``_classify_cycle_kind``'s
``"tool_continuation"``) is tagged with its parent conversation cycle's
``request_id`` (``PipelineEventBus``'s per-session "open turn" tracking) so
the frontend can render one logical user request -- however many tool
round-trips it takes -- as a single screen instead of one chip per raw HTTP
call. ``POST /veil-panel/hook-event`` lets the ``PreToolUse``/``PostToolUse``
hooks (separate, short-lived processes) report best-effort live progress
markers (a fixed ``hook``/``phase``/``tool_name`` shape, never a command
string or tool output) into that same open turn, which is what makes the
actual local DB round-trip's duration visible in the panel for the first
time.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit

import httpx

from veil_pii_middleware.core.audit import AuditLog
from veil_pii_middleware.core.detector import (
    LLMClient,
    Span,
    SpanEncoder,
    detect,
    llm_client_from_env,
)
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

#: Persisted cache of the last regex/LLM/union metrics comparison,
#: git-ignored like every other ``.veil/`` file. Running the LLM/union
#: modes is expensive (one real LLM call per manifest entry), so the panel's
#: Metrics tab shows whatever was last written here rather than recomputing
#: on every page load, and this file survives a gateway restart -- unlike the
#: in-memory pipeline-stage stream below.
METRICS_COMPARISON_PATH = Path(".veil") / "metrics_comparison.json"

#: Reserved path prefix for the observability panel's own HTTP surface
#: -- checked before the unconditional upstream-proxy pass-through,
#: so a panel request never reaches the real API and a real API path can
#: never collide with it (no Anthropic endpoint starts with this segment).
_PANEL_PREFIX: Final = "/veil-panel"

#: How often the audit-log SSE tail polls for new lines when caught up to
#: end-of-file. Short enough to feel live in a demo, long enough not to spin.
_AUDIT_TAIL_POLL_SECONDS: Final = 0.25

#: Pipeline-stage names published on the in-memory SSE stream, in the order
#: the panel's flow diagram lists them. Structurally separate from
#: AuditLog's JSONL: these carry the actual request/response text (needed to
#: render the panel's four panes) and are never persisted, which is why the
#: two streams deliberately do not share a schema.
PIPELINE_STAGES: Final = (
    "received",
    "tokenized",
    "forwarded",
    "response_received",
    "detokenized",
    "displayed",
)

#: How many completed request/response cycles ``PipelineEventBus`` keeps
#: buffered in memory (oldest evicted first) so a panel tab opened against
#: an already-running gateway can see recent history instead of nothing --
#: see the module docstring. Matches the
#: frontend's own cycle-selector cap; both sides must agree on this number.
PIPELINE_HISTORY_LIMIT: Final = 20

#: Cycle kinds that are real Claude Code requests but never human-authored
#: conversation (``_classify_cycle_kind``) -- the
#: panel hides them from the selector by default, and, after a
#: live-testing fix, they also get their *own* ``PIPELINE_HISTORY_LIMIT``-
#: sized ring buffer (``PipelineEventBus._aux_order``) entirely separate
#: from real turns' (``_order``). Found live: a single real conversation
#: with several tool round-trips produced enough interleaved
#: ``"safety_check"`` cycles (Claude Code's own harm classifier, one per
#: tool call) to evict the real conversation's own cycle from a single
#: shared 20-slot buffer before the panel ever connected. Must mirror the
#: frontend's own ``HIDDEN_KINDS``
#: constant exactly; both sides need to agree on what counts as noise.
_AUXILIARY_CYCLE_KINDS: Final = frozenset(
    {"title_generation", "safety_check", "internal_continuation"}
)

#: Allowed values for ``POST /veil-panel/hook-event``'s ``hook``/``phase``
#: fields -- a fixed, small enum, not freeform text, so the
#: schema stays structurally incapable of carrying a command string or tool
#: output (this must be true by construction, not by convention).
_ALLOWED_HOOK_NAMES: Final = frozenset({"pre_tool_use", "post_tool_use"})
_ALLOWED_HOOK_PHASES: Final = frozenset({"start", "done", "error"})


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

    Also keeps a bounded ring buffer of the last ``PIPELINE_HISTORY_LIMIT``
    request/response cycles' events (by ``request_id``, oldest evicted
    first) so a subscriber connecting after those cycles already completed
    still sees them -- ``subscribe()`` returns that backlog as a snapshot
    alongside the queue. The backlog snapshot and the subscriber-list
    snapshot ``publish()`` takes both happen under the same lock, so a given
    event is always delivered to a given subscriber exactly once, via
    either the backlog or a live ``put``, never both and never neither,
    regardless of how the two calls interleave across threads. Still purely
    in-memory: a process restart loses it, same as before this buffer.

    **Two separate ring buffers, not one (found live).** A
    cycle whose kind is in ``_AUXILIARY_CYCLE_KINDS`` (hidden from the
    selector by default) is evicted from its own ``_aux_order`` deque,
    entirely independent of real turns' ``_order`` -- a single shared
    20-slot buffer let a burst of Claude Code's own internal harm-classifier
    calls (one per tool call, observed live during one ordinary demo-db
    query) evict the *real* conversation cycle before a freshly-opened
    panel ever got to see it.

    **Turn tracking (revised after live testing against a real
    ``claude -p`` session).** A first cut keyed the session's "open turn" by
    *the most recently seen ``"conversation"`` cycle*, full stop. Live
    testing showed that breaks two ways: (a)
    Claude Code's own internal harm/safety-classifier request briefly read
    as an ordinary ``"conversation"`` cycle before ``_classify_cycle_kind``
    learned to recognize it, silently stealing the pointer between two
    stages of the *real* turn and misattributing a hook event to the wrong
    screen; (b) even correctly classified, the same real human turn can
    legitimately produce *more than one* top-level ``"conversation"`` cycle
    with byte-identical received text (observed live: two independent,
    complete tool-calling round-trips for the same one-sentence prompt) --
    a single "most recent wins" pointer would still split those across two
    chips instead of folding them together.

    Revised design: turns are keyed by ``(session_id, received_text)``, not
    by session alone. The *first* ``"conversation"`` cycle seen for a given
    key becomes that key's canonical turn id; every later
    ``"conversation"`` cycle with the *same* key folds under that same turn
    (gets a ``parent_request_id`` pointing at it, just like a
    ``tool_continuation`` does) instead of becoming a new turn or stealing
    the pointer. ``_open_turn_by_session`` still tracks one *canonical turn
    id* per session for ``tool_continuation``/hook attachment, but it is
    always resolved through the key map first, so a same-text sibling
    cycle can never redirect it away from the original turn. A cycle
    classified anything other than ``"conversation"`` never touches this
    tracking at all. No ``parent_request_id`` at all means "no open turn
    was found" -- the frontend's documented fallback (a standalone chip,
    never silently dropped).
    """

    def __init__(self, maxsize: int = 500) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        #: Real turns (``"conversation"``/``"tool_continuation"``, plus
        #: anything an early failure never got to classify) -- see
        #: ``_AUXILIARY_CYCLE_KINDS`` for why this is a *separate* budget
        #: from ``_aux_order`` below, not one shared 20-slot buffer.
        self._order: deque[str] = deque(maxlen=PIPELINE_HISTORY_LIMIT)
        #: ``title_generation``/``safety_check``/``internal_continuation``
        #: cycles -- hidden from the selector by default, but still worth
        #: keeping their own bounded history for the "Show hidden" toggle.
        #: Its own ring buffer so a burst of these (observed live: Claude
        #: Code's harm classifier fires roughly once per tool call) can
        #: never evict a real, still-relevant turn from ``_order``.
        self._aux_order: deque[str] = deque(maxlen=PIPELINE_HISTORY_LIMIT)
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._open_turn_by_session: dict[str, str] = {}
        self._turn_id_by_key: dict[tuple[str, str], str] = {}
        self._parent_of: dict[str, str | None] = {}

    def subscribe(self) -> tuple[queue.Queue[dict[str, Any]], list[dict[str, Any]]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            backlog = [
                event
                for request_id in (*self._order, *self._aux_order)
                for event in self._history[request_id]
            ]
            self._subscribers.add(subscriber)
        return subscriber, backlog

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def open_turn_for_session(self, session_id: str) -> str | None:
        """Return the ``request_id`` of ``session_id``'s currently open turn, if any.

        Used by ``/veil-panel/hook-event`` to attach a hook's
        best-effort progress report to the right screen -- see the class
        docstring's "Turn tracking" section.
        """
        with self._lock:
            return self._open_turn_by_session.get(session_id)

    def publish(self, event: dict[str, Any], session_id: str | None = None) -> None:
        with self._lock:
            request_id = event["request_id"]
            if request_id not in self._history:
                kind = event.get("kind")
                order = self._aux_order if kind in _AUXILIARY_CYCLE_KINDS else self._order
                if len(order) == order.maxlen:
                    evicted = order.popleft()
                    self._history.pop(evicted, None)
                    self._parent_of.pop(evicted, None)
                order.append(request_id)
                self._history[request_id] = []
                if kind == "conversation" and session_id:
                    key = (session_id, event.get("text", ""))
                    # `_turn_id_by_key` is never pruned on eviction (unlike
                    # `_parent_of`/`_open_turn_by_session`) -- a stale entry
                    # only means a *very* old, already-evicted turn id is
                    # still on record as "the" turn for that exact text,
                    # which just falls through to the orphan fallback below
                    # once that id is no longer in `_history`. Acceptable
                    # for a demo-scale, single-session process.
                    turn_id = self._turn_id_by_key.get(key)
                    if turn_id is None or turn_id not in self._history:
                        turn_id = request_id
                        self._turn_id_by_key[key] = turn_id
                        self._parent_of[request_id] = None
                    else:
                        self._parent_of[request_id] = turn_id
                    self._open_turn_by_session[session_id] = turn_id
                elif kind == "tool_continuation" and session_id:
                    self._parent_of[request_id] = self._open_turn_by_session.get(session_id)
                else:
                    self._parent_of[request_id] = None
            parent_request_id = self._parent_of.get(request_id)
            if parent_request_id is not None:
                event = {**event, "parent_request_id": parent_request_id}
            self._history[request_id].append(event)
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
    kind: str | None = None,
    session_id: str | None = None,
) -> None:
    """Publish one pipeline-stage event. ``spans`` is only meaningful for ``"received"``.

    Entity type/detector are the only span metadata a stage event carries --
    the offsets index into this same event's own ``text``, so nothing about
    the value itself is duplicated beyond what ``text`` already contains.

    ``kind`` (``_classify_cycle_kind``'s best-effort label) is published on
    every stage of a cycle, not just ``"received"``, so the frontend can
    filter a whole cycle out of the selector as soon as any one of its
    events arrives, regardless of which stage that happens to be.

    ``session_id`` feeds ``PipelineEventBus``'s turn-tracking --
    only meaningful the first time a given ``request_id`` is published (see
    ``PipelineEventBus.publish``), but harmless to pass on every call.
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
    if kind is not None:
        event["kind"] = kind
    bus.publish(event, session_id=session_id)


#: Directory containing the static, unbuilt observability panel page --
#: plain HTML/CSS/vanilla-loaded React, no bundler, served directly
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


#: Real Claude Code session id, sent on every outbound request (confirmed
#: in live testing). Reading it lets the gateway key its ``Vault`` calls the same
#: way ``PreToolUse``/``PostToolUse`` already key theirs from the hook
#: payload's ``session_id`` -- see the module docstring.
SESSION_ID_HEADER = "X-Claude-Code-Session-Id"


def _resolve_session_id(handler: BaseHTTPRequestHandler, fallback_session_id: str) -> str:
    """Return the session id to use for this request's ``Vault`` calls.

    Prefers ``SESSION_ID_HEADER`` if present and non-empty after stripping
    whitespace; otherwise falls back to ``fallback_session_id`` (the
    per-process UUID) and logs a stderr warning. Matches the hooks adapters'
    own ``session_id`` validation (non-empty ``str``, no further format
    check) -- deliberately no stricter schema.
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


def _write_error(
    handler: BaseHTTPRequestHandler,
    status: int,
    message: str,
    error_type: str = "veil_gateway_error",
) -> None:
    # Shaped like an Anthropic API error object so an SDK client on the
    # other end has a recognizable error to surface, rather than an opaque
    # connection failure.
    body = json.dumps({"type": "error", "error": {"type": error_type, "message": message}}).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


#: Anthropic content-block types this module renders as display text; any
#: other block type (``tool_use``, ``tool_result``, ``image``, ...) becomes
#: a short bracketed placeholder instead of being silently dropped, so a
#: turn with no text/thinking block still renders as *something* rather
#: than a mysteriously empty pane (the panel's four panes must show the
#: actual message, not nothing).
_TEXT_CONTENT_BLOCK_FIELDS: Final = {"text": "text", "thinking": "thinking"}


def _extract_text_from_content_blocks(content: JSONValue) -> str:
    """Render an Anthropic ``content`` value (plain string or block list) as display text.

    A plain string is returned as-is -- the common shape in this codebase's
    own tests and in a simple single-turn request. A list of blocks joins
    every ``text``/``thinking`` block's own field, in order; see
    ``_TEXT_CONTENT_BLOCK_FIELDS`` for how a non-text block renders instead.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        field = _TEXT_CONTENT_BLOCK_FIELDS.get(str(block_type))
        field_value = block.get(field) if field is not None else None
        if isinstance(field_value, str):
            parts.append(field_value)
        else:
            parts.append(f"[{block_type}]" if block_type else "[content]")
    return "\n".join(parts)


def _find_last_user_message_content(parsed: JSONValue) -> JSONValue:
    """Return the last ``role == "user"`` message's raw ``content`` value.

    Shared by ``_extract_last_user_message_text`` and
    ``_last_user_content_has_text`` (for ``_classify_cycle_kind``'s
    ``tool_continuation`` distinction) so both walk the exact same message,
    never two independently-drifting definitions of "the last user turn".
    Returns ``None`` if ``parsed`` is not shaped like a Messages API request
    body (not a dict, no ``messages`` list, or no ``user``-role message in
    it) or that message has no ``content`` key.
    """
    if not isinstance(parsed, dict):
        return None
    messages = parsed.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return message.get("content")
    return None


def _extract_last_user_message_text(parsed: JSONValue) -> str:
    """Return the last ``role == "user"`` message's display text from a request body.

    Claude Code resends the full conversation history on every request; the
    panel's "Input" pane is scoped to what the user actually typed *this
    turn*, not the repeated history.
    """
    return _extract_text_from_content_blocks(_find_last_user_message_content(parsed))


def _last_user_content_has_text(parsed: JSONValue) -> bool:
    """Whether the last user message contains any real ``text``/``thinking`` block.

    ``False`` for a message whose content is entirely non-text blocks (e.g.
    a bare ``tool_result`` continuation, or a lone ``tool_use`` echo) -- the
    signal ``_classify_cycle_kind`` needs to tell a genuine tool_result
    continuation apart from an ordinary conversational turn, since
    ``_extract_text_from_content_blocks`` renders the former as the
    non-empty placeholder ``"[tool_result]"`` (this is exactly
    why that placeholder used to be misread as real
    conversational content).
    """
    content = _find_last_user_message_content(parsed)
    if isinstance(content, str):
        return bool(_strip_injected_wrapper_tags(content))
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        field = _TEXT_CONTENT_BLOCK_FIELDS.get(str(block.get("type")))
        value = block.get(field) if field is not None else None
        # A continuation often carries a `<system-reminder>` text block (todo
        # reminders, hook context); only text left after stripping wrappers
        # counts, or the continuation would pose as a new turn.
        if isinstance(value, str) and _strip_injected_wrapper_tags(value):
            return True
    return False


def _extract_response_text(parsed: JSONValue) -> str:
    """Return a Messages API response body's display text (its ``content`` blocks)."""
    if not isinstance(parsed, dict):
        return ""
    return _extract_text_from_content_blocks(parsed.get("content"))


def _default_llm_client_for_display() -> LLMClient | None:
    """Build the same env-gated ``LLMClient`` ``core.tokenizer``'s internal default would.

    Built explicitly here, rather than letting ``tokenize()`` build and
    close its own invisible one, so the *same instance* can be reused for
    one extra, scoped ``detect()`` call this module makes purely for
    display-span offsets (see ``_tokenize_body``). ``core.detector``'s
    process-wide LLM response cache is keyed by ``(text, client.identity)``,
    so reusing the instance is what makes that second call a cache hit
    instead of a second network round trip.
    """
    return llm_client_from_env()


#: Substring of Claude Code's own system prompt for its dedicated
#: session-title-generation request (confirmed live 2026-09-08 by capturing
#: raw traffic through a throwaway diagnostic proxy, bypassing this
#: module's own extraction entirely, to rule out that we were the ones
#: adding it). Best-effort, not a protocol guarantee -- the exact wording
#: could change between Claude Code CLI versions -- but stable enough to be
#: worth keying off rather than showing every session's title-generation
#: request in the panel as if it were a real conversational turn.
_TITLE_GENERATION_SYSTEM_MARKER: Final = "naming a coding session"

#: Substring of Claude Code's own internal harm/safety-classifier request
#: (confirmed live 2026-09-09 by capturing a real `claude -p
#: --demo-db` session's raw traffic): the *last user message* itself --
#: not the system prompt, unlike the title-generation marker above -- wraps
#: a JSON summary of the transcript in `<transcript>...</transcript>` and
#: asks the model to grade harm as `<severity>N</severity>`. Checked on
#: `stripped_received_text` because the trailing instruction text sits
#: *outside* the `<transcript>` tag, so stripping known wrapper tags alone
#: does not empty this message the way it does for `<system-reminder>`
#: etc. -- without this marker, this cycle read as an ordinary
#: `"conversation"` and, worse, kept stealing the turn-tracking
#: away from the real conversation between two of its own stages.
_SAFETY_CLASSIFIER_MARKER: Final = "respond with <severity>"

#: Tag names Claude Code itself wraps around non-human-authored content
#: within an otherwise-ordinary user turn -- confirmed the same way as the
#: marker above, and directly by this project's own live Claude Code
#: sessions: a `<system-reminder>` block is prepended to essentially every
#: real conversational turn, and `<local-command-caveat>`/`<command-name>`/
#: `<command-message>`/`<command-args>`/`<local-command-stdout>` wrap a
#: local slash-command's echoed output. `<session>` wraps session context
#: Claude Code includes both in its dedicated title-generation request and,
#: live-reported by a human tester, around context it prepends to an
#: ordinary conversational turn too -- so its presence alone must not be
#: used to hide a cycle (see ``_TITLE_GENERATION_SYSTEM_MARKER`` for that).
#: Stripped from *displayed* text only -- never from what is tokenized or
#: forwarded upstream, which is computed separately from the untouched
#: parsed body.
_INJECTED_WRAPPER_TAGS: Final = (
    "system-reminder",
    "local-command-caveat",
    "local-command-stdout",
    "command-name",
    "command-message",
    "command-args",
    "session",
)

_INJECTED_WRAPPER_TAG_RE: Final = re.compile(
    r"<(" + "|".join(_INJECTED_WRAPPER_TAGS) + r")\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


def _strip_injected_wrapper_tags(text: str) -> str:
    """Remove Claude-Code-injected framing blocks, leaving only human-authored text.

    A real user turn is not always *just* what the human typed -- Claude
    Code interleaves framing like `<system-reminder>...</system-reminder>`
    or `<session>...</session>` in the same message. The panel's Input pane
    must show exactly what the human typed, "ni mas ni menos" -- so this
    removes every recognized wrapper block (``_INJECTED_WRAPPER_TAGS``,
    best-effort by tag name, not a protocol guarantee) and collapses the
    blank lines left behind, rather than showing the raw interleaved text
    or hiding the whole cycle because a wrapper happens to sit at the start.
    """
    stripped = _INJECTED_WRAPPER_TAG_RE.sub("", text)
    return re.sub(r"\n{2,}", "\n", stripped).strip()


#: Anthropic's own reserved commit-trailer address (``Co-Authored-By: Claude
#: ... <noreply@anthropic.com>``), part of Claude Code's own static tool
#: instructions -- never a real person's address, so excluding this exact,
#: fixed string from detection loses no real protection. A value-based
#: exclusion (unlike a leaf-level skip) is safe here specifically because
#: this string is permanently-fixed Anthropic infrastructure text, not
#: something a real user's own PII could ever coincide with.
_ANTHROPIC_COMMIT_TRAILER_EMAIL: Final = "noreply@anthropic.com"


def _excluded_ranges(text: str) -> list[tuple[int, int]]:
    """Ranges within one string leaf that must never be tokenized.

    Passed as ``tokenize()``'s ``exclude`` callback (``core/tokenizer.py``).
    Only Anthropic's own fixed commit-trailer address is carved out.
    Wrapper tags (``_INJECTED_WRAPPER_TAGS``) are stripped for *display*
    only: their contents are tokenized like any other text, because Claude
    Code puts real file content, IDE selections and hook context inside
    `<system-reminder>`, and a tool result can contain any of these tags.
    """
    ranges = []
    start = text.find(_ANTHROPIC_COMMIT_TRAILER_EMAIL)
    while start != -1:
        ranges.append((start, start + len(_ANTHROPIC_COMMIT_TRAILER_EMAIL)))
        start = text.find(_ANTHROPIC_COMMIT_TRAILER_EMAIL, start + 1)
    return ranges


#: The only top-level request field that is tokenized (regex *and* LLM). It
#: holds the conversation: the user's typed text and every tool result
#: (database rows, file contents) re-sent on each turn -- the only place
#: customer data can appear. Everything else (``system`` prompt, ``tools``
#: definitions, ``metadata``, model/params) is Claude Code's own boilerplate and
#: is forwarded byte-for-byte: scanning it protected no customer data and
#: produced false positives ("Claude Code" as a PERSON, a temp-dir id as a
#: phone number).
_TOKENIZED_FIELDS: Final = frozenset({"messages"})


#: Same variable ``core.encoder`` reads (duplicated so this module can check
#: it without importing the heavy optional encoder dependencies).
ENCODER_ENV_VAR: Final = "VEIL_ENCODER"

#: Encoder NER detector (``core.encoder``) shared by every request, loaded
#: once in ``main()`` when ``VEIL_ENCODER`` is set -- loading a model takes
#: seconds, so it must never happen per request. ``None`` means off.
_ENCODER: SpanEncoder | None = None


def _tokenize_request(
    parsed: JSONValue,
    vault: Vault,
    llm_client: LLMClient | None,
    audit_log: AuditLog,
) -> JSONValue:
    """Tokenize a request body's ``messages``; every other field passes through untouched."""

    def run(value: JSONValue) -> JSONValue:
        return tokenize(
            value,
            vault,
            llm_client=llm_client,
            audit_log=audit_log,
            adapter="gateway",
            exclude=_excluded_ranges,
            encoder=_ENCODER,
        )

    if not isinstance(parsed, dict):
        return run(parsed)
    return {
        key: _tokenize_messages(value, run) if key in _TOKENIZED_FIELDS else value
        for key, value in parsed.items()
    }


#: Content blocks forwarded byte for byte. Thinking blocks are signed (any
#: change makes the API reject the conversation) and were generated from
#: already-tokenized input, so they hold no raw PII; images carry base64.
_PASSTHROUGH_BLOCK_TYPES: Final = frozenset({"thinking", "redacted_thinking", "image"})

#: Keys of a content block (or message) that are protocol data, never user
#: text: ids, signatures, tool names, cache control, media types.
_PASSTHROUGH_BLOCK_KEYS: Final = frozenset(
    {"type", "id", "tool_use_id", "signature", "cache_control", "name", "media_type", "role"}
)

#: ``source.type`` values whose payload is binary or a reference, never text.
_OPAQUE_SOURCE_TYPES: Final = frozenset({"base64", "url", "file"})


def _tokenize_messages(messages: JSONValue, run: Callable[[JSONValue], JSONValue]) -> JSONValue:
    """Tokenize the text a ``messages`` array carries, and nothing else.

    Walks messages and content blocks with fixed rules: protocol keys
    (``_PASSTHROUGH_BLOCK_KEYS``), thinking and image blocks, and base64 or
    URL sources pass through untouched; ``text``, ``tool_result`` content
    (string or nested blocks), ``tool_use.input``, document text and any
    other field are tokenized. Scanning ids, signatures and base64 used to
    corrupt them (a random base64 run can pass the IBAN check) and cost
    most of the gateway's detection time.
    """
    if not isinstance(messages, list):
        return run(messages)
    return [_tokenize_block(message, run) for message in messages]


def _tokenize_block(block: JSONValue, run: Callable[[JSONValue], JSONValue]) -> JSONValue:
    if isinstance(block, list):
        return [_tokenize_block(item, run) for item in block]
    if not isinstance(block, dict):
        return run(block)
    if block.get("type") in _PASSTHROUGH_BLOCK_TYPES:
        return block
    out: dict[str, JSONValue] = {}
    for key, value in block.items():
        if key in _PASSTHROUGH_BLOCK_KEYS:
            out[key] = value
        elif key == "source" and isinstance(value, dict):
            out[key] = (
                value if value.get("type") in _OPAQUE_SOURCE_TYPES else _tokenize_block(value, run)
            )
        elif key == "content":
            out[key] = _tokenize_block(value, run) if isinstance(value, list) else run(value)
        else:
            out[key] = run(value)
    return out


def _extract_system_text(parsed: JSONValue) -> str:
    """Join a Messages API request body's ``system`` field into plain text.

    ``system`` is either a plain string or a list of blocks (mirroring
    ``content``'s own two shapes) -- used only for ``_classify_cycle_kind``'s
    heuristic, never displayed.
    """
    if not isinstance(parsed, dict):
        return ""
    system_value = parsed.get("system")
    if isinstance(system_value, str):
        return system_value
    if not isinstance(system_value, list):
        return ""
    parts: list[str] = []
    for block in system_value:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _classify_cycle_kind(
    parsed: JSONValue, stripped_received_text: str, has_real_text: bool
) -> str:
    """Best-effort label for what kind of request this cycle is.

    Heuristic, not a protocol guarantee. ``"title_generation"``,
    ``"safety_check"``, ``"internal_continuation"`` and
    ``"tool_continuation"`` are all real, legitimate Claude Code requests --
    classified so the panel's cycle selector can default to hiding the
    first three (they carry no human-authored conversational content) and
    fold the fourth into its parent turn's screen instead of
    giving it a chip of its own, not because anything about any of them is
    wrong. ``"conversation"`` is the default for everything else, including
    a request this heuristic fails to recognize as one of the other four.

    ``stripped_received_text`` must already have gone through
    ``_strip_injected_wrapper_tags`` -- classifying on the *raw* text would
    mislabel any ordinary conversational turn that happens to have a
    `<system-reminder>` (or other wrapper) block interleaved with the
    human's own text as ``"internal_continuation"``, which is the
    over-hiding bug this replaced: a plain prefix check on the raw text
    fires on the common case where Claude Code prepends a reminder block
    *before* real human-authored content, not just on a turn that is
    entirely reminder content.

    ``has_real_text`` (``_last_user_content_has_text``) is what tells a
    pure tool_result continuation apart from an ordinary turn: the former's
    ``stripped_received_text`` is the non-empty placeholder
    ``"[tool_result]"`` (so it fails the ``internal_continuation`` check
    above it), but contains no actual human/model-authored text or
    thinking block at all.
    """
    if _TITLE_GENERATION_SYSTEM_MARKER in _extract_system_text(parsed).lower():
        return "title_generation"
    if _SAFETY_CLASSIFIER_MARKER in stripped_received_text.lower():
        return "safety_check"
    if not stripped_received_text:
        return "internal_continuation"
    if not has_real_text:
        return "tool_continuation"
    return "conversation"


def _tokenize_body(
    raw_body: bytes, session_id: str, audit_log: AuditLog
) -> tuple[bytes, str, list[Span], str, str]:
    """Tokenize a JSON request body. Raises on any failure -- callers must fail closed.

    Returns ``(forward_body, received_text, received_spans, outbound_text, cycle_kind)``:
    ``received_text`` is the last user message's display text *before*
    substitution (``_extract_last_user_message_text``, then
    ``_strip_injected_wrapper_tags`` so the pane shows exactly what the
    human typed, not Claude-Code-injected framing interleaved with it);
    ``received_spans`` is that same stripped text's own spans, with offsets
    relative to ``received_text`` itself -- ``core/tokenizer.py``'s per-leaf
    ``span_sink`` has no leaf identity attached, so those offsets cannot be
    reused against a shorter display string without misaligning, hence one
    extra, scoped ``detect()`` call here instead (a cache hit, not a second
    LLM round trip, whenever ``received_text`` is exactly a leaf
    ``tokenize()`` already scanned -- true whenever the message's content is
    a single string with no wrapper tags to strip); ``outbound_text`` is the
    same message's text *after* substitution, likewise stripped, for the
    "Outbound to API" pane; ``cycle_kind`` is ``_classify_cycle_kind``'s
    label for the whole cycle, computed from the *stripped* received text.

    The stripping only affects what this function returns for the
    pipeline-stream side channel -- ``tokenize()`` above runs against the
    untouched ``parsed`` body, so what is actually forwarded upstream is
    unaffected.
    """
    parsed = json.loads(raw_body)
    display_client = _default_llm_client_for_display()
    VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        # No gateway-wide lock: `Vault.get_or_create_token` is transactional
        # across connections, and the shared encoder locks its own inference,
        # so concurrent requests no longer queue behind each other's LLM calls.
        with Vault(VAULT_DB_PATH, session_id) as vault:
            tokenized: JSONValue = _tokenize_request(parsed, vault, display_client, audit_log)
        received_text = _strip_injected_wrapper_tags(_extract_last_user_message_text(parsed))
        outbound_text = _strip_injected_wrapper_tags(_extract_last_user_message_text(tokenized))
        cycle_kind = _classify_cycle_kind(
            parsed, received_text, _last_user_content_has_text(parsed)
        )
        received_spans = (
            detect(
                received_text,
                use_regex=True,
                use_llm=display_client is not None,
                llm_client=display_client,
                encoder=_ENCODER,
            )
            if received_text
            else []
        )
    finally:
        if display_client is not None:
            display_client.close()
    return (
        json.dumps(tokenized).encode(),
        received_text,
        received_spans,
        outbound_text,
        cycle_kind,
    )


#: content-type prefix that routes a response through the SSE rewrite path
#: rather than the buffered-JSON or untouched-relay paths.
_SSE_CONTENT_TYPE = "text/event-stream"

# Delta shapes confirmed live against the real Anthropic API that
# carry freeform, potentially token-bearing
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

#: The delta fields ``_SseResponseRewriter`` actually detokenizes. Thinking
#: is displayed (above) but never rewritten: Claude Code stores it and sends
#: it back signed, and a detokenized copy would have to re-tokenize to the
#: exact same bytes on the next turn or the API rejects the signature.
_DETOKENIZED_DELTA_FIELDS = {"text_delta": "text"}


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


def _extract_sse_display_text(raw_events: list[str]) -> str:
    """Reconstruct the assistant's visible text from a list of raw SSE event strings.

    Walks every ``content_block_delta`` event's text-bearing field
    (``_TEXT_BEARING_DELTA_FIELDS``), in order -- the same reconstruction a
    real streaming client performs -- instead of exposing the raw
    ``event:``/``data:`` wire framing (``message_start``, `content_block_start``
    envelope JSON, etc.) those events actually carry. Used for the pipeline
    panel's "API response"/"Developer view" panes; the caller passes this
    the pre-detokenization events for the tokenized display and the
    post-detokenization events for the resolved display.
    """
    parts: list[str] = []
    for raw_event in raw_events:
        _, obj, _ = _split_sse_event(raw_event)
        if not isinstance(obj, dict) or obj.get("type") != "content_block_delta":
            continue
        delta = obj.get("delta")
        if not isinstance(delta, dict):
            continue
        delta_type = delta.get("type")
        if not isinstance(delta_type, str):
            continue
        field = _TEXT_BEARING_DELTA_FIELDS.get(delta_type)
        if field is not None and isinstance(delta.get(field), str):
            parts.append(delta[field])
    return "".join(parts)


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
        field = _DETOKENIZED_DELTA_FIELDS.get(delta_type)
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
        field = _DETOKENIZED_DELTA_FIELDS[delta_type]
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
    # A zero-length chunk is the end-of-body marker; writing one for empty
    # data would end the body early and corrupt a keep-alive connection.
    if not data:
        return
    handler.wfile.write(f"{len(data):X}\r\n".encode())
    handler.wfile.write(data)
    handler.wfile.write(b"\r\n")


def _relay_sse_response(
    handler: BaseHTTPRequestHandler, upstream: httpx.Response, vault: Vault
) -> tuple[str, str]:
    """Relay an SSE response, returning ``(raw_display, rewritten_display)`` for the pipeline panel.

    Both are the assistant's reconstructed visible text (``_extract_sse_display_text``),
    not the raw ``event:``/``data:`` wire framing -- the wire bytes this
    function actually writes to ``handler.wfile`` via ``_write_chunk`` are
    unchanged by this; only what gets published on the in-memory
    pipeline-stage event bus after this function returns (see the caller in
    ``GatewayHandler._handle``) changes.
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
    return _extract_sse_display_text(raw_events), _extract_sse_display_text(out_events)


def _extract_buffered_display_text(body: bytes, raw_text: str) -> str:
    """Return a buffered response body's display text, falling back to ``raw_text``.

    ``raw_text`` (the plain-decoded body) is the fallback for anything that
    is not a JSON Messages API response shape (an error object, a ``ping``,
    non-JSON) -- there is no structured content to extract there, so the
    decoded bytes are the best available display text, same as before these
    extraction helpers existed.
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw_text
    return _extract_response_text(parsed) or raw_text


def _detokenize_response_text_blocks(parsed: JSONValue, vault: Vault) -> JSONValue:
    """Resolve tokens in a buffered Messages response's ``text`` blocks only.

    Thinking blocks are signed and go back to the API verbatim; tool_use
    input stays tokenized, as on the SSE path, for PreToolUse to resolve.
    """
    blocks = parsed.get("content") if isinstance(parsed, dict) else None
    if not isinstance(parsed, dict) or not isinstance(blocks, list):
        return parsed
    content: list[JSONValue] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            block = {**block, "text": detokenize_lenient(block.get("text"), vault)}
        content.append(block)
    return {**parsed, "content": content}


def _relay_buffered_response(
    handler: BaseHTTPRequestHandler, upstream: httpx.Response, session_id: str
) -> tuple[str, str]:
    # Fully buffers the body into memory before writing anything, unlike
    # the original bounded, progressively-streamed relay -- a
    # trade-off accepted here because detokenizing requires the whole JSON
    # value up front, response bodies on this path (anything that isn't
    # `text/event-stream`) are the small, non-streaming Anthropic API
    # replies this gateway targets, and the byte-for-byte non-JSON case just
    # below could stream but is kept in the same function for one code path.
    #
    # Returns ``(raw_display, rewritten_display)`` for the pipeline panel --
    # see ``_relay_sse_response``'s docstring; the wire bytes actually
    # written via `_write_chunk` below are unaffected by this.
    body = upstream.read()
    raw_text = _decode(body)
    raw_display = _extract_buffered_display_text(body, raw_text)
    # Every token this codebase ever mints starts with the literal "PII_"
    # (core/tokenizer.py's _TOKEN_PATTERN) -- if that substring is absent,
    # nothing here could possibly resolve, so this skips opening the vault
    # at all, the same way a GET with no request body never touches it.
    if b"PII_" not in body:
        _write_chunk(handler, body)
        handler.wfile.write(b"0\r\n\r\n")
        return raw_display, raw_display
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _write_chunk(handler, body)
        handler.wfile.write(b"0\r\n\r\n")
        return raw_display, raw_display
    VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with Vault(VAULT_DB_PATH, session_id) as vault:
        rewritten = _detokenize_response_text_blocks(parsed, vault)
    out_bytes = json.dumps(rewritten).encode()
    _write_chunk(handler, out_bytes)
    handler.wfile.write(b"0\r\n\r\n")
    return raw_display, _extract_response_text(rewritten) or _decode(out_bytes)


#: Every mode the comparison endpoint runs, in the order shown -- kept as a
#: single source of truth rather than repeating the three literal strings.
_COMPARISON_MODES: Final[tuple[eval_module.Mode, ...]] = ("regex", "llm", "union")


def _serialize_metrics(metrics: eval_module.Metrics) -> dict[str, Any]:
    return {
        "support": metrics.support,
        "true_positives": metrics.true_positives,
        "false_positives": metrics.false_positives,
        "recall": metrics.recall,
        "precision": metrics.precision,
    }


def _serialize_report(report: eval_module.Report) -> dict[str, Any]:
    """Aggregate-only serialization of a ``Report``.

    Deliberately never includes ``Miss.value`` (the manifest's synthetic
    PII value) -- the comparison tab shows recall/precision numbers, not
    the underlying data, by design.
    """
    return {
        "by_type": {name: _serialize_metrics(m) for name, m in report.by_type.items()},
        "by_detector": {name: _serialize_metrics(m) for name, m in report.by_detector.items()},
        "seconds": report.seconds,
        "cells": report.cells,
    }


def _run_encoder_comparison() -> dict[str, Any]:
    """The separate encoder section: ``regex+encoder`` with the loaded encoder.

    Reported apart from the regex/LLM/union grid so the two soft-layer
    approaches (generative LLM vs encoder NER) can be compared side by side,
    timing included. Never raises, like ``_run_comparison_mode``.
    """
    if _ENCODER is None:
        return {"available": False, "reason": f"{ENCODER_ENV_VAR} is not set"}
    try:
        report = eval_module.evaluate(mode="regex+encoder", encoder=_ENCODER)
        return {"available": True, "model": _ENCODER.identity, **_serialize_report(report)}
    except Exception as exc:
        return {"available": False, "reason": str(exc)}


def _run_comparison_mode(mode: eval_module.Mode) -> dict[str, Any]:
    """Run ``demo.eval.evaluate()`` for one mode, never raising.

    Returns ``{"available": True, **_serialize_report(...)}`` on success, or
    ``{"available": False, "reason": ...}`` on any failure -- a missing demo
    database, unset LLM env vars (``eval_module.llm_client_from_env()``
    raises ``SystemExit`` for that), or a real network failure talking to
    the local LLM -- so one mode's failure never blocks the other two.
    """
    llm_client: LLMClient | None = None
    try:
        if mode != "regex":
            llm_client = eval_module.llm_client_from_env()
        report = eval_module.evaluate(mode=mode, llm_client=llm_client)
        return {"available": True, **_serialize_report(report)}
    except SystemExit as exc:
        return {"available": False, "reason": str(exc)}
    except Exception as exc:  # LM Studio unreachable, bad manifest, etc.
        return {"available": False, "reason": str(exc)}
    finally:
        if llm_client is not None:
            llm_client.close()


def _read_metrics_comparison_cache() -> dict[str, Any]:
    try:
        cached: dict[str, Any] = json.loads(METRICS_COMPARISON_PATH.read_text(encoding="utf-8"))
        return cached
    except (FileNotFoundError, json.JSONDecodeError):
        return {"available": False}


def _write_metrics_comparison_cache(payload: dict[str, Any]) -> None:
    METRICS_COMPARISON_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_COMPARISON_PATH.write_text(json.dumps(payload), encoding="utf-8")


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
    #: demo tool, not a multi-tenant dashboard.
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
                #: Default for a request that never reaches ``_tokenize_body``'s
                #: classification (empty body) or that fails before it gets
                #: there -- treated as an ordinary conversational cycle rather
                #: than guessed at from nothing.
                cycle_kind = "conversation"
                if raw_body:
                    try:
                        (
                            forward_body,
                            received_text,
                            received_spans,
                            outbound_text,
                            cycle_kind,
                        ) = _tokenize_body(raw_body, session_id, audit_log)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        _publish_stage(
                            pipeline_bus,
                            request_id,
                            "received",
                            _decode(raw_body),
                            session_id=session_id,
                        )
                        _write_error(
                            self,
                            400,
                            "veil blocked: request body is not valid JSON; blocked rather"
                            " than forwarded unscanned",
                            "invalid_request_error",
                        )
                        return
                    except Exception:
                        _publish_stage(
                            pipeline_bus,
                            request_id,
                            "received",
                            _decode(raw_body),
                            session_id=session_id,
                        )
                        # 400, not 500: Claude Code retries 5xx with backoff,
                        # and each retry would re-run the whole scan only to
                        # be blocked again.
                        _write_error(
                            self,
                            400,
                            "veil blocked: sanitization failed; request blocked rather than"
                            " forwarded",
                            "invalid_request_error",
                        )
                        return
                    _publish_stage(
                        pipeline_bus,
                        request_id,
                        "received",
                        received_text,
                        spans=received_spans,
                        kind=cycle_kind,
                        session_id=session_id,
                    )
                    _publish_stage(
                        pipeline_bus,
                        request_id,
                        "tokenized",
                        outbound_text,
                        kind=cycle_kind,
                        session_id=session_id,
                    )
                    _publish_stage(
                        pipeline_bus,
                        request_id,
                        "forwarded",
                        outbound_text,
                        kind=cycle_kind,
                        session_id=session_id,
                    )

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
                    # 1xx, 204 and 304 must not carry a body, chunked or not.
                    bodyless = upstream.status_code in (204, 304) or upstream.status_code < 200
                    if not bodyless:
                        self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    response_started = True

                    content_type = upstream.headers.get("content-type", "")
                    if bodyless:
                        upstream.read()
                        raw_text = out_text = ""
                    elif content_type.startswith(_SSE_CONTENT_TYPE):
                        VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                        with Vault(VAULT_DB_PATH, session_id) as vault:
                            raw_text, out_text = _relay_sse_response(self, upstream, vault)
                    else:
                        raw_text, out_text = _relay_buffered_response(self, upstream, session_id)
                    # A bodyless request (a GET such as `/v1/models`) is not a
                    # turn: publishing only its response stages would create
                    # a panel cycle keyed by the response text and steal the
                    # session's open-turn pointer.
                    if raw_body:
                        for stage, text in (
                            ("response_received", raw_text),
                            ("detokenized", out_text),
                            ("displayed", out_text),
                        ):
                            _publish_stage(
                                pipeline_bus,
                                request_id,
                                stage,
                                text,
                                kind=cycle_kind,
                                session_id=session_id,
                            )
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
            if path == f"{_PANEL_PREFIX}/metrics/comparison" and self.command == "GET":
                self._serve_metrics_comparison_get()
                return True
            if path == f"{_PANEL_PREFIX}/metrics/comparison" and self.command == "POST":
                self._serve_metrics_comparison_post()
                return True
            if path == f"{_PANEL_PREFIX}/hook-event" and self.command == "POST":
                self._serve_hook_event()
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
            subscriber, backlog = pipeline_bus.subscribe()
            try:
                for event in backlog:
                    _write_chunk(self, f"data: {json.dumps(event)}\n\n".encode())
                while True:
                    event = subscriber.get()
                    _write_chunk(self, f"data: {json.dumps(event)}\n\n".encode())
            except OSError:  # BrokenPipeError/ConnectionResetError are OSError subclasses
                return
            finally:
                pipeline_bus.unsubscribe(subscriber)

        def _serve_vault_list(self) -> None:
            """Serve the vault inspector's listing.

            With no ``?session_id=`` query param, lists mappings across
            *every* session in ``VAULT_DB_PATH`` (``list_all_mappings()``) --
            not just whichever session happens to be "current" -- so a
            gateway restart (a fresh fallback session id every time) never
            makes an earlier session's tokens silently disappear from the
            panel. Pass an explicit ``?session_id=`` to scope to just one.
            """
            requested_session_id = _query_param(self.path, "session_id")
            VAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            session_id = requested_session_id or self._default_session_id()
            with Vault(VAULT_DB_PATH, session_id) as vault:
                mappings = (
                    vault.list_mappings() if requested_session_id else vault.list_all_mappings()
                )
            _write_json(
                self,
                200,
                {
                    "session_id": requested_session_id or "",
                    "default_session_id": self._default_session_id(),
                    "mappings": [
                        {
                            "token": m.token,
                            "entity_type": m.entity_type,
                            "ciphertext_b64": m.ciphertext_b64,
                            "created_at": m.created_at,
                            "session_id": m.session_id,
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
            # is to return a decrypted value on request (the panel's
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

        def _serve_metrics_comparison_get(self) -> None:
            _write_json(self, 200, _read_metrics_comparison_cache())

        def _serve_metrics_comparison_post(self) -> None:
            # Genuinely expensive (one real LLM call per manifest entry for
            # the llm/union modes) -- only ever runs on this explicit POST,
            # never automatically, by design. Persisted
            # so the result survives a gateway restart; the panel's default
            # view is whatever this last wrote, not a fresh run every time.
            payload = {
                "available": True,
                "generated_at": datetime.now(UTC).isoformat(),
                "modes": {mode: _run_comparison_mode(mode) for mode in _COMPARISON_MODES},
                "encoder": _run_encoder_comparison(),
            }
            _write_metrics_comparison_cache(payload)
            _write_json(self, 200, payload)

        def _serve_hook_event(self) -> None:
            """Best-effort ingress for a hook's own live progress report.

            Request schema is deliberately minimal --
            ``session_id``/``hook``/``phase``/``tool_name``, all short
            fixed-shape strings -- and structurally incapable of carrying a
            command string, a tool response, or any tokenized/detokenized
            value: there is no field for one, and any extra field in the
            request body is ignored outright, never stored or forwarded, so
            a caller cannot smuggle sensitive text through by adding a field
            this schema does not expect.

            Always responds ``204`` regardless of outcome (unrecognized
            session, no open turn for it, malformed body) -- the reporting
            hook never inspects the response, matching its own best-effort
            contract (``adapters/hooks/_panel_report.py``).
            """
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return
            if not isinstance(payload, dict):
                return
            session_id = payload.get("session_id")
            hook = payload.get("hook")
            phase = payload.get("phase")
            tool_name = payload.get("tool_name")
            if (
                not isinstance(session_id, str)
                or not session_id
                or hook not in _ALLOWED_HOOK_NAMES
                or phase not in _ALLOWED_HOOK_PHASES
                or not isinstance(tool_name, str)
                or not tool_name
            ):
                return
            turn_request_id = pipeline_bus.open_turn_for_session(session_id)
            if turn_request_id is None:
                return
            pipeline_bus.publish(
                {
                    "request_id": turn_request_id,
                    "kind": "hook",
                    "hook": hook,
                    "phase": phase,
                    "tool_name": tool_name,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
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

    global _ENCODER
    if os.environ.get(ENCODER_ENV_VAR, "").strip():
        # Lazy import: torch/gliner only load when an encoder is configured.
        from veil_pii_middleware.core.encoder import encoder_from_env

        _ENCODER = encoder_from_env()

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
