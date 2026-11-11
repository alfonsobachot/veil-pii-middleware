"""Recall and precision of the detector against the ground-truth manifest.

Sweeps queries against the demo database, runs the results through the
detector and reports metrics per entity type and per detector, including the
concrete list of misses.

Structured matching is strict: a structured instance (email, DNI, card...)
counts as detected only when a span starts at the recorded offset, carries the
recorded value and has the recorded type. Soft instances (names,
organisations, addresses...) count as detected when the soft spans credited to
them cover at least 90% of their alphanumeric characters, since soft layers
disagree on boundaries. Each span is credited to at most one instance.

Person names are in the manifest on purpose. No regex can prove them, so they
report as misses in regex-only mode — false
negatives are deliberately published rather than hidden, and they are excluded from the
gated set rather than deleted from the corpus. ``--mode llm``/``--mode union``
run the same sweep through the LLM layer instead of (or in addition to) regex,
so this is the concrete comparison: the 100%-recall gate stays bound to
``--mode regex`` (``STRUCTURED_TYPES`` only), since an LLM-only run is not
expected to reach it and gating it would leave the gate permanently red.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from veil_pii_middleware.core.detector import (
    LLM_BASE_URL_ENV_VAR,
    LLM_MODEL_ENV_VAR,
    STRUCTURED_TYPES,
    LLMClient,
    Span,
    SpanEncoder,
    detect,
)
from veil_pii_middleware.core.detector import (
    llm_client_from_env as _detector_llm_client_from_env,
)
from veil_pii_middleware.demo.seed import (
    DEFAULT_DB_PATH,
    DEFAULT_DECOYS_PATH,
    DEFAULT_MANIFEST_PATH,
)

#: Which layers a run enables. "regex" is today's default and the only mode
#: the recall gate is bound to; "llm"/"union" report, they do not gate.
Mode = Literal["regex", "llm", "union", "encoder", "regex+encoder"]

#: Modes that need an LLM client / an encoder.
LLM_MODES: Final = frozenset({"llm", "union"})
ENCODER_MODES: Final = frozenset({"encoder", "regex+encoder"})
REGEX_MODES: Final = frozenset({"regex", "union", "regex+encoder"})

__all__ = [
    "DecoyHit",
    "DecoyReport",
    "Metrics",
    "Report",
    "evaluate",
    "evaluate_decoys",
    "llm_client_from_env",
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
    #: Wall-clock seconds the detection calls took, and how many cells.
    seconds: float = 0.0
    cells: int = 0

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

    # `with sqlite3.connect(...)` only commits/rolls back on exit -- it does
    # NOT close the connection (a common Python sqlite3 pitfall). That never
    # mattered for eval.py's own short-lived CLI process, but the gateway's
    # `/veil-panel/metrics` endpoint calls this repeatedly from one
    # long-running process, so a real close is needed or the file handle
    # outlives this call and blocks `seed.py` from regenerating the database
    # (observed: `PermissionError` on Windows).
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        for (table, column), row_ids in sorted(by_column.items()):
            # Identifiers come from the manifest this repo generates, never
            # from user input, and sqlite cannot parameterize them.
            query = f"SELECT id, {column} FROM {table} WHERE id IN ({','.join('?' * len(row_ids))})"  # noqa: S608
            for row_id, value in connection.execute(query, row_ids):
                texts[(table, column, int(row_id))] = str(value)
    return texts


def evaluate(
    db_path: Path = DEFAULT_DB_PATH,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    *,
    mode: Mode = "regex",
    llm_client: LLMClient | None = None,
    encoder: SpanEncoder | None = None,
) -> Report:
    """Score the enabled layer(s) over every cell the manifest says holds PII.

    ``mode="regex"`` (the default) matches every prior behavior of this
    function exactly. ``mode="llm"``/``"union"`` require ``llm_client``; a
    result is cached per content block by ``core.detector``, so a value
    repeated across cells is only sent to the model once.
    """
    if mode in LLM_MODES and llm_client is None:
        raise ValueError(f"mode={mode!r} requires an llm_client")
    if mode in ENCODER_MODES and encoder is None:
        raise ValueError(f"mode={mode!r} requires an encoder")
    use_regex = mode in REGEX_MODES
    use_llm = mode in LLM_MODES
    active_encoder = encoder if mode in ENCODER_MODES else None

    manifest: list[dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8"))

    expected: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for instance in manifest:
        expected[(instance["table"], instance["column"], instance["row_id"])].append(instance)

    texts = _load_texts(db_path, set(expected))

    type_counts: dict[str, dict[str, int]] = defaultdict(dict)
    detector_counts: dict[str, dict[str, int]] = defaultdict(dict)
    misses: list[Miss] = []
    seconds = 0.0

    total_cells = len(expected)
    for done, (cell, instances) in enumerate(sorted(expected.items()), start=1):
        if done % 25 == 0 or done == total_cells:
            # Progress on stderr: an LLM sweep takes minutes, never leave it silent.
            print(f"progress: {done}/{total_cells} cells", file=sys.stderr, flush=True)
        text = texts.get(cell)
        if text is None:
            raise ValueError(f"manifest points at a row that is not in the database: {cell}")
        started = time.perf_counter()
        spans: list[Span] = detect(
            text,
            use_regex=use_regex,
            use_llm=use_llm,
            llm_client=llm_client,
            encoder=active_encoder,
        )
        seconds += time.perf_counter() - started
        # Each span is credited to at most one ground-truth value, so a single
        # span stretched over several values (or a whole cell) cannot collect
        # recall for all of them.
        credited: set[int] = set()
        soft_instances: list[dict[str, Any]] = []
        for instance in instances:
            entity_type = str(instance["entity_type"])
            _bump(type_counts, entity_type, "support")
            if entity_type not in STRUCTURED_TYPES:
                soft_instances.append(instance)
                continue
            exact = _exact_structured_span(instance, spans, credited)
            if exact is None:
                misses.append(_miss(cell, instance))
                continue
            credited.add(exact)
            _bump(type_counts, entity_type, "true_positives")
            _bump(detector_counts, str(spans[exact].detector), "true_positives")
            _bump(detector_counts, str(spans[exact].detector), "support")

        assigned = _assign_soft_spans(soft_instances, spans, credited)
        for index, instance in enumerate(soft_instances):
            own = assigned.get(index, [])
            # A span on a real value is never a false positive, even when it
            # covers too little of it for the value to count as detected.
            credited.update(own)
            if not own or _soft_coverage(instance, [spans[i] for i in own]) < _SOFT_MIN_COVERAGE:
                misses.append(_miss(cell, instance))
                continue
            _bump(type_counts, str(instance["entity_type"]), "true_positives")
            for detector in sorted({str(spans[i].detector) for i in own}):
                _bump(detector_counts, detector, "true_positives")
                _bump(detector_counts, detector, "support")

        for index, span in enumerate(spans):
            if index not in credited:
                _bump(type_counts, str(span.entity_type), "false_positives")
                _bump(detector_counts, str(span.detector), "false_positives")

    by_type = {name: Metrics(**counts) for name, counts in sorted(type_counts.items())}
    by_detector = {name: Metrics(**counts) for name, counts in sorted(detector_counts.items())}
    return Report(
        by_type=by_type,
        by_detector=by_detector,
        misses=misses,
        seconds=seconds,
        cells=len(expected),
    )


#: A soft ground-truth value counts as detected when the soft spans credited
#: to it, together, cover at least this share of its alphanumeric characters.
#: Soft layers disagree on boundaries ("is diabetic" vs "diabetic"), so the
#: union of spans is what counts: a name split into "Rosendo" and "Cifuentes
#: Rojas" is detected, while "Rosendo Cifuentes" alone leaves the second
#: surname to leak and is a miss.
_SOFT_MIN_COVERAGE: Final = 0.9


def _miss(cell: tuple[str, str, int], instance: dict[str, Any]) -> Miss:
    return Miss(
        table=cell[0],
        column=cell[1],
        row_id=cell[2],
        entity_type=str(instance["entity_type"]),
        value=str(instance["value"]),
    )


def _exact_structured_span(
    instance: dict[str, Any], spans: list[Span], credited: set[int]
) -> int | None:
    """Index of an uncredited span with the instance's exact offset, value and type."""
    offset, value = int(instance["offset"]), str(instance["value"])
    entity_type = str(instance["entity_type"])
    for index, span in enumerate(spans):
        if (
            index not in credited
            and span.start == offset
            and span.text == value
            and _type_matches(entity_type, str(span.entity_type))
        ):
            return index
    return None


