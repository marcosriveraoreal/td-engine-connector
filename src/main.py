"""Punto de entrada: bucle multi-grupo TDengine → SQL Server con schedule por grupo."""
from __future__ import annotations

import logging
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

from config import AppConfig, ConfigError, NotificationConfig, load_config
from errors import classify_error
from groups_config import GroupConfig, GroupStore
from logging_conf import setup_logging
from monitoring import LogBufferHandler, RunRecord, RunTracker
from rabbit_client import send_email_notification
from schedule_config import MADRID_TZ, compute_next_run
from sqlserver_client import SqlServerClient
from tdengine_client import TDengineClient

log = logging.getLogger(__name__)

_stop_requested = False


def _handle_signal(signum, _frame) -> None:
    global _stop_requested
    log.info("Señal %s recibida, se detendrá tras los ciclos en curso.", signum)
    _stop_requested = True


def _write_heartbeat(path: str) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except OSError as exc:
        log.warning("No se pudo escribir el heartbeat: %s", exc)


def _fmt_madrid(dt: datetime) -> str:
    return dt.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S (hora de Madrid)")


_TRIGGER_LABELS = {
    "startup": "Arranque del sistema",
    "scheduled": "Ejecución programada",
    "manual": "Ejecución manual",
}

_AGG_LABELS = {
    "twa": "Media ponderada (TWA)",
    "last": "Horómetro (LAST)",
}


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _notify_failure(notif: NotificationConfig, group: GroupConfig, run: RunRecord) -> None:
    if not notif.enabled or not notif.destinatarios:
        return

    fecha = run.finished_at.astimezone(MADRID_TZ).strftime("%d/%m/%Y %H:%M:%S")
    tags_faltantes = ", ".join(run.tags_missing) if run.tags_missing else "Ninguno"
    detalle_tecnico = (run.error_detail or "")[:500]

    variables = [
        f"grupo:{group.name}",
        f"tipo_agregacion:{_AGG_LABELS.get(group.agg_type, group.agg_type)}",
        f"tabla_destino:{group.target_table}",
        f"trigger:{_TRIGGER_LABELS.get(run.trigger, run.trigger)}",
        f"fecha:{fecha}",
        f"duracion:{_fmt_duration(run.duration_seconds)}",
        f"error:{run.error_category or 'Error desconocido'}",
        f"sugerencia:{run.error_hint or ''}",
        f"detalle_tecnico:{detalle_tecnico}",
        f"tags_esperados:{run.tags_expected}",
        f"tags_recibidos:{run.tags_received}",
        f"tags_faltantes:{tags_faltantes}",
    ]

    send_email_notification(
        host=notif.rabbitmq_host,
        port=notif.rabbitmq_port,
        user=notif.rabbitmq_user,
        password=notif.rabbitmq_password,
        vhost=notif.rabbitmq_vhost,
        destinatarios=notif.destinatarios,
        modelo_correo=notif.modelo_correo,
        cuenta_correo=notif.cuenta_correo,
        auth=notif.auth_token,
        cc=notif.cc,
        variables=variables,
    )


def sync_group(
    td_client: TDengineClient,
    sql_client: SqlServerClient,
    group: GroupConfig,
    trigger: str,
    tracker: RunTracker,
    heartbeat_file: str,
    notif: NotificationConfig | None = None,
    dry_run: bool = False,
) -> None:
    if dry_run:
        log.warning("[%s] DRY_RUN=true — ejecución bloqueada, no se escribe en BD.", group.name)
        return
    tracker.set_running(group.id, True)
    started_at = datetime.now(timezone.utc)
    try:
        start, end = group.date_range.resolve()
        log.info("[%s] Ventana: %s → %s", group.name, start.isoformat(), end.isoformat())

        if group.agg_type == "twa":
            rows = td_client.fetch_hourly_averages(start, end, group.tags or None)
            sql_client.upsert_hourly_averages(rows, group.target_table)
            row_count = len(rows)
            received = {r.tagname for r in rows}
            missing = sorted(set(group.tags) - received) if group.tags else []
        else:
            rows = td_client.fetch_hourly_last(start, end, group.tags or None, group.tbname_pattern)
            sql_client.upsert_horometros(rows, group.target_table)
            row_count = len(rows)
            received = {r.tagname for r in rows}
            missing = sorted(set(group.tags) - received) if group.tags else []
        if missing:
            log.warning("[%s] %d tag(s) sin datos: %s", group.name, len(missing), ", ".join(missing))

        finished_at = datetime.now(timezone.utc)
        log.info("[%s] Ciclo completado: %d filas.", group.name, row_count)
        run = RunRecord(
            group_id=group.id,
            group_name=group.name,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=(finished_at - started_at).total_seconds(),
            status="ok",
            trigger=trigger,
            rows_written=row_count,
            tags_expected=len(group.tags),
            tags_received=len(received),
            tags_missing=missing,
        )
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        info = classify_error(exc)
        log.exception("[%s] Fallo: %s", group.name, info.category)
        run = RunRecord(
            group_id=group.id,
            group_name=group.name,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=(finished_at - started_at).total_seconds(),
            status="error",
            trigger=trigger,
            rows_written=0,
            tags_expected=len(group.tags),
            tags_received=0,
            tags_missing=[],
            error_category=info.category,
            error_hint=info.hint,
            error_detail=info.detail,
        )
    finally:
        tracker.set_running(group.id, False)

    tracker.record(run)
    if run.is_ok:
        _write_heartbeat(heartbeat_file)
    elif notif:
        _notify_failure(notif, group, run)


