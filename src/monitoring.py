"""Estado en memoria para el dashboard web: historial de ejecuciones y buffer de logs."""
from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Set


@dataclass(frozen=True)
class RunRecord:
    group_id: str
    group_name: str
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    status: str          # "ok" | "error"
    trigger: str         # "startup" | "scheduled" | "manual"
    rows_written: int
    tags_expected: int
    tags_received: int
    tags_missing: List[str] = field(default_factory=list)
    error_category: Optional[str] = None
    error_hint: Optional[str] = None
    error_detail: Optional[str] = None

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


class RunTracker:
    """Historial en memoria (thread-safe) de los últimos ciclos, indexado por grupo."""

    def __init__(self, max_runs: int = 200):
        self._lock = threading.Lock()
        self._runs: Deque[RunRecord] = deque(maxlen=max_runs)
        self._next_runs: Dict[str, datetime] = {}   # group_id -> next datetime
        self._running: Set[str] = set()              # group_ids en ejecución
        self._process_started_at = datetime.now(timezone.utc)

    def record(self, run: RunRecord) -> None:
        with self._lock:
            self._runs.append(run)

    def set_next_run(self, group_id: str, when: Optional[datetime]) -> None:
        with self._lock:
            if when is None:
                self._next_runs.pop(group_id, None)
            else:
                self._next_runs[group_id] = when

    def set_running(self, group_id: str, running: bool) -> None:
        with self._lock:
            if running:
                self._running.add(group_id)
            else:
                self._running.discard(group_id)

    def is_running(self, group_id: Optional[str] = None) -> bool:
        with self._lock:
            return (group_id in self._running) if group_id else bool(self._running)

    def next_run_at(self, group_id: Optional[str] = None) -> Optional[datetime]:
        with self._lock:
            if group_id:
                return self._next_runs.get(group_id)
            return min(self._next_runs.values()) if self._next_runs else None

    def next_runs_all(self) -> Dict[str, datetime]:
        with self._lock:
            return dict(self._next_runs)

    def last_run_for(self, group_id: str) -> Optional[RunRecord]:
        with self._lock:
            for run in reversed(list(self._runs)):
                if run.group_id == group_id:
                    return run
        return None

    def last_run(self) -> Optional[RunRecord]:
        with self._lock:
            return self._runs[-1] if self._runs else None

    def snapshot(self, limit: Optional[int] = None, group_id: Optional[str] = None) -> List[RunRecord]:
        with self._lock:
            runs = list(self._runs)
        runs.reverse()
        if group_id:
            runs = [r for r in runs if r.group_id == group_id]
        return runs[:limit] if limit else runs

    @property
    def process_started_at(self) -> datetime:
        return self._process_started_at


class LogBufferHandler(logging.Handler):
    """Handler de logging que retiene las últimas N líneas en memoria."""

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self._capacity = capacity
        self._lock = threading.Lock()
        self._records: Deque[dict] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + "".join(traceback.format_exception(*record.exc_info))
            entry = {
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
        except Exception:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": "ERROR",
                "logger": "logging",
                "message": "No se pudo formatear un registro de log.",
            }
        with self._lock:
            self._records.append(entry)

    def snapshot(self, limit: Optional[int] = None, level: Optional[str] = None) -> List[dict]:
        with self._lock:
            records = list(self._records)
        records.reverse()
        if level:
            records = [r for r in records if r["level"] == level.upper()]
        return records[:limit] if limit else records


class LoginThrottle:
    """Bloqueo temporal por IP tras varios intentos de login fallidos."""

    def __init__(self, max_attempts: int = 5, lockout_seconds: int = 60):
        self._max_attempts = max_attempts
        self._lockout_seconds = lockout_seconds
        self._lock = threading.Lock()
        self._failures: dict = {}

    def is_locked(self, key: str) -> Optional[int]:
        with self._lock:
            entry = self._failures.get(key)
            if not entry:
                return None
            count, locked_until = entry
            if count < self._max_attempts:
                return None
            remaining = locked_until - time.monotonic()
            if remaining <= 0:
                del self._failures[key]
                return None
            return int(remaining) + 1

    def record_failure(self, key: str) -> None:
        with self._lock:
            count, _ = self._failures.get(key, (0, 0.0))
            count += 1
            self._failures[key] = (count, time.monotonic() + self._lockout_seconds)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
