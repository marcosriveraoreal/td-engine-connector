"""Docker HEALTHCHECK: verifica que el heartbeat no supere el umbral de edad adaptativo.

Lee groups.json para calcular el período mínimo entre ejecuciones y ajusta el umbral
automáticamente. Así, un cron de cada 2 h no dispara falsos positivos con un límite fijo
de 15 min, y un cron de cada 5 min sí detecta cuelgues rápidamente.

Si groups.json no está disponible (primer arranque, volumen nuevo…) usa el valor fijo
HEARTBEAT_MAX_AGE_SECONDS como respaldo.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/app/state/heartbeat")
GROUPS_STATE_FILE = os.environ.get("GROUPS_STATE_FILE", "/app/state/groups.json")
GRACE_SECONDS = int(os.environ.get("HEARTBEAT_GRACE_SECONDS", "300"))
FALLBACK_MAX_AGE = int(os.environ.get("HEARTBEAT_MAX_AGE_SECONDS", "900"))


def _min_group_period_seconds() -> int | None:
    """Período mínimo en segundos del schedule entre los grupos activos."""
    try:
        path = Path(GROUPS_STATE_FILE)
        if not path.is_file():
            return None
        groups = json.loads(path.read_text(encoding="utf-8"))
        if "/app/src" not in sys.path:
            sys.path.insert(0, "/app/src")
        from schedule_config import ScheduleConfig, compute_next_run, compute_prev_run

        now = datetime.now(timezone.utc)
        min_period: int | None = None
        for g in groups:
            if not g.get("enabled", True):
                continue
            sched_data = g.get("schedule", {})
            if not sched_data:
                continue
            config = ScheduleConfig.from_dict(sched_data)
            prev = compute_prev_run(config, now)
            nxt = compute_next_run(config, prev)
            period = int(
                (nxt.astimezone(timezone.utc) - prev.astimezone(timezone.utc)).total_seconds()
            )
            if period > 0 and (min_period is None or period < min_period):
                min_period = period
        return min_period
    except Exception:
        return None


def main() -> int:
    if not os.path.exists(HEARTBEAT_FILE):
        print(f"Heartbeat no encontrado todavía: {HEARTBEAT_FILE}")
        return 1

    age = time.time() - os.path.getmtime(HEARTBEAT_FILE)

    min_period = _min_group_period_seconds()
    if min_period is not None:
        max_age = min_period + GRACE_SECONDS
        source = f"adaptativo: {min_period // 60}min período + {GRACE_SECONDS}s margen"
    else:
        max_age = FALLBACK_MAX_AGE
        source = f"fijo: HEARTBEAT_MAX_AGE_SECONDS={FALLBACK_MAX_AGE}s"

    if age > max_age:
        print(f"Heartbeat obsoleto: {age:.0f}s > {max_age}s [{source}]")
        return 1

    print(f"OK: heartbeat de hace {age:.0f}s, umbral {max_age}s [{source}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