def _assign_soft_spans(
    instances: list[dict[str, Any]], spans: list[Span], credited: set[int]
) -> dict[int, list[int]]:
    """Give each uncredited soft span to the soft value it overlaps most.

    Returns, per index into ``instances``, the indexes of the spans assigned
    to it. A span that overlaps no soft value is left unassigned and later
    counts as a false positive. Ties go to the earlier value.
    """
    assigned: dict[int, list[int]] = defaultdict(list)
    for span_index, span in enumerate(spans):
        if span_index in credited or str(span.entity_type) in STRUCTURED_TYPES:
            continue
        best: int | None = None
        best_overlap = 0
        for instance_index, instance in enumerate(instances):
            start = int(instance["offset"])
            end = start + len(str(instance["value"]))
            overlap = min(end, span.end) - max(start, span.start)
            if overlap > best_overlap:
                best, best_overlap = instance_index, overlap
        if best is not None:
            assigned[best].append(span_index)
    return assigned


def _soft_coverage(instance: dict[str, Any], spans: list[Span]) -> float:
    """Share of the value's alphanumeric characters covered by the union of ``spans``."""
    start, value = int(instance["offset"]), str(instance["value"])
    positions = [start + i for i, char in enumerate(value) if char.isalnum()]
    if not positions:
        positions = list(range(start, start + len(value)))
    covered = sum(1 for pos in positions if any(s.start <= pos < s.end for s in spans))
    return covered / len(positions)


