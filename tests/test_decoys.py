"""The false-positive gate: decoys that must never be detected.

Every decoy inserted by `demo/seed.py` is either deliberately invalid PII (a
wrong check digit) or a non-PII value that merely resembles PII. None of them
appear in `demo/manifest.json`. Under the current, unmodified detector, the
false-positive count against this known-negative set must stay at zero — and
the sabotage tests below prove that a broken check-digit validator, one that
starts accepting everything, is exactly the kind of regression this gate is
built to catch.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
import veil_pii_middleware.core.detector as detector
import veil_pii_middleware.demo.eval as eval_module
from veil_pii_middleware.core.detector import EntityType, LLMClient, Span
from veil_pii_middleware.demo.eval import evaluate_decoys
from veil_pii_middleware.demo.seed import seed_demo

_Patterns = tuple[tuple[EntityType, re.Pattern[str], Callable[[str], bool] | None], ...]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    """Seed once per module; generating it is the expensive part."""
    directory = tmp_path_factory.mktemp("decoy_corpus")
    db_path = directory / "demo.db"
    manifest_path = directory / "manifest.json"
    decoys_path = directory / "decoys.json"
    seed_demo(db_path, manifest_path, decoys_path)
    return db_path, manifest_path, decoys_path


def _accept_everything(_: str) -> bool:
    return True


def _sabotage(entity_type: EntityType) -> _Patterns:
    """A copy of `_PATTERNS` with the validator for `entity_type` replaced.

    `detect_regex` resolves `_PATTERNS` as a module global at call time, so
    rebinding the module attribute (via `monkeypatch.setattr`) is enough to
    simulate a validator that silently regressed to accepting everything —
    no edit to `core/detector.py` itself is needed or made.
    """
    return tuple(
        (kind, pattern, _accept_everything if kind is entity_type else validator)
        for kind, pattern, validator in detector._PATTERNS
    )


# --------------------------------------------------------------------------
# The decoy set itself
# --------------------------------------------------------------------------


def test_decoys_are_never_recorded_in_the_manifest(corpus: tuple[Path, Path, Path]) -> None:
    _, manifest_path, decoys_path = corpus
    manifest_cells = {
        (i["table"], i["column"], i["row_id"], i["offset"])
        for i in json.loads(manifest_path.read_text(encoding="utf-8"))
    }
    decoy_cells = {
        (d["table"], d["column"], d["row_id"], d["offset"])
        for d in json.loads(decoys_path.read_text(encoding="utf-8"))
    }
    assert manifest_cells.isdisjoint(decoy_cells)
    assert all(table == "decoy_samples" for table, _, _, _ in decoy_cells)


def test_decoy_manifest_is_byte_identical_across_runs(tmp_path: Path) -> None:
    first, second = tmp_path / "a-decoys.json", tmp_path / "b-decoys.json"
    seed_demo(tmp_path / "a.db", tmp_path / "a.json", first)
    seed_demo(tmp_path / "b.db", tmp_path / "b.json", second)
    assert first.read_bytes() == second.read_bytes()


def test_every_decoy_category_is_present(corpus: tuple[Path, Path, Path]) -> None:
    _, _, decoys_path = corpus
    reasons = {
        d["reason"].split(":", 1)[0] for d in json.loads(decoys_path.read_text(encoding="utf-8"))
    }
    assert reasons == {
        "CARD_BAD_LUHN",
        "CIF_BAD_CONTROL",
        "CLOCK_TIME",
        "CPP_SCOPE",
        "DNI_BAD_LETTER",
        "FLOAT_PAIR",
        "HEX_HASH",
        "IBAN_BAD_MOD97",
        "INVALID_OCTET_IP",
        "MASKED_PASSWORD",
        "MASKED_SECRET",
        "NIE_BAD_LETTER",
        "NON_BIRTH_DATE",
        "NSS_BAD_CONTROL",
        "ORDER_NUMBER",
        "PLATE_WITH_VOWELS",
        "ROLE_PHRASE",
        "SOFTWARE_NAME",
        "TECH_HEALTH",
        "THOUSANDS_SEPARATOR",
        "UUID",
        "VERSION_STRING",
        "ZERO_PADDED_ACCOUNT",
    }


# --------------------------------------------------------------------------
# The false-positive gate, unmodified detector
# --------------------------------------------------------------------------


def test_no_decoy_is_detected_under_the_real_validators(
    corpus: tuple[Path, Path, Path],
) -> None:
    db_path, _, decoys_path = corpus
    report = evaluate_decoys(db_path, decoys_path)
    assert report.hits == []
    assert report.exceeds(0) is False


# --------------------------------------------------------------------------
# The decoy check must exercise whatever layer(s) the
# calling --mode enabled, not a hardcoded detect_regex() call
# --------------------------------------------------------------------------


def test_evaluate_decoys_routes_through_detect_with_the_callers_flags(
    corpus: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before this fix, `evaluate_decoys()` always called `detect_regex()`
    directly regardless of `--mode`, so an LLM hallucinating a decoy as valid
    PII went completely unmeasured in every mode. It must now route through
    the same `detect()` `evaluate()` uses, with the caller's own
    `use_regex`/`use_llm`/`llm_client`.
    """
    db_path, _, decoys_path = corpus
    calls: list[tuple[bool, bool, LLMClient | None]] = []

    def _fake_detect(
        text: str,
        *,
        use_regex: bool = True,
        use_llm: bool = True,
        llm_client: LLMClient | None = None,
        encoder: object | None = None,
    ) -> list[Span]:
        calls.append((use_regex, use_llm, llm_client))
        return []

    monkeypatch.setattr(eval_module, "detect", _fake_detect)
    sentinel = object.__new__(LLMClient)

    eval_module.evaluate_decoys(
        db_path, decoys_path, use_regex=False, use_llm=True, llm_client=sentinel
    )

    assert calls, "detect() was never called"
    assert all(call == (False, True, sentinel) for call in calls)


def test_evaluate_decoys_default_matches_regex_only_behavior(
    corpus: tuple[Path, Path, Path],
) -> None:
    """Omitting use_regex/use_llm/llm_client (every pre-existing caller) must
    reproduce today's regex-only behavior exactly.
    """
    db_path, _, decoys_path = corpus
    report = evaluate_decoys(db_path, decoys_path)
    assert report.hits == []


# --------------------------------------------------------------------------
# Sabotage: a broken validator must flip the gate red
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entity_type",
    [EntityType.DNI, EntityType.CARD, EntityType.IBAN],
)
def test_sabotaging_a_check_digit_validator_turns_the_gate_red(
    corpus: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    entity_type: EntityType,
) -> None:
    """The recall gate alone cannot see this: it only ever grows more lenient.

    Making `valid_dni` (or `valid_card`, `valid_iban`) accept every value
    keeps recall at a perfect 1.000 -- every real instance is still found --
    while the corresponding decoy, previously and correctly rejected, is now
    detected. That is exactly the false positive this gate exists to catch.
    """
    db_path, _, decoys_path = corpus
    monkeypatch.setattr(detector, "_PATTERNS", _sabotage(entity_type))

    report = evaluate_decoys(db_path, decoys_path)

    assert any(hit.entity_type == entity_type for hit in report.hits)
    assert report.exceeds(0) is True
