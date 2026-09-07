"""Corpus integrity and the measured contract between seed and detector.

The manifest is the ground truth the recall gate is judged against, so these
tests check the manifest itself first. A corpus whose offsets are wrong would
make a perfectly good detector look broken, and a corpus generated differently
on every run would make the gate flaky rather than informative.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from veil_pii_middleware.core.detector import STRUCTURED_TYPES, EntityType
from veil_pii_middleware.demo.eval import evaluate
from veil_pii_middleware.demo.seed import PERSON, seed_demo


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Seed once per module; generating it is the expensive part."""
    directory = tmp_path_factory.mktemp("corpus")
    db_path = directory / "demo.db"
    manifest_path = directory / "manifest.json"
    seed_demo(db_path, manifest_path)
    return db_path, manifest_path


def _manifest(manifest_path: Path) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8"))
    return loaded


# --------------------------------------------------------------------------
# The corpus itself
# --------------------------------------------------------------------------


def test_seed_is_byte_identical_across_runs(tmp_path: Path) -> None:
    """Faker is seeded, so the ground truth must not drift between runs."""
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    seed_demo(tmp_path / "a.db", first)
    seed_demo(tmp_path / "b.db", second)
    assert first.read_bytes() == second.read_bytes()


def test_manifest_offsets_locate_the_value_in_the_database(corpus: tuple[Path, Path]) -> None:
    """Ground-truth integrity, checked without involving the detector."""
    db_path, manifest_path = corpus
    with sqlite3.connect(db_path) as connection:
        for instance in _manifest(manifest_path):
            query = f"SELECT {instance['column']} FROM {instance['table']} WHERE id = ?"  # noqa: S608
            row = connection.execute(query, (instance["row_id"],)).fetchone()
            assert row is not None
            text = str(row[0])
            start = int(instance["offset"])
            assert text[start : start + len(instance["value"])] == instance["value"]


def test_manifest_records_every_required_field(corpus: tuple[Path, Path]) -> None:
    _, manifest_path = corpus
    required = {"table", "column", "row_id", "offset", "entity_type", "value"}
    for instance in _manifest(manifest_path):
        assert required <= set(instance)
        assert instance["value"]


def test_pii_lands_where_column_masking_cannot_reach(corpus: tuple[Path, Path]) -> None:
    """The whole premise: PII outside the obvious columns."""
    _, manifest_path = corpus
    placed = {(i["table"], i["column"], i["entity_type"]) for i in _manifest(manifest_path)}
    assert ("support_tickets", "body", EntityType.EMAIL) in placed
    assert ("webhooks", "payload", EntityType.DNI) in placed
    assert ("error_logs", "message", PERSON) in placed
    assert ("invoices", "notes", EntityType.PHONE) in placed


# --------------------------------------------------------------------------
# The measured contract
# --------------------------------------------------------------------------


def test_every_structured_type_reaches_full_recall(corpus: tuple[Path, Path]) -> None:
    report = evaluate(*corpus)
    assert report.failing_types(1.0) == []


def test_every_structured_type_is_actually_present_in_the_corpus(
    corpus: tuple[Path, Path],
) -> None:
    """A type absent from the manifest would score recall 1.0 vacuously."""
    report = evaluate(*corpus)
    for entity_type in STRUCTURED_TYPES:
        assert report.by_type[entity_type].support > 0


def test_person_names_are_reported_as_an_open_gap(corpus: tuple[Path, Path]) -> None:
    """Names need the LLM layer. They stay in the corpus and stay ungated."""
    report = evaluate(*corpus)
    assert report.by_type[PERSON].recall == 0.0
    assert PERSON not in STRUCTURED_TYPES
    assert report.failing_types(1.0) == []
