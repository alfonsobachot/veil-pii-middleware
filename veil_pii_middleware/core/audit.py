"""Append-only JSONL audit log.

Records timestamp, token, entity type, detector, offset and originating
adapter. It never contains the original value, nor a hash of it: an unkeyed
hash of a DNI or phone number is recoverable by brute force in about a
minute, and the token already correlates repeated values.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from veil_pii_middleware.core.detector import Detector, EntityType

__all__ = ["AuditEntry", "AuditLog"]

#: Guards this *process's own* writers against interleaving one line's bytes
#: with another's -- necessary but not sufficient, since `.veil/audit.jsonl`
#: is written by up to three separate OS processes (gateway, a hook
#: invocation, `/veil`) that do not share this lock. Cross-process
#: serialization is a separate mechanism -- see `_lock_sidecar`/
#: `_cross_process_lock` below. Measured empirically (real subprocess
#: workers, not threads) that plain append-mode writes alone are NOT
#: cross-process-atomic on this project's Windows target: interleaved writes
#: from separate processes lost and corrupted lines. A single-process
#: `threading.Lock` cannot fix that; only the OS-level lock below can.
_write_lock = threading.Lock()

#: How long to sleep between non-blocking lock attempts while contending for
#: the sidecar lock file. Short enough that many rapid writers still make
#: fast progress; long enough not to spin the CPU.
_LOCK_RETRY_SECONDS = 0.005

#: The sidecar lock file only needs to exist and have at least one byte --
#: msvcrt.locking() locks a byte range, not a "does this file have content"
#: check -- so this is written once if the file is empty, never read back.
_LOCK_BYTE_COUNT = 1


def _lock_sidecar(handle: IO[bytes]) -> None:
    """Block until this process holds the exclusive lock on ``handle``.

    Windows: a manual non-blocking retry loop around ``msvcrt.locking``,
    rather than that function's own ``LK_LOCK`` mode, whose internal retry
    is capped at ten one-second attempts and raises past that -- too fragile
    under the write volumes this log sees (many rapid audit entries from a
    single busy request). POSIX: ``fcntl.flock`` already blocks natively.
    """
    if sys.platform == "win32":
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTE_COUNT)
                return
            except OSError:
                time.sleep(_LOCK_RETRY_SECONDS)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_sidecar(handle: IO[bytes]) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTE_COUNT)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One line of the audit log, read back. Never carries the original value."""

    timestamp: str
    token: str
    entity_type: str
    detector: str
    offset: int
    adapter: str


class AuditLog:
    """Append-only JSONL log at a fixed path. Opens/creates lazily on first write."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        #: Sidecar file whose only purpose is to be OS-lockable -- see
        #: `_lock_sidecar`. Never contains audit data itself.
        self._lock_path = self._path.with_name(self._path.name + ".lock")

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        *,
        token: str,
        entity_type: EntityType,
        detector: Detector,
        offset: int,
        adapter: str,
    ) -> None:
        """Append one entry. Takes no value at all, so none can be written."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "token": token,
            "entity_type": entity_type.value,
            "detector": detector.value,
            "offset": offset,
            "adapter": adapter,
        }
        line = json.dumps(entry) + "\n"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            self._append_cross_process_safe(line)

    def _append_cross_process_safe(self, line: str) -> None:
        """Append ``line``, holding an OS-level lock for the whole operation.

        The in-process ``_write_lock`` (already held by the caller) only
        serializes threads of this one process; `.veil/audit.jsonl` is
        written by up to three separate processes (gateway, a hook
        invocation, `/veil`) that share none of this process's Python
        objects, so only an OS-level lock on a shared file actually
        serializes them. Plain append-mode writes alone were measured to
        lose and corrupt lines under real concurrent-process load on this
        project's Windows target -- see `_lock_sidecar`'s docstring.
        """
        with self._lock_path.open("a+b") as lock_handle:
            if os.fstat(lock_handle.fileno()).st_size == 0:
                lock_handle.write(b"\0")
                lock_handle.flush()
            lock_handle.seek(0)
            _lock_sidecar(lock_handle)
            try:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                _unlock_sidecar(lock_handle)

    @staticmethod
    def read_entries(path: str | Path) -> Iterator[AuditEntry]:
        """Yield every entry currently in the file at ``path``, in append order.

        Returns nothing (not an error) if the file does not exist yet -- a
        log with no entries written is a normal, expected state.
        """
        file_path = Path(path)
        if not file_path.exists():
            return
        with file_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                yield AuditEntry(
                    timestamp=raw["timestamp"],
                    token=raw["token"],
                    entity_type=raw["entity_type"],
                    detector=raw["detector"],
                    offset=raw["offset"],
                    adapter=raw["adapter"],
                )
