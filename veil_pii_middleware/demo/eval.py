"""Recall and precision of the detector against the ground-truth manifest.

Sweeps queries against the demo database, runs the results through the
detector and reports metrics per entity type and per detector, including the
concrete list of misses.

Matching is strict: a manifest instance counts as detected only when a span
starts at the recorded offset and carries the recorded value. Anything looser
would score a detector that finds *roughly* the right region, and the
tokenizer needs the exact one.

Person names are in the manifest on purpose. No regex can prove them, so they
report as misses until the LLM layer lands — the BRIEF is explicit that false
negatives are published rather than hidden, and they are excluded from the
gated set rather than deleted from the corpus.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from veil_pii_middleware.core.detector import STRUCTURED_TYPES, Span, detect_regex
from veil_pii_middleware.demo.seed import (
    DEFAULT_DB_PATH,
    DEFAULT_DECOYS_PATH,
    DEFAULT_MANIFEST_PATH,
)

__all__ = [
    "DecoyHit",
    "DecoyReport",
    "Metrics",
    "Report",
    "evaluate",
    "evaluate_decoys",
    "main",
]

_MAX_LISTED_MISSES: Final = 10


@dataclass(frozen=True, slots=True)
class Metrics:
    """Counts for one entity type. `support` is what the manifest claims."""

    support: int = 0
    true_positives: int = 0
    false_positives: int = 0

    @property
    def false_negatives(self) -> int:
        return self.support - self.true_positives

    @property
    def recall(self) -> float:
        return self.true_positives / self.support if self.support else 1.0

    @property
    def precision(self) -> float:
        claimed = self.true_positives + self.false_positives
        return self.true_positives / claimed if claimed else 1.0


@dataclass(frozen=True, slots=True)
class Miss:
    table: str
    column: str
    row_id: int
    entity_type: str
    value: str


@dataclass(frozen=True, slots=True)
class Report:
    by_type: dict[str, Metrics]
    by_detector: dict[str, Metrics]
    misses: list[Miss]

    def failing_types(self, min_recall: float) -> list[str]:
        """Structured types whose recall falls below the gate threshold."""
        return sorted(
            name
            for name, metrics in self.by_type.items()
            if name in STRUCTURED_TYPES and metrics.recall < min_recall
        )


def _bump(counters: dict[str, dict[str, int]], key: str, field: str, amount: int = 1) -> None:
    counters[key][field] = counters[key].get(field, 0) + amount


def _load_texts(db_path: Path, cells: set[tuple[str, str, int]]) -> dict[tuple[str, str, int], str]:
    """Fetch exactly the cells the manifest points at, one query per column."""
    texts: dict[tuple[str, str, int], str] = {}
    by_column: dict[tuple[str, str], list[int]] = defaultdict(list)
    for table, column, row_id in cells:
        by_column[(table, column)].append(row_id)

    with sqlite3.connect(db_path) as connection:
        for (table, column), row_ids in sorted(by_column.items()):
            # Identifiers come from the manifest this repo generates, never
            # from user input, and sqlite cannot parameterize them.
            query = f"SELECT id, {column} FROM {table} WHERE id IN ({','.join('?' * len(row_ids))})"  # noqa: S608
            for row_id, value in connection.execute(query, row_ids):
                texts[(table, column, int(row_id))] = str(value)
    return texts


def evaluate(
    db_path: Path = DEFAULT_DB_PATH, manifest_path: Path = DEFAULT_MANIFEST_PATH
) -> Report:
    """Score the regex layer over every cell the manifest says holds PII."""
    manifest: list[dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8"))

    expected: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for instance in manifest:
        expected[(instance["table"], instance["column"], instance["row_id"])].append(instance)

    texts = _load_texts(db_path, set(expected))

    type_counts: dict[str, dict[str, int]] = defaultdict(dict)
    detector_counts: dict[str, dict[str, int]] = defaultdict(dict)
    misses: list[Miss] = []

    for cell, instances in sorted(expected.items()):
        text = texts.get(cell)
        if text is None:
            raise ValueError(f"manifest points at a row that is not in the database: {cell}")
        spans: list[Span] = detect_regex(text)
        # (offset, value) is the identity of a detection, matching how the
        # manifest records ground truth.
        found: set[tuple[int, str]] = {(span.start, span.text) for span in spans}
        claimed: dict[tuple[int, str], Span] = {(span.start, span.text): span for span in spans}

        matched: set[tuple[int, str]] = set()
        for instance in instances:
            entity_type = str(instance["entity_type"])
            key = (int(instance["offset"]), str(instance["value"]))
            _bump(type_counts, entity_type, "support")
            if key in found and claimed[key].entity_type == entity_type:
                _bump(type_counts, entity_type, "true_positives")
                _bump(detector_counts, str(claimed[key].detector), "true_positives")
                _bump(detector_counts, str(claimed[key].detector), "support")
                matched.add(key)
            else:
                misses.append(
                    Miss(
                        table=cell[0],
                        column=cell[1],
                        row_id=cell[2],
                        entity_type=entity_type,
                        value=str(instance["value"]),
                    )
                )

        for key, span in claimed.items():
            if key not in matched:
                _bump(type_counts, str(span.entity_type), "false_positives")
                _bump(detector_counts, str(span.detector), "false_positives")

    by_type = {name: Metrics(**counts) for name, counts in sorted(type_counts.items())}
    by_detector = {name: Metrics(**counts) for name, counts in sorted(detector_counts.items())}
    return Report(by_type=by_type, by_detector=by_detector, misses=misses)


@dataclass(frozen=True, slots=True)
class DecoyHit:
    """A decoy that the regex layer detected anyway -- a false positive.

    Every decoy is either deliberately invalid PII (a wrong check digit) or a
    non-PII value that merely resembles PII. Under correct operation this
    list is always empty; a hit here means the layer, or a validator inside
    it, just started accepting something it must reject.
    """

    table: str
    column: str
    row_id: int
    entity_type: str
    value: str
    reason: str


@dataclass(frozen=True, slots=True)
class DecoyReport:
    hits: list[DecoyHit]

    def exceeds(self, max_false_positives: int) -> bool:
        return len(self.hits) > max_false_positives


def evaluate_decoys(
    db_path: Path = DEFAULT_DB_PATH, decoys_path: Path = DEFAULT_DECOYS_PATH
) -> DecoyReport:
    """Score the regex layer against values that must never be detected.

    `decoys_path` is a known-negative set, symmetric to the manifest's
    known-positive one: none of these values appear in the manifest, and a
    span landing on one of them is a false positive by definition, not a
    judgement call the way an unmatched span in `evaluate()` can be.
    """
    decoys: list[dict[str, Any]] = json.loads(decoys_path.read_text(encoding="utf-8"))
    cells = {(str(d["table"]), str(d["column"]), int(d["row_id"])) for d in decoys}
    texts = _load_texts(db_path, cells)

    hits: list[DecoyHit] = []
    for decoy in decoys:
        cell = (str(decoy["table"]), str(decoy["column"]), int(decoy["row_id"]))
        text = texts.get(cell)
        if text is None:
            raise ValueError(f"decoy points at a row that is not in the database: {cell}")
        offset, value = int(decoy["offset"]), str(decoy["value"])
        for span in detect_regex(text):
            if span.start == offset and span.text == value:
                hits.append(
                    DecoyHit(
                        table=cell[0],
                        column=cell[1],
                        row_id=cell[2],
                        entity_type=str(span.entity_type),
                        value=value,
                        reason=str(decoy["reason"]),
                    )
                )
    return DecoyReport(hits=hits)


def _print_report(report: Report, min_recall: float) -> None:
    print(f"{'entity':<10}{'support':>9}{'found':>8}{'missed':>8}{'fp':>6}{'recall':>9}{'prec':>8}")
    print("-" * 58)
    for name, metrics in report.by_type.items():
        gated = "" if name in STRUCTURED_TYPES else "  (not gated)"
        print(
            f"{name:<10}{metrics.support:>9}{metrics.true_positives:>8}"
            f"{metrics.false_negatives:>8}{metrics.false_positives:>6}"
            f"{metrics.recall:>9.3f}{metrics.precision:>8.3f}{gated}"
        )

    print()
    for name, metrics in report.by_detector.items():
        print(
            f"detector {name:<6} found {metrics.true_positives}, "
            f"false positives {metrics.false_positives}"
        )

    gated_misses = [miss for miss in report.misses if miss.entity_type in STRUCTURED_TYPES]
    if gated_misses:
        print(f"\n{len(gated_misses)} gated miss(es); first {_MAX_LISTED_MISSES}:")
        for miss in gated_misses[:_MAX_LISTED_MISSES]:
            print(
                f"  {miss.entity_type:<6} {miss.table}.{miss.column}#{miss.row_id} {miss.value!r}"
            )

    failing = report.failing_types(min_recall)
    if failing:
        print(f"\nFAIL — recall below {min_recall:.2f} for: {', '.join(failing)}")
    else:
        print(f"\nPASS — every structured entity type is at or above {min_recall:.2f} recall")


def _print_decoy_report(decoy_report: DecoyReport, max_false_positives: int) -> None:
    print(
        f"\ndecoys: {len(decoy_report.hits)} false positive(s) (max allowed {max_false_positives})"
    )
    for hit in decoy_report.hits:
        print(
            f"  {hit.entity_type:<6} {hit.table}.{hit.column}#{hit.row_id} "
            f"{hit.value!r} -- {hit.reason}"
        )

    if decoy_report.exceeds(max_false_positives):
        print(f"FAIL — {len(decoy_report.hits)} decoy(s) detected, budget is {max_false_positives}")
    else:
        print(f"PASS — decoys at or below the {max_false_positives} false-positive budget")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the detector against the manifest.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--decoys", type=Path, default=DEFAULT_DECOYS_PATH)
    parser.add_argument(
        "--min-recall",
        type=float,
        default=1.0,
        help="Fail when any structured entity type scores below this recall.",
    )
    parser.add_argument(
        "--max-false-positives",
        type=int,
        default=0,
        help="Fail when more than this many decoys are detected.",
    )
    args = parser.parse_args(argv)

    report = evaluate(args.db, args.manifest)
    _print_report(report, args.min_recall)

    decoy_report = evaluate_decoys(args.db, args.decoys)
    _print_decoy_report(decoy_report, args.max_false_positives)

    recall_failed = bool(report.failing_types(args.min_recall))
    decoys_failed = decoy_report.exceeds(args.max_false_positives)
    return 1 if recall_failed or decoys_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
