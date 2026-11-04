"""Configuración de la ventana temporal de consulta: turno anterior, día concreto o rango libre."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

_MADRID_TZ = ZoneInfo("Europe/Madrid")


@dataclass
class DateRangeConfig:
    mode: str = "shift_yesterday"   # shift_yesterday | shift_date | range
    shift_date: Optional[str] = None   # "YYYY-MM-DD"
    range_start: Optional[str] = None  # "YYYY-MM-DDTHH:MM"
    range_end: Optional[str] = None    # "YYYY-MM-DDTHH:MM"

    def resolve(self) -> Tuple[datetime, datetime]:
        """Devuelve (inicio, fin) en UTC."""
        if self.mode == "shift_yesterday":
            # TODAY()-15h a TODAY()+9h — equivalente al ventana original del conector
            midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            return midnight - timedelta(hours=15), midnight + timedelta(hours=9)

        if self.mode == "turno_diario":
            # Turno de 24h: ayer 21:00 Madrid → hoy 21:00 Madrid.
            # Se convierte a UTC para la consulta. En verano (CEST, UTC+2): 19:00 UTC.
            # En invierno (CET, UTC+1): 20:00 UTC. El cambio de hora se gestiona solo.
            today_madrid = datetime.now(_MADRID_TZ).date()
            end = datetime(today_madrid.year, today_madrid.month, today_madrid.day,
                           21, 0, 0, tzinfo=_MADRID_TZ).astimezone(timezone.utc)
            return end - timedelta(hours=24), end

        if self.mode == "shift_date":
            if not self.shift_date:
                raise ValueError("shift_date es obligatorio en modo shift_date")
            d = date.fromisoformat(self.shift_date)
            end = datetime(d.year, d.month, d.day, 21, 0, 0,
                           tzinfo=_MADRID_TZ).astimezone(timezone.utc)
            return end - timedelta(hours=24), end

        if self.mode == "range":
            if not self.range_start or not self.range_end:
                raise ValueError("range_start y range_end son obligatorios en modo range")
            fmt = "%Y-%m-%dT%H:%M"
            start = datetime.strptime(self.range_start, fmt).replace(tzinfo=timezone.utc)
            end = datetime.strptime(self.range_end, fmt).replace(tzinfo=timezone.utc)
            if end <= start:
                raise ValueError("range_end debe ser posterior a range_start")
            return start, end

        raise ValueError(f"Modo desconocido: {self.mode!r}")

    def describe(self) -> str:
        if self.mode == "shift_yesterday":
            return "Ventana diaria (hoy -15h → +9h)"
        if self.mode == "turno_diario":
            return "Turno diario automático (ayer 21:00 → hoy 21:00)"
        if self.mode == "shift_date":
            return f"Turno del {self.shift_date}"
        if self.mode == "range":
            return f"{self.range_start} → {self.range_end}"
        return self.mode


class DateRangeStore:
    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._cfg = self._load()

    def _load(self) -> DateRangeConfig:
        if not self._path.is_file():
            return DateRangeConfig()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return DateRangeConfig(
                mode=data.get("mode", "shift_yesterday"),
                shift_date=data.get("shift_date"),
                range_start=data.get("range_start"),
                range_end=data.get("range_end"),
            )
        except Exception:
            return DateRangeConfig()

    def get(self) -> DateRangeConfig:
        with self._lock:
            return self._cfg

    def set(self, cfg: DateRangeConfig) -> None:
        with self._lock:
            self._cfg = cfg
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {
                        "mode": cfg.mode,
                        "shift_date": cfg.shift_date,
                        "range_start": cfg.range_start,
                        "range_end": cfg.range_end,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
