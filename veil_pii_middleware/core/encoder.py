"""Encoder-based NER detectors: an alternative to the LLM layer for soft PII.

The LLM layer asks a generative chat model for JSON spans. An encoder NER
model (GLiNER, a fine-tuned token classifier, ...) is built for exactly that
task instead: it labels spans of the input directly, returns character
offsets, and runs on the GPU in milliseconds. This module wraps a few such
model families behind one small interface (``SpanEncoder`` in
``core.detector``) so ``detect()`` can union their spans with the regex and
LLM layers, and ``demo/eval.py`` can benchmark them side by side.

Heavy dependencies (``torch``, ``gliner``, ``gliner2``, ``transformers``) are
optional (``pip install -e ".[ner]"``) and imported lazily, only when an
encoder is actually built. They ship no type hints, so they are handled as
``Any`` at this boundary and converted to typed ``Span`` objects immediately.

Configured like the LLM layer, from the environment:
``VEIL_ENCODER=<backend>:<model id>`` (backends: ``gliner``, ``gliner2``,
``hf``), optional ``VEIL_ENCODER_THRESHOLD`` (default 0.4) and
``VEIL_ENCODER_DEVICE`` (default ``cuda`` when available).
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from collections.abc import Sequence
from typing import Any, Final

from veil_pii_middleware.core.detector import Detector, EntityType, Span

__all__ = [
    "ENCODER_ENV_VAR",
    "EncoderBase",
    "GlinerEncoder",
    "Gliner2Encoder",
    "TokenClassificationEncoder",
    "build_encoder",
    "encoder_from_env",
]

ENCODER_ENV_VAR: Final = "VEIL_ENCODER"
ENCODER_THRESHOLD_ENV_VAR: Final = "VEIL_ENCODER_THRESHOLD"
ENCODER_DEVICE_ENV_VAR: Final = "VEIL_ENCODER_DEVICE"
DEFAULT_THRESHOLD: Final = 0.4
#: Set to "1" to let an ``hf:`` model repository run its own code (off by default).
TRUST_REMOTE_CODE_ENV_VAR: Final = "VEIL_ENCODER_TRUST_REMOTE_CODE"

#: Zero-shot label prompts for the GLiNER families, mapped to our entity
#: types. Several prompts may map to one type: zero-shot models respond to
#: the wording, and a broader set of phrasings catches more of the free-form
#: "anything that names someone or something identifiable" data the soft
#: layer exists for. The type itself matters little -- every one of these is
#: tokenized -- it only labels the token.
ZERO_SHOT_LABELS: Final[dict[str, EntityType]] = {
    "person": EntityType.PERSON,
    "full name": EntityType.PERSON,
    "organization": EntityType.ORG,
    "company": EntityType.ORG,
    "address": EntityType.ADDRESS,
    "street address": EntityType.ADDRESS,
    "date of birth": EntityType.DOB,
    "username": EntityType.USERNAME,
    "medical condition": EntityType.SENSITIVE,
}

#: Fixed-label token classifiers name their classes differently; map the
#: common spellings (after stripping a ``B-``/``I-`` prefix) to our types.
#: Unknown labels are ignored rather than guessed.
_TOKEN_CLASS_LABELS: Final[dict[str, EntityType]] = {
    "PER": EntityType.PERSON,
    "PERSON": EntityType.PERSON,
    "FIRSTNAME": EntityType.PERSON,
    "LASTNAME": EntityType.PERSON,
    "MIDDLENAME": EntityType.PERSON,
    "FULLNAME": EntityType.PERSON,
    "PRIVATE_PERSON": EntityType.PERSON,
    "NAME": EntityType.PERSON,
    "ORG": EntityType.ORG,
    "ORGANIZATION": EntityType.ORG,
    "ORGANISATION": EntityType.ORG,
    "COMPANY": EntityType.ORG,
    "COMPANYNAME": EntityType.ORG,
    "ADDRESS": EntityType.ADDRESS,
    "PRIVATE_ADDRESS": EntityType.ADDRESS,
    "STREET": EntityType.ADDRESS,
    "STREETADDRESS": EntityType.ADDRESS,
    "BUILDINGNUMBER": EntityType.ADDRESS,
    "SECONDARYADDRESS": EntityType.ADDRESS,
    "ZIPCODE": EntityType.ADDRESS,
    "CITY": EntityType.ADDRESS,
    "DOB": EntityType.DOB,
    "DATEOFBIRTH": EntityType.DOB,
    "BIRTHDATE": EntityType.DOB,
    "USERNAME": EntityType.USERNAME,
    "USER_NAME": EntityType.USERNAME,
}

#: Long cells (support tickets, log batches) are split into overlapping
#: windows: GLiNER-style models truncate at a few hundred words.
_CHUNK_CHARS: Final = 1200
_CHUNK_OVERLAP: Final = 200


def _chunks(text: str) -> list[tuple[int, str]]:
    """Split ``text`` into overlapping windows, cut at whitespace when possible."""
    if len(text) <= _CHUNK_CHARS:
        return [(0, text)]
    out: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end = min(start + _CHUNK_CHARS, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + _CHUNK_CHARS // 2, end)
            end = cut if cut != -1 else end
        out.append((start, text[start:end]))
        if end >= len(text):
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return out


#: One predicted entity: offsets into the window, our type, and the surface
#: text the model reported (``None`` when the backend gives offsets only).
_Triple = tuple[int, int, EntityType, str | None]


def _relocate(window: str, start: int, end: int, surface: str | None) -> tuple[int, int] | None:
    """Trust the offsets only if they point at the reported text; else find it.

    Some backends normalise whitespace internally, so their offsets drift on
    text with repeated spaces. A span that cannot be placed exactly is
    dropped rather than guessed -- a misplaced span would tokenize the wrong
    characters.
    """
    in_range = 0 <= start < end <= len(window)
    if surface is None:
        return (start, end) if in_range else None
    if in_range and window[start:end] == surface:
        return start, end
    best: int | None = None
    position = window.find(surface)
    while position != -1:
        if best is None or abs(position - start) < abs(best - start):
            best = position
        position = window.find(surface, position + 1)
    return (best, best + len(surface)) if best is not None else None


def _default_device() -> str:
    explicit = os.environ.get(ENCODER_DEVICE_ENV_VAR, "").strip()
    if explicit:
        return explicit
    torch: Any = importlib.import_module("torch")
    return "cuda" if torch.cuda.is_available() else "cpu"


class EncoderBase:
    """Shared span conversion, chunking and timing for every backend.

    Subclasses implement ``_predict(texts)`` returning, per input text, a list
    of ``(start, end, label)`` triples with character offsets into that text.
    """

    def __init__(self, model_id: str, threshold: float) -> None:
        self.model_id = model_id
        self.threshold = threshold
        #: Cumulative wall-clock seconds spent in model inference and number
        #: of texts processed -- read by ``demo/eval.py`` for its timing report.
        self.seconds = 0.0
        self.calls = 0

    @property
    def identity(self) -> str:
        return f"{type(self).__name__}|{self.model_id}|{self.threshold}"

    def _predict(self, texts: Sequence[str]) -> list[list[_Triple]]:
        raise NotImplementedError

    def detect_spans(self, text: str) -> list[Span]:
        return self.detect_batch([text])[0]

    def detect_batch(self, texts: Sequence[str]) -> list[list[Span]]:
        """Detect spans in many texts with as few forward passes as possible."""
        windows: list[tuple[int, int, str]] = []  # (text index, offset, window text)
        for index, text in enumerate(texts):
            for offset, window in _chunks(text):
                if window.strip():
                    windows.append((index, offset, window))
        started = time.perf_counter()
        predictions = self._predict([w for _, _, w in windows]) if windows else []
        self.seconds += time.perf_counter() - started
        self.calls += len(texts)

        results: list[list[Span]] = [[] for _ in texts]
        seen: list[set[tuple[int, int]]] = [set() for _ in texts]
        for (index, offset, window), triples in zip(windows, predictions, strict=True):
            source = texts[index]
            for start, end, entity_type, surface in triples:
                located = _relocate(window, start, end, surface)
                if located is None:
                    continue
                abs_start, abs_end = offset + located[0], offset + located[1]
                # Trim whitespace the tokenizer sometimes includes at the edges.
                while abs_start < abs_end and source[abs_start].isspace():
                    abs_start += 1
                while abs_end > abs_start and source[abs_end - 1].isspace():
                    abs_end -= 1
                if abs_end <= abs_start or (abs_start, abs_end) in seen[index]:
                    continue
                seen[index].add((abs_start, abs_end))
                results[index].append(
                    Span(
                        start=abs_start,
                        end=abs_end,
                        entity_type=entity_type,
                        text=source[abs_start:abs_end],
                        detector=Detector.ENCODER,
                    )
                )
        return results


class GlinerEncoder(EncoderBase):
    """GLiNER zero-shot NER (``pip install gliner``), e.g. ``urchade/gliner_multi_pii-v1``."""

    def __init__(
        self,
        model_id: str,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        device: str | None = None,
        labels: dict[str, EntityType] | None = None,
    ) -> None:
        super().__init__(model_id, threshold)
        self.labels = dict(labels or ZERO_SHOT_LABELS)
        gliner: Any = importlib.import_module("gliner")
        self._model: Any = gliner.GLiNER.from_pretrained(model_id).to(device or _default_device())

    def _predict(self, texts: Sequence[str]) -> list[list[_Triple]]:
        label_names = list(self.labels)
        batch: Any = self._model.inference(
            list(texts), label_names, threshold=self.threshold, batch_size=16
        )
        return [
            [
                (int(e["start"]), int(e["end"]), self.labels[e["label"]], e.get("text"))
                for e in entities
                if e["label"] in self.labels
            ]
            for entities in batch
        ]


class Gliner2Encoder(EncoderBase):
    """GLiNER2 (``pip install gliner2``), e.g. ``fastino/gliner2-privacy-filter-PII-multi``."""

    def __init__(
        self,
        model_id: str,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        device: str | None = None,
        labels: dict[str, EntityType] | None = None,
    ) -> None:
        super().__init__(model_id, threshold)
        self.labels = dict(labels or ZERO_SHOT_LABELS)
        gliner2: Any = importlib.import_module("gliner2")
        self._model: Any = gliner2.GLiNER2.from_pretrained(
            model_id, map_location=device or _default_device()
        )

    def _predict(self, texts: Sequence[str]) -> list[list[_Triple]]:
        batch: Any = self._model.batch_extract_entities(
            list(texts),
            list(self.labels),
            batch_size=16,
            threshold=self.threshold,
            include_spans=True,
            include_confidence=True,
        )
        out: list[list[_Triple]] = []
        for result in batch:
            entities: Any = result.get("entities", result) if isinstance(result, dict) else {}
            triples: list[_Triple] = []
            for label, items in entities.items():
                entity_type = self.labels.get(label)
                if entity_type is None:
                    continue
                for item in items:
                    if isinstance(item, dict) and "start" in item and "end" in item:
                        triples.append(
                            (int(item["start"]), int(item["end"]), entity_type, item.get("text"))
                        )
            out.append(triples)
        return out


class TokenClassificationEncoder(EncoderBase):
    """A fixed-label token classifier via ``transformers.pipeline`` (``hf:`` backend)."""

    def __init__(
        self,
        model_id: str,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        device: str | None = None,
        trust_remote_code: bool | None = None,
    ) -> None:
        super().__init__(model_id, threshold)
        # Off unless explicitly enabled: it lets the model repository run its
        # own Python code on this machine.
        if trust_remote_code is None:
            trust_remote_code = os.environ.get(TRUST_REMOTE_CODE_ENV_VAR, "") == "1"
        transformers: Any = importlib.import_module("transformers")
        resolved = device or _default_device()
        self._pipe: Any = transformers.pipeline(
            "token-classification",
            model=model_id,
            aggregation_strategy="simple",
            device=0 if resolved == "cuda" else -1,
            trust_remote_code=trust_remote_code,
        )

    @staticmethod
    def _map_label(raw: str) -> EntityType | None:
        label = raw.upper()
        for prefix in ("B-", "I-", "E-", "S-", "L-", "U-"):
            if label.startswith(prefix):
                label = label[len(prefix) :]
                break
        return _TOKEN_CLASS_LABELS.get(label.replace(" ", "").replace("-", "_"))

    def _predict(self, texts: Sequence[str]) -> list[list[_Triple]]:
        batch: Any = self._pipe(list(texts), batch_size=16)
        if texts and batch and isinstance(batch[0], dict):  # single input -> flat list
            batch = [batch]
        out: list[list[_Triple]] = []
        for entities in batch:
            triples: list[tuple[int, int, EntityType]] = []
            for e in entities:
                entity_type = self._map_label(str(e.get("entity_group", e.get("entity", ""))))
                start, end = e.get("start"), e.get("end")
                if entity_type is None or start is None or end is None:
                    continue
                if float(e.get("score", 1.0)) < self.threshold:
                    continue
                triples.append((int(start), int(end), entity_type))
            out.append([(a, b, t, None) for a, b, t in _merge_adjacent(triples)])
        return out


def _merge_adjacent(
    triples: list[tuple[int, int, EntityType]],
) -> list[tuple[int, int, EntityType]]:
    """Join same-type pieces separated by at most one character (first + last name)."""
    merged: list[tuple[int, int, EntityType]] = []
    for start, end, entity_type in sorted(triples):
        if merged and merged[-1][2] == entity_type and start - merged[-1][1] <= 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]), entity_type)
        else:
            merged.append((start, end, entity_type))
    return merged


_BACKENDS: Final = {
    "gliner": GlinerEncoder,
    "gliner2": Gliner2Encoder,
    "hf": TokenClassificationEncoder,
}


def build_encoder(spec: str, *, threshold: float = DEFAULT_THRESHOLD) -> EncoderBase:
    """Build an encoder from ``<backend>:<model id>``.

    Example: ``gliner:urchade/gliner_multi_pii-v1``.
    """
    backend, sep, model_id = spec.partition(":")
    if not sep or backend not in _BACKENDS or not model_id:
        raise ValueError(
            f"encoder spec must be '<backend>:<model id>' with backend in {sorted(_BACKENDS)}"
        )
    started = time.perf_counter()
    encoder: EncoderBase = _BACKENDS[backend](model_id, threshold=threshold)
    print(
        f"veil encoder: loaded {spec} in {time.perf_counter() - started:.1f}s",
        file=sys.stderr,
    )
    return encoder


def encoder_from_env() -> EncoderBase | None:
    """Build the encoder ``VEIL_ENCODER`` names, or ``None`` when it is unset."""
    spec = os.environ.get(ENCODER_ENV_VAR, "").strip()
    if not spec:
        return None
    raw = os.environ.get(ENCODER_THRESHOLD_ENV_VAR, "").strip()
    return build_encoder(spec, threshold=float(raw) if raw else DEFAULT_THRESHOLD)
