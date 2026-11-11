"""Tests for veil_pii_middleware.core.audit.

Covers the append/read round trip, the no-plaintext-ever invariant (BRIEF
SS3.4: "otherwise we would be creating a file full of clear-text PII in order
to audit that we do not leak PII"), and cross-writer append safety, since
`.veil/audit.jsonl` is written by up to three separate OS processes (gateway,
a hook invocation, `/veil`) in normal use.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from veil_pii_middleware.core.audit import AuditLog
from veil_pii_middleware.core.detector import Detector, EntityType
from veil_pii_middleware.core.tokenizer import tokenize
from veil_pii_middleware.core.vault import Vault

VALID_EMAIL = "alice@example.com"

#: Run as a real OS process via `python -c` (see TestConcurrentAppendSafety)
#: -- a `threading.Lock` cannot prove cross-process safety, since threads of
#: one process already share the same lock object, and this project's own
#: `.veil/audit.jsonl` is written by up to three separate processes (gateway,
#: a hook invocation, `/veil`) in normal use, never by threads of one.
_CONCURRENT_WORKER_SCRIPT = """
import sys
from veil_pii_middleware.core.audit import AuditLog
from veil_pii_middleware.core.detector import Detector, EntityType

log_path, worker_id, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
log = AuditLog(log_path)
for i in range(count):
    log.record(
        token=f"PII_EMAIL_{i:04d}",
        entity_type=EntityType.EMAIL,
        detector=Detector.REGEX,
        offset=i,
        adapter=f"process-{worker_id}",
    )
"""
VALID_DNI = "12345678Z"


class TestRecordNeverWritesPlaintext:
    def test_tokenizing_writes_neither_the_value_nor_its_hash(self, tmp_path: Path) -> None:
        """An unkeyed SHA-256 of a DNI is brute-forced in about a minute, so no hash either."""
        log_path = tmp_path / "audit.jsonl"
        text = f"Customer {VALID_EMAIL}, DNI {VALID_DNI}"
        with Vault(tmp_path / "vault.db", "session-1", master_key=os.urandom(32)) as vault:
            tokenize(text, vault, audit_log=AuditLog(log_path), adapter="test")

        raw = log_path.read_text(encoding="utf-8")
        assert len(raw.splitlines()) == 2
        for value in (VALID_EMAIL, VALID_DNI):
            assert value not in raw
            assert hashlib.sha256(value.encode()).hexdigest() not in raw


class TestEntryShape:
    def test_one_line_has_exactly_the_documented_fields(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(log_path)

        log.record(
            token="PII_EMAIL_0001",
            entity_type=EntityType.EMAIL,
            detector=Detector.LLM,
            offset=3,
            adapter="gateway",
        )

        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert set(entry.keys()) == {
            "timestamp",
            "token",
            "entity_type",
            "detector",
            "offset",
            "adapter",
        }
        assert entry["token"] == "PII_EMAIL_0001"
        assert entry["entity_type"] == "EMAIL"
        assert entry["detector"] == "LLM"
        assert entry["offset"] == 3
        assert entry["adapter"] == "gateway"

    def test_read_entries_round_trips(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(log_path)
        log.record(
            token="PII_EMAIL_0001",
            entity_type=EntityType.EMAIL,
            detector=Detector.REGEX,
            offset=0,
            adapter="test",
        )
        log.record(
            token="PII_DNI_0001",
            entity_type=EntityType.DNI,
            detector=Detector.REGEX,
            offset=10,
            adapter="test",
        )

        entries = list(AuditLog.read_entries(log_path))

        assert [e.token for e in entries] == ["PII_EMAIL_0001", "PII_DNI_0001"]
        assert entries[0].entity_type == "EMAIL"

    def test_reading_a_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        assert list(AuditLog.read_entries(tmp_path / "does-not-exist.jsonl")) == []


class TestConcurrentAppendSafety:
    def test_interleaved_writers_never_corrupt_a_line(self, tmp_path: Path) -> None:
        """Real OS processes racing to append -- not threads of one process.

        Plain append-mode writes with only an in-process lock were measured
        (empirically, on this project's own Windows target) to lose and
        corrupt lines under exactly this scenario before `AuditLog` gained
        its cross-process sidecar-file lock. This proves the fix, not just
        the absence of a single-process race.
        """
        log_path = tmp_path / "audit.jsonl"
        writes_per_worker = 150
        worker_count = 5

        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _CONCURRENT_WORKER_SCRIPT,
                    str(log_path),
                    str(worker_id),
                    str(writes_per_worker),
                ],
                stderr=subprocess.PIPE,
            )
            for worker_id in range(worker_count)
        ]
        for process in processes:
            _, stderr = process.communicate(timeout=60)
            assert process.returncode == 0, stderr.decode(errors="replace")

        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == writes_per_worker * worker_count
        for line in lines:
            entry = json.loads(line)
            assert set(entry.keys()) == {
                "timestamp",
                "token",
                "entity_type",
                "detector",
                "offset",
                "adapter",
            }
