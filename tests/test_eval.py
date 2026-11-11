"""Scoring rules of `demo/eval.py`, checked on a one-table corpus with a fake detector.

The seeded corpus is too large to reason about one span at a time, so these
tests build a tiny database and manifest by hand and replace `detect()` with
a function that returns exactly the spans each case needs.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import veil_pii_middleware.demo.eval as eval_module
from veil_pii_middleware.core.detector import Detector, EntityType, Span
from veil_pii_middleware.demo.eval import Report, evaluate

_TEXT = "Rosa Pérez Gil is diabetic, email rosa@example.com, see Rosendo Cifuentes Rojas."


def _locate(value: str) -> int:
    offset = _TEXT.find(value)
    assert offset >= 0, value
    return offset


def _instance(entity_type: str, value: str) -> dict[str, Any]:
    return {
        "table": "notes",
        "column": "body",
        "row_id": 1,
        "offset": _locate(value),
        "entity_type": entity_type,
        "value": value,
    }


def _span(value: str, entity_type: EntityType, detector: Detector = Detector.LLM) -> Span:
    start = _locate(value)
    return Span(start, start + len(value), entity_type, value, detector)


def _corpus(tmp_path: Path, instances: Sequence[dict[str, Any]]) -> tuple[Path, Path]:
    db_path = tmp_path / "eval.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO notes VALUES (1, ?)", (_TEXT,))
    connection.close()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(list(instances)), encoding="utf-8")
    return db_path, manifest_path


def _evaluate_with(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    instances: Sequence[dict[str, Any]],
    spans: Sequence[Span],
) -> Report:
    monkeypatch.setattr(eval_module, "detect", lambda *a, **k: list(spans))
    return evaluate(*_corpus(tmp_path, instances))


_NAME = "Rosendo Cifuentes Rojas"


class TestSoftCoverage:
    """E1: soft values need the union of their spans to cover at least 90%."""

    def test_two_of_three_name_tokens_is_a_miss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = _evaluate_with(
            tmp_path,
            monkeypatch,
            [_instance("PERSON", _NAME)],
            [_span("Rosendo Cifuentes", EntityType.PERSON)],
        )
        assert report.by_type["PERSON"].true_positives == 0
        assert [miss.value for miss in report.misses] == [_NAME]

    def test_a_name_split_into_two_spans_is_a_hit_without_false_positives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = _evaluate_with(
            tmp_path,
            monkeypatch,
            [_instance("PERSON", _NAME)],
            [_span("Rosendo", EntityType.PERSON), _span("Cifuentes Rojas", EntityType.PERSON)],
        )
        assert report.by_type["PERSON"].true_positives == 1
        assert report.by_type["PERSON"].false_positives == 0
        assert report.by_detector["LLM"].true_positives == 1

    def test_three_short_spans_covering_the_whole_name_are_a_hit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = _evaluate_with(
            tmp_path,
            monkeypatch,
            [_instance("PERSON", _NAME)],
            [
                _span("Rosendo", EntityType.PERSON),
                _span("Cifuentes", EntityType.PERSON),
                _span("Rojas", EntityType.PERSON),
            ],
        )
        assert report.by_type["PERSON"].true_positives == 1

    def test_a_partial_span_on_a_real_value_is_not_a_false_positive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = _evaluate_with(
            tmp_path,
            monkeypatch,
            [_instance("PERSON", _NAME)],
            [_span("Rosendo", EntityType.PERSON)],
        )
        assert report.by_type["PERSON"].true_positives == 0
        assert report.by_type["PERSON"].false_positives == 0

    def test_a_span_on_no_value_is_a_false_positive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = _evaluate_with(
            tmp_path,
            monkeypatch,
            [_instance("PERSON", _NAME)],
            [_span(_NAME, EntityType.PERSON), _span("diabetic", EntityType.SENSITIVE)],
        )
        assert report.by_type["PERSON"].true_positives == 1
        assert report.by_type["SENSITIVE"].false_positives == 1


class TestOneValuePerSpan:
    """E2: a span stretched over several values is credited to one of them."""

    def test_a_span_over_two_values_detects_only_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = _evaluate_with(
            tmp_path,
            monkeypatch,
            [_instance("PERSON", "Rosa Pérez Gil"), _instance("SENSITIVE", "diabetic")],
            [_span("Rosa Pérez Gil is diabetic", EntityType.PERSON, Detector.ENCODER)],
        )
        found = report.by_type["PERSON"].true_positives + report.by_type["SENSITIVE"].true_positives
        assert found == 1
        assert report.by_detector["ENCODER"].true_positives == 1
        assert len(report.misses) == 1

    def test_a_whole_cell_span_does_not_take_every_soft_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = _evaluate_with(
            tmp_path,
            monkeypatch,
            [
                _instance("PERSON", "Rosa Pérez Gil"),
                _instance("SENSITIVE", "diabetic"),
                _instance("PERSON", _NAME),
            ],
            [_span(_TEXT, EntityType.PERSON)],
        )
        found = sum(m.true_positives for m in report.by_type.values())
        assert found == 1

    def test_structured_values_still_need_an_exact_typed_span(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = _evaluate_with(
            tmp_path,
            monkeypatch,
            [_instance("EMAIL", "rosa@example.com")],
            [_span("rosa@example.com", EntityType.PERSON)],
        )
        assert report.by_type["EMAIL"].true_positives == 0
        assert report.by_type["PERSON"].false_positives == 1