def _type_matches(expected: str, claimed: str) -> bool:
    """Structured types must match exactly; soft types match each other.

    For the soft layer the label is cosmetic -- a company tagged PERSON is
    still tokenized and never leaves the machine -- so any soft label on the
    right span counts as detected. Structured types keep the strict check:
    a DNI labelled NIE is a real defect in a check-digit-provable type.
    """
    if expected in STRUCTURED_TYPES or claimed in STRUCTURED_TYPES:
        return expected == claimed
    return True


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
    db_path: Path = DEFAULT_DB_PATH,
    decoys_path: Path = DEFAULT_DECOYS_PATH,
    *,
    use_regex: bool = True,
    use_llm: bool = False,
    llm_client: LLMClient | None = None,
    encoder: SpanEncoder | None = None,
) -> DecoyReport:
    """Score the enabled layer(s) against values that must never be detected.

    `decoys_path` is a known-negative set, symmetric to the manifest's
    known-positive one: none of these values appear in the manifest, and a
    span landing on one of them is a false positive by definition, not a
    judgement call the way an unmatched span in `evaluate()` can be.

    `use_regex`/`use_llm`/`llm_client` mirror `evaluate()`'s own -- routing
    through `detect()` instead of a hardcoded regex-only call means a
    `--mode llm`/`--mode union` run's decoy check actually exercises the LLM
    layer too (previously this check never did, in any mode, so an
    LLM hallucinating a decoy as valid would have gone completely unmeasured).
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
        spans = detect(
            text, use_regex=use_regex, use_llm=use_llm, llm_client=llm_client, encoder=encoder
        )
        for span in spans:
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


def llm_client_from_env() -> LLMClient:
    """Build an ``LLMClient`` from the environment, or exit if unconfigured.

    Shared by ``main()``'s LLM modes and the gateway's
    ``/veil-panel/metrics/comparison`` endpoint; wraps
    ``core.detector.llm_client_from_env`` so every caller reads the same
    variables (including the optional reasoning-effort and timeout ones).
    """
    client = _detector_llm_client_from_env()
    if client is None:
        raise SystemExit(
            f"--mode requires both {LLM_BASE_URL_ENV_VAR} and {LLM_MODEL_ENV_VAR} to be set"
        )
    return client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the detector against the manifest.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--decoys", type=Path, default=DEFAULT_DECOYS_PATH)
    parser.add_argument(
        "--mode",
        choices=("regex", "llm", "union", "encoder", "regex+encoder"),
        default="regex",
        help=(
            "Which layer(s) to score. Only 'regex' (the default) gates "
            "--min-recall/--max-false-positives; 'llm'/'union' are report-only "
            f"and read {LLM_BASE_URL_ENV_VAR}/{LLM_MODEL_ENV_VAR}; 'encoder'/'regex+encoder' "
            "read VEIL_ENCODER or --encoder."
        ),
    )
    parser.add_argument(
        "--encoder",
        default=None,
        help="Encoder spec '<backend>:<model id>' (overrides VEIL_ENCODER), e.g. "
        "gliner:urchade/gliner_multi_pii-v1.",
    )
    parser.add_argument(
        "--encoder-threshold",
        type=float,
        default=None,
        help="Confidence threshold for the encoder (default 0.4 or VEIL_ENCODER_THRESHOLD).",
    )
    parser.add_argument(
        "--min-recall",
        type=float,
        default=1.0,
        help="Fail when any structured entity type scores below this recall (--mode regex only).",
    )
    parser.add_argument(
        "--max-false-positives",
        type=int,
        default=0,
        help="Fail when more than this many decoys are detected (--mode regex only).",
    )
    args = parser.parse_args(argv)
    mode: Mode = args.mode

    use_regex = mode in REGEX_MODES
    use_llm = mode in LLM_MODES
    llm_client = llm_client_from_env() if use_llm else None
    encoder = _encoder_for(args) if mode in ENCODER_MODES else None
    try:
        report = evaluate(args.db, args.manifest, mode=mode, llm_client=llm_client, encoder=encoder)
        _print_report(report, args.min_recall)
        per_cell_ms = 1000 * report.seconds / report.cells if report.cells else 0.0
        print(
            f"\ntiming: {report.seconds:.1f}s over {report.cells} cells ({per_cell_ms:.1f} ms/cell)"
        )

        # Same use_regex/use_llm/llm_client this run already resolved for
        # evaluate() above -- previously this call
        # always hardcoded detect_regex(), so an LLM hallucinating a decoy
        # as valid PII went completely unmeasured in every --mode. Now
        # --mode llm/union's decoy check exercises the LLM layer too, the
        # same layer(s) that mode's recall numbers reflect.
        decoy_report = evaluate_decoys(
            args.db,
            args.decoys,
            use_regex=use_regex,
            use_llm=use_llm,
            llm_client=llm_client,
            encoder=encoder,
        )
        _print_decoy_report(decoy_report, args.max_false_positives)
    finally:
        if llm_client is not None:
            llm_client.close()

    # The recall gate is scoped to --mode regex only (an LLM-only/union run
    # is not expected to reach it and gating it would leave the gate
    # permanently red). The decoy gate still runs and still fails the
    # process in every mode, but it now scores
    # whichever layer(s) that mode enabled (regex-only, LLM-only, or the
    # union) -- symmetric with what that mode's recall numbers reflect,
    # rather than always silently checking regex alone.
    recall_failed = mode == "regex" and bool(report.failing_types(args.min_recall))
    decoys_failed = decoy_report.exceeds(args.max_false_positives)
    if mode != "regex":
        print(
            f"\n--mode {mode}'s recall is report-only (exit code reflects --mode regex's "
            f"recall gate only); the decoy false-positive gate still applies, scored against "
            f"--mode {mode}'s own enabled layer(s)."
        )
    return 1 if recall_failed or decoys_failed else 0


def _encoder_for(args: argparse.Namespace) -> SpanEncoder:
    # Lazy import: the encoder's heavy optional dependencies only load here.
    from veil_pii_middleware.core.encoder import DEFAULT_THRESHOLD, build_encoder, encoder_from_env

    if args.encoder:
        threshold = args.encoder_threshold
        return build_encoder(args.encoder, threshold=threshold or DEFAULT_THRESHOLD)
    encoder = encoder_from_env()
    if encoder is None:
        raise SystemExit("--mode encoder needs --encoder or VEIL_ENCODER")
    return encoder


if __name__ == "__main__":
    raise SystemExit(main())
