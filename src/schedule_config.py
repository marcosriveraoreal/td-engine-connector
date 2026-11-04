"""Planificación de ejecuciones, expresada y evaluada en hora de Madrid (Europe/Madrid).

Se fija un valor inicial por variable de entorno (SCHEDULE_CRON) pero, a partir de ahí,
puede cambiarse en caliente desde el panel web (sección "Configuración") sin reiniciar el
contenedor: cada cambio se persiste en disco (SCHEDULE_STATE_FILE) y tiene prioridad sobre
la variable de entorno en el siguiente arranque.

Se admiten tres formas de definir cuándo ejecutar, pensadas para no tener que escribir cron
a mano salvo que de verdad se necesite:
  - "interval": cada N minutos.
  - "times": a una lista de horas concretas del día (HH:MM, hora de Madrid).
  - "cron": expresión cron estándar de 5 campos, para casos avanzados (p.ej. solo días
    laborables). Los campos de hora se interpretan también en hora de Madrid.

El modo "times" no se traduce a una única expresión cron interna porque una lista de horas
arbitrarias (p.ej. 06:00 y 14:30) no se puede representar como un cruce simple de campos
minuto/hora de cron sin generar combinaciones no deseadas; en su lugar se calcula
directamente la próxima/anterior hora candidata.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from croniter import croniter

log = logging.getLogger(__name__)

MADRID_TZ = ZoneInfo("Europe/Madrid")

_VALID_MODES = ("interval", "times", "cron")


class ScheduleError(ValueError):
    """Configuración de planificación inválida (entrada de usuario)."""


def _parse_hhmm(value: str) -> dtime:
    try:
        hh, mm = value.strip().split(":")
        return dtime(hour=int(hh), minute=int(mm))
    except (ValueError, TypeError) as exc:
        raise ScheduleError(f"'{value}' no es una hora válida (formato HH:MM, 24 horas).") from exc


@dataclass(frozen=True)
class ScheduleConfig:
    mode: str = "cron"
    interval_minutes: int = 5
    times: List[str] = field(default_factory=lambda: ["06:00"])
    cron: str = "*/5 * * * *"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "ScheduleConfig":
        return ScheduleConfig(
            mode=str(data.get("mode", "cron")),
            interval_minutes=int(data.get("interval_minutes", 5)),
            times=[str(t) for t in data.get("times", ["06:00"])],
            cron=str(data.get("cron", "*/5 * * * *")),
        )

    def describe(self) -> str:
        if self.mode == "interval":
            return f"Cada {self.interval_minutes} min (hora de Madrid)"
        if self.mode == "times":
            return "A las " + ", ".join(self.times) + " (hora de Madrid)"
        return f"Cron avanzado «{self.cron}» (hora de Madrid)"

    def _effective_cron(self) -> str:
        return f"*/{self.interval_minutes} * * * *" if self.mode == "interval" else self.cron


def validate(config: ScheduleConfig) -> None:
    if config.mode not in _VALID_MODES:
        raise ScheduleError(f"Modo de planificación desconocido: '{config.mode}'.")

    if config.mode == "interval":
        if not (1 <= config.interval_minutes <= 1440):
            raise ScheduleError("El intervalo debe estar entre 1 y 1440 minutos.")
    elif config.mode == "times":
        if not config.times:
            raise ScheduleError("Debe indicarse al menos una hora de ejecución.")
        for t in config.times:
            _parse_hhmm(t)
    else:
        try:
            croniter(config.cron)
        except Exception as exc:  # croniter agrupa varios tipos de error de parseo
            raise ScheduleError(f"Expresión cron no válida: {exc}") from exc


def compute_next_run(config: ScheduleConfig, after: datetime) -> datetime:
    """Próxima ejecución estrictamente posterior a `after` (tz-aware, hora de Madrid)."""
    after_madrid = after.astimezone(MADRID_TZ)

    if config.mode == "times":
        candidates = []
        for t in config.times:
            parsed = _parse_hhmm(t)
            candidate = after_madrid.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            if candidate <= after_madrid:
                candidate += timedelta(days=1)
            candidates.append(candidate)
        return min(candidates)

    return croniter(config._effective_cron(), after_madrid).get_next(datetime)


def compute_prev_run(config: ScheduleConfig, before: datetime) -> datetime:
    """Última ejecución programada anterior o igual a `before` (para el healthcheck)."""
    before_madrid = before.astimezone(MADRID_TZ)

    if config.mode == "times":
        candidates = []
        for t in config.times:
            parsed = _parse_hhmm(t)
            candidate = before_madrid.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            if candidate > before_madrid:
                candidate -= timedelta(days=1)
            candidates.append(candidate)
        return max(candidates)

    return croniter(config._effective_cron(), before_madrid).get_prev(datetime)


class ScheduleStore:
    """Persiste la configuración de planificación en disco y la mantiene en memoria (thread-safe)."""

    def __init__(self, path: str, default: ScheduleConfig, create_if_missing: bool = True):
        self._path = Path(path)
        self._lock = threading.Lock()
        loaded = self._load()
        if loaded is not None:
            self._config = loaded
        else:
            self._config = default
            if create_if_missing:
                self._save(default)

    def _load(self) -> Optional[ScheduleConfig]:
        if not self._path.is_file():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            config = ScheduleConfig.from_dict(data)
            validate(config)
            return config
        except (OSError, ValueError, ScheduleError) as exc:
            log.warning(
                "No se pudo leer la planificación guardada en %s (%s); se usará el valor por defecto.",
                self._path, exc,
            )
            return None

    def _save(self, config: ScheduleConfig) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(config.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning("No se pudo guardar la planificación en %s: %s", self._path, exc)

    def get(self) -> ScheduleConfig:
        with self._lock:
            return self._config

    def set(self, config: ScheduleConfig) -> None:
        validate(config)
        with self._lock:
            self._config = config
            self._save(config)
        log.info("Planificación actualizada: %s", config.describe())
