"""Scoring rules of `demo/eval.py`, checked on a one-table corpus with a fake detector.

The seeded corpus is too large to reason about one span at a time, so these
tests build a tiny database and manifest by hand and replace `detect()` with
a function that returns exactly the spans each case needs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import veil_pii_middleware.core.detector as detector
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


class TestDecoyOverlap:
    """E3: a span touching any part of a decoy is a hit."""

    _DECOY_TEXT = "Escalated to the billing department yesterday."

    def _decoys(self, tmp_path: Path) -> tuple[Path, Path]:
        db_path = tmp_path / "decoys.db"
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
            connection.execute("INSERT INTO notes VALUES (1, ?)", (self._DECOY_TEXT,))
        connection.close()
        value = "the billing department"
        decoys_path = tmp_path / "decoys.json"
        decoys_path.write_text(
            json.dumps(
                [
                    {
                        "table": "notes",
                        "column": "body",
                        "row_id": 1,
                        "offset": self._DECOY_TEXT.index(value),
                        "entity_type": "ORG",
                        "value": value,
                        "reason": "generic department, not an organisation",
                    }
                ]
            ),
            encoding="utf-8",
        )
        return db_path, decoys_path

    def _span(self, value: str) -> Span:
        start = self._DECOY_TEXT.index(value)
        return Span(start, start + len(value), EntityType.ORG, value, Detector.LLM)

    def test_partial_overlap_counts_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spans = [self._span("billing"), self._span("department")]
        monkeypatch.setattr(eval_module, "detect", lambda *a, **k: spans)
        report = eval_module.evaluate_decoys(*self._decoys(tmp_path))
        assert len(report.hits) == 1
        assert report.hits[0].entity_type == "ORG"

    def test_an_adjacent_span_is_not_a_hit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spans = [self._span("yesterday")]
        monkeypatch.setattr(eval_module, "detect", lambda *a, **k: spans)
        report = eval_module.evaluate_decoys(*self._decoys(tmp_path))
        assert report.hits == []


class _FakeClient:
    """Stands in for an LLMClient: main() only builds and closes it."""

    def close(self) -> None:
        pass


def _main_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> list[str]:
    db_path, manifest_path = _corpus(tmp_path, [_instance("EMAIL", "rosa@example.com")])
    decoys_path = tmp_path / "decoys.json"
    decoys_path.write_text(
        json.dumps(
            [
                {
                    "table": "notes",
                    "column": "body",
                    "row_id": 1,
                    "offset": _locate("diabetic"),
                    "entity_type": "SENSITIVE",
                    "value": "diabetic",
                    "reason": "test decoy",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_module, "llm_client_from_env", _FakeClient)
    return [
        "--db",
        str(db_path),
        "--manifest",
        str(manifest_path),
        "--decoys",
        str(decoys_path),
        "--mode",
        mode,
    ]


def _fake_detect(spans: Sequence[Span], *, fail: bool = False) -> Any:
    def detect(text: str, **kwargs: Any) -> list[Span]:
        if fail and kwargs.get("on_layer_error") is not None:
            kwargs["on_layer_error"]("llm", ConnectionError("refused"))
        return list(spans)

    return detect


class TestMainExitCodes:
    """E4: only --mode regex gates; a failed layer call is exit 2 in any mode."""

    def test_regex_recall_failure_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _main_args(tmp_path, monkeypatch, "regex")
        monkeypatch.setattr(eval_module, "detect", _fake_detect([]))
        assert eval_module.main(args) == 1
        assert "FAIL — recall below" in capsys.readouterr().out

    def test_regex_decoy_hit_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        args = _main_args(tmp_path, monkeypatch, "regex")
        spans = [
            _span("rosa@example.com", EntityType.EMAIL, Detector.REGEX),
            _span("diabetic", EntityType.SENSITIVE, Detector.REGEX),
        ]
        monkeypatch.setattr(eval_module, "detect", _fake_detect(spans))
        assert eval_module.main(args) == 1

    def test_clean_regex_run_exits_0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        args = _main_args(tmp_path, monkeypatch, "regex")
        spans = [_span("rosa@example.com", EntityType.EMAIL, Detector.REGEX)]
        monkeypatch.setattr(eval_module, "detect", _fake_detect(spans))
        assert eval_module.main(args) == 0

    def test_llm_low_recall_and_decoy_hit_are_report_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _main_args(tmp_path, monkeypatch, "llm")
        spans = [_span("diabetic", EntityType.SENSITIVE)]
        monkeypatch.setattr(eval_module, "detect", _fake_detect(spans))
        assert eval_module.main(args) == 0
        out = capsys.readouterr().out
        assert "FAIL" not in out
        assert "report-only — structured recall below" in out
        assert "report-only — 1 decoy(s) hit" in out

    def test_a_probe_failure_exits_2_before_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _main_args(tmp_path, monkeypatch, "union")
        monkeypatch.setattr(eval_module, "detect", _fake_detect([], fail=True))
        assert eval_module.main(args) == 2
        captured = capsys.readouterr()
        assert "did not answer the probe call" in captured.err
        assert "progress:" not in captured.err

    def test_a_failure_during_the_sweep_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _main_args(tmp_path, monkeypatch, "llm")
        calls = 0

        def detect(text: str, **kwargs: Any) -> list[Span]:
            nonlocal calls
            calls += 1
            if calls > 1:  # the probe answers, the sweep and the decoy pass do not
                kwargs["on_layer_error"]("llm", TimeoutError("slow"))
            return []

        monkeypatch.setattr(eval_module, "detect", detect)
        assert eval_module.main(args) == 2
        captured = capsys.readouterr()
        assert "2 detection layer call(s) failed" in captured.out
        assert captured.err.count("llm layer failed") == 1

    def test_progress_reports_the_first_cell_and_the_decoys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _main_args(tmp_path, monkeypatch, "regex")
        monkeypatch.setattr(eval_module, "detect", _fake_detect([]))
        eval_module.main(args)
        err = capsys.readouterr().err
        assert "progress: 1/1 cells" in err
        assert "progress: 1/1 decoys" in err


class TestEncoderThreshold:
    """E4: --encoder-threshold 0 is honoured and the env var applies with --encoder."""

    @pytest.fixture
    def built(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
        import veil_pii_middleware.core.encoder as encoder_module

        calls: list[tuple[str, float]] = []

        def build(spec: str, *, threshold: float) -> object:
            calls.append((spec, threshold))
            return object()

        monkeypatch.setattr(encoder_module, "build_encoder", build)
        monkeypatch.delenv("VEIL_ENCODER", raising=False)
        monkeypatch.delenv("VEIL_ENCODER_THRESHOLD", raising=False)
        return calls

    def _args(self, encoder: str | None, threshold: float | None) -> argparse.Namespace:
        return argparse.Namespace(encoder=encoder, encoder_threshold=threshold)

    def test_zero_threshold_is_kept(self, built: list[tuple[str, float]]) -> None:
        eval_module._encoder_for(self._args("gliner:m", 0.0))
        assert built == [("gliner:m", 0.0)]

    def test_env_threshold_applies_with_explicit_encoder(
        self, built: list[tuple[str, float]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VEIL_ENCODER_THRESHOLD", "0.7")
        eval_module._encoder_for(self._args("gliner:m", None))
        assert built == [("gliner:m", 0.7)]

    def test_flag_threshold_applies_with_env_encoder(
        self, built: list[tuple[str, float]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VEIL_ENCODER", "gliner:env")
        eval_module._encoder_for(self._args(None, 0.25))
        assert built == [("gliner:env", 0.25)]

    def test_default_threshold_without_flag_or_env(self, built: list[tuple[str, float]]) -> None:
        eval_module._encoder_for(self._args("gliner:m", None))
        assert built == [("gliner:m", 0.4)]

    def test_no_encoder_anywhere_exits(self, built: list[tuple[str, float]]) -> None:
        with pytest.raises(SystemExit, match="needs --encoder"):
            eval_module._encoder_for(self._args(None, None))


class TestDetectLayerErrorHandler:
    """detect() hands swallowed failures to on_layer_error instead of stderr."""

    class _BrokenEncoder:
        identity = "broken"

        def detect_spans(self, text: str) -> list[Span]:
            raise RuntimeError("model not loaded")

    def test_encoder_failure_reaches_the_handler(self, capsys: pytest.CaptureFixture[str]) -> None:
        seen: list[tuple[str, str]] = []
        spans = detector.detect(
            "mail rosa@example.com",
            use_llm=False,
            encoder=self._BrokenEncoder(),
            on_layer_error=lambda layer, exc: seen.append((layer, str(exc))),
        )
        assert seen == [("encoder", "model not loaded")]
        assert [s.entity_type for s in spans] == [EntityType.EMAIL]
        assert capsys.readouterr().err == ""

    def test_llm_failure_reaches_the_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken(text: str, client: object) -> list[Span]:
            raise ConnectionError("refused")

        monkeypatch.setattr(detector, "detect_llm", broken)
        seen: list[str] = []
        detector.detect(
            "hello",
            llm_client=object.__new__(detector.LLMClient),
            on_layer_error=lambda layer, exc: seen.append(layer),
        )
        assert seen == ["llm"]

    def test_without_a_handler_the_failure_is_logged(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        detector.detect("hello", use_llm=False, encoder=self._BrokenEncoder())
        assert "veil detector: encoder layer failed (RuntimeError" in capsys.readouterr().err
