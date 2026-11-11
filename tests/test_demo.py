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
from veil_pii_middleware.core.detector import (
    LLM_BASE_URL_ENV_VAR,
    LLM_MODEL_ENV_VAR,
    STRUCTURED_TYPES,
    EntityType,
)
from veil_pii_middleware.demo.eval import evaluate, llm_client_from_env
from veil_pii_middleware.demo.seed import ADDRESS, DEMO_EMAIL, DOB, ORG, PERSON, seed_demo


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Seed once per module; generating it is the expensive part."""
    directory = tmp_path_factory.mktemp("corpus")
    db_path = directory / "demo.db"
    manifest_path = directory / "manifest.json"
    seed_demo(db_path, manifest_path, directory / "decoys.json")
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
    seed_demo(tmp_path / "a.db", first, tmp_path / "a-decoys.json")
    seed_demo(tmp_path / "b.db", second, tmp_path / "b-decoys.json")
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


def test_demo_customer_rows_carry_many_kinds_of_pii(corpus: tuple[Path, Path]) -> None:
    """The presentation turn must surface a lot of PII, not two or three values.

    Follows the lookups an investigation of the demo email naturally makes:
    the user, their company, its users, invoices, payments and tickets.
    """
    db_path, manifest_path = corpus
    lookups = {
        "users": "SELECT id FROM users WHERE customer_id = ?",
        "customers": "SELECT id FROM customers WHERE id = ?",
        "invoices": "SELECT id FROM invoices WHERE customer_id = ?",
        "payments": "SELECT p.id FROM payments p JOIN invoices i ON i.id = p.invoice_id "
        "WHERE i.customer_id = ?",
        "support_tickets": "SELECT id FROM support_tickets WHERE customer_id = ?",
    }
    with sqlite3.connect(db_path) as connection:
        (customer_id,) = connection.execute(
            "SELECT customer_id FROM users WHERE email = ?", (DEMO_EMAIL,)
        ).fetchone()
        rows = {
            (table, row_id)
            for table, query in lookups.items()
            for (row_id,) in connection.execute(query, (customer_id,))
        }
    found = [i for i in _manifest(manifest_path) if (i["table"], i["row_id"]) in rows]
    assert len({i["value"] for i in found}) >= 15
    assert {i["entity_type"] for i in found} >= {
        EntityType.EMAIL,
        EntityType.PHONE,
        EntityType.DNI,
        EntityType.CIF,
        EntityType.IBAN,
        EntityType.CARD,
        PERSON,
        ORG,
        ADDRESS,
        DOB,
    }


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


# --------------------------------------------------------------------------
# llm_client_from_env (renamed from a private helper so the
# gateway's metrics-comparison endpoint can reuse it instead of duplicating
# this env-reading logic a third time)
# --------------------------------------------------------------------------


def test_llm_client_from_env_raises_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LLM_BASE_URL_ENV_VAR, raising=False)
    monkeypatch.delenv(LLM_MODEL_ENV_VAR, raising=False)
    with pytest.raises(SystemExit, match="requires both"):
        llm_client_from_env()
