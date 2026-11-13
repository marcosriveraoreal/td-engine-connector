"""Modelo de datos y persistencia de grupos de carga (state/groups.json)."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from date_config import DateRangeConfig
from schedule_config import ScheduleConfig, validate as validate_schedule

log = logging.getLogger(__name__)

VALID_AGG_TYPES = ("twa", "last")


class GroupError(ValueError):
    pass


@dataclass
class GroupConfig:
    id: str
    name: str
    agg_type: str       # "twa" | "last"
    target_table: str
    tags: List[str]
    schedule: ScheduleConfig
    date_range: DateRangeConfig
    enabled: bool = True
    tbname_pattern: Optional[str] = None  # si se define, los tags se descubren dinámicamente

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "agg_type": self.agg_type,
            "target_table": self.target_table,
            "tags": self.tags,
            "tbname_pattern": self.tbname_pattern,
            "schedule": self.schedule.to_dict(),
            "date_range": {
                "mode": self.date_range.mode,
                "shift_date": self.date_range.shift_date,
                "range_start": self.date_range.range_start,
                "range_end": self.date_range.range_end,
            },
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> GroupConfig:
        dr = d.get("date_range", {})
        return cls(
            id=d["id"],
            name=d["name"],
            agg_type=d.get("agg_type", "twa"),
            target_table=d.get("target_table", ""),
            tags=d.get("tags", []),
            tbname_pattern=d.get("tbname_pattern") or None,
            schedule=ScheduleConfig.from_dict(d.get("schedule", {})),
            date_range=DateRangeConfig(
                mode=dr.get("mode", "shift_yesterday"),
                shift_date=dr.get("shift_date"),
                range_start=dr.get("range_start"),
                range_end=dr.get("range_end"),
            ),
            enabled=d.get("enabled", True),
        )


def validate_group(g: GroupConfig) -> None:
    if not g.name.strip():
        raise GroupError("El nombre del grupo no puede estar vacío.")
    if g.agg_type not in VALID_AGG_TYPES:
        raise GroupError(f"Tipo de agregación no válido: '{g.agg_type}'. Use 'twa' o 'last'.")
    if not g.target_table.strip():
        raise GroupError("La tabla destino no puede estar vacía.")
    validate_schedule(g.schedule)


class GroupStore:
    """Lista de grupos persistida en disco, thread-safe."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._groups: List[GroupConfig] = self._load()

    def _load(self) -> List[GroupConfig]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return [GroupConfig.from_dict(d) for d in data]
        except Exception as exc:
            log.warning("No se pudo leer groups.json (%s); empezando sin grupos.", exc)
            return []

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps([g.to_dict() for g in self._groups], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("No se pudo guardar groups.json: %s", exc)

    def list(self) -> List[GroupConfig]:
        with self._lock:
            return list(self._groups)

    def get(self, group_id: str) -> Optional[GroupConfig]:
        with self._lock:
            return next((g for g in self._groups if g.id == group_id), None)

    def create(self, group: GroupConfig) -> GroupConfig:
        validate_group(group)
        with self._lock:
            self._groups.append(group)
            self._save()
        log.info("Grupo creado: %s (%s)", group.name, group.id)
        return group

    def update(self, group: GroupConfig) -> None:
        validate_group(group)
        with self._lock:
            for i, g in enumerate(self._groups):
                if g.id == group.id:
                    self._groups[i] = group
                    self._save()
                    return
        raise GroupError(f"Grupo no encontrado: {group.id}")

    def delete(self, group_id: str) -> bool:
        with self._lock:
            before = len(self._groups)
            self._groups = [g for g in self._groups if g.id != group_id]
            changed = len(self._groups) < before
            if changed:
                self._save()
        return changed

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]