def _run_loop(
    td_client: TDengineClient,
    sql_client: SqlServerClient,
    config: AppConfig,
    tracker: RunTracker,
    group_store: GroupStore,
    manual_queue: queue.Queue,
) -> None:
    notif = config.notification
    dry_run = config.dry_run
    if dry_run:
        log.warning("DRY_RUN=true — ninguna ejecución escribirá en la base de datos.")

    if config.sync_on_startup and not dry_run:
        for group in group_store.list():
            if group.enabled:
                sync_group(td_client, sql_client, group, "startup", tracker, config.heartbeat_file, notif)
    elif not dry_run:
        log.info("SYNC_ON_STARTUP=false — se omite la sincronización inicial.")

    # Inicializar next_run por grupo
    next_runs: Dict[str, datetime] = {}
    now = datetime.now(timezone.utc)
    for group in group_store.list():
        if group.enabled:
            next_runs[group.id] = compute_next_run(group.schedule, now)
            tracker.set_next_run(group.id, next_runs[group.id])
            log.info("[%s] Próxima: %s", group.name, _fmt_madrid(next_runs[group.id]))

    while not _stop_requested:
        # Ejecuciones manuales solicitadas desde el panel web
        while True:
            try:
                group_id = manual_queue.get_nowait()
            except queue.Empty:
                break
            group = group_store.get(group_id)
            if group and group.enabled:
                sync_group(td_client, sql_client, group, "manual", tracker, config.heartbeat_file, notif, dry_run=dry_run)
                next_runs[group_id] = compute_next_run(group.schedule, datetime.now(timezone.utc))
                tracker.set_next_run(group_id, next_runs[group_id])

        # Sincronizar lista de grupos (altas/bajas desde la web)
        current_groups = {g.id: g for g in group_store.list() if g.enabled}
        now = datetime.now(timezone.utc)

        for gid in list(next_runs.keys()):
            if gid not in current_groups:
                del next_runs[gid]
                tracker.set_next_run(gid, None)

        for gid, group in current_groups.items():
            if gid not in next_runs:
                next_runs[gid] = compute_next_run(group.schedule, now)
                tracker.set_next_run(gid, next_runs[gid])
                log.info("[%s] Grupo activado, próxima: %s", group.name, _fmt_madrid(next_runs[gid]))

        # Ejecutar grupos con el turno cumplido
        now = datetime.now(timezone.utc)
        for gid, due_at in list(next_runs.items()):
            if due_at <= now:
                group = current_groups.get(gid)
                if group:
                    sync_group(td_client, sql_client, group, "scheduled", tracker, config.heartbeat_file, notif, dry_run=dry_run)
                    next_runs[gid] = compute_next_run(group.schedule, datetime.now(timezone.utc))
                    tracker.set_next_run(gid, next_runs[gid])

        time.sleep(1.0)


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2

    tracker = RunTracker(max_runs=config.web_ui.max_runs_history)
    log_buffer = LogBufferHandler(capacity=config.web_ui.max_log_lines)
    setup_logging(config.log_level, extra_handler=log_buffer)
    log.info("Iniciando connector-td-engine (run_once=%s, dry_run=%s)", config.run_once, config.dry_run)

    if config.web_ui.password_generated:
        log.warning(
            "Contraseña temporal del panel: usuario=%s contraseña=%s",
            config.web_ui.user, config.web_ui.password,
        )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    td_client = TDengineClient(config.tdengine)
    sql_client = SqlServerClient(config.sqlserver)
    group_store = GroupStore(config.groups_state_file)
    manual_queue: queue.Queue = queue.Queue()

    web_server = None
    if config.web_ui.enabled and not config.run_once:
        from webapp import start_web_server
        try:
            web_server = start_web_server(config, tracker, log_buffer, sql_client, group_store, manual_queue)
        except Exception:
            log.exception("No se pudo arrancar el panel web; el conector seguirá sincronizando sin él.")

    try:
        if config.run_once:
            for group in group_store.list():
                if group.enabled:
                    sync_group(td_client, sql_client, group, "startup", tracker, config.heartbeat_file, config.notification, dry_run=config.dry_run)
            failed = any(not r.is_ok for r in tracker.snapshot())
            return 1 if failed else 0

        _run_loop(td_client, sql_client, config, tracker, group_store, manual_queue)
        log.info("Detenido correctamente.")
        return 0
    finally:
        td_client.close()
        if web_server is not None:
            web_server.stop()


if __name__ == "__main__":
    sys.exit(main())
