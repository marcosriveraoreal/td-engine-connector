"""Panel web de monitorización con gestión de grupos de carga."""
from __future__ import annotations

import hmac
import logging
import queue
import threading
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, redirect, render_template_string, request, send_from_directory, session, url_for
from waitress import create_server

from config import AppConfig
from date_config import DateRangeConfig
from groups_config import GroupConfig, GroupError, GroupStore
from monitoring import LogBufferHandler, LoginThrottle, RunRecord, RunTracker
from schedule_config import MADRID_TZ, ScheduleConfig, ScheduleError, compute_next_run
from sqlserver_client import SqlServerClient

log = logging.getLogger(__name__)

_TRIGGER_LABELS = {"startup": "Arranque", "scheduled": "Programado", "manual": "Manual"}


def _resolve_logo(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://", "data:", "/assets/")):
        return raw
    return f"/assets/{raw.lstrip('./').rsplit('/', 1)[-1]}"


def _ser_run(run: RunRecord) -> dict:
    return {
        "group_id": run.group_id,
        "group_name": run.group_name,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat(),
        "duration_seconds": round(run.duration_seconds, 2),
        "status": run.status,
        "trigger": run.trigger,
        "trigger_label": _TRIGGER_LABELS.get(run.trigger, run.trigger),
        "rows_written": run.rows_written,
        "tags_expected": run.tags_expected,
        "tags_received": run.tags_received,
        "tags_missing": run.tags_missing,
        "error_category": run.error_category,
        "error_hint": run.error_hint,
        "error_detail": run.error_detail,
    }


def _ser_group(group: GroupConfig, tracker: RunTracker) -> dict:
    last = tracker.last_run_for(group.id)
    next_run = tracker.next_run_at(group.id)
    return {
        "id": group.id,
        "name": group.name,
        "agg_type": group.agg_type,
        "target_table": group.target_table,
        "tags": group.tags,
        "tags_count": len(group.tags),
        "tbname_pattern": group.tbname_pattern,
        "enabled": group.enabled,
        "is_running": tracker.is_running(group.id),
        "schedule": group.schedule.to_dict(),
        "schedule_description": group.schedule.describe(),
        "date_range": {
            "mode": group.date_range.mode,
            "shift_date": group.date_range.shift_date,
            "range_start": group.date_range.range_start,
            "range_end": group.date_range.range_end,
            "description": group.date_range.describe(),
        },
        "next_run_at": next_run.isoformat() if next_run else None,
        "next_run_madrid": next_run.astimezone(MADRID_TZ).strftime("%H:%M:%S") if next_run else None,
        "last_run": _ser_run(last) if last else None,
    }


def create_app(
    config: AppConfig,
    tracker: RunTracker,
    log_buffer: LogBufferHandler,
    sql_client: SqlServerClient,
    group_store: GroupStore,
    manual_queue: queue.Queue,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = config.web_ui.secret_key
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

    web_cfg = config.web_ui
    brand = config.branding
    throttle = LoginThrottle()

    def _check_creds(u: str, p: str) -> bool:
        return hmac.compare_digest(u or "", web_cfg.user) and hmac.compare_digest(p or "", web_cfg.password)

    def require_login(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login", next=request.path))
            return fn(*args, **kwargs)
        return wrapper

    def require_auth(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("authenticated"):
                return jsonify(error="No autenticado."), 401
            return fn(*args, **kwargs)
        return wrapper

    @app.after_request
    def _no_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def _brand():
        return {
            "company_name": brand.company_name,
            "product_name": brand.product_name,
            "accent_color": brand.accent_color,
            "logo_url": _resolve_logo(brand.logo_url),
            "monogram": "".join(w[0] for w in brand.company_name.split()[:2]).upper() or "AM",
        }

    @app.get("/assets/<path:filename>")
    def assets(filename):
        return send_from_directory(brand.assets_dir, filename)

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    # ---- Auth ----

    @app.get("/login")
    def login():
        if session.get("authenticated"):
            return redirect("/")
        return render_template_string(LOGIN_HTML, error=None, username="", next=request.args.get("next", "/"))

    @app.post("/login")
    def login_post():
        ip = request.remote_addr or "unknown"
        locked = throttle.is_locked(ip)
        if locked:
            return render_template_string(LOGIN_HTML, error=f"Demasiados intentos. Espera {locked}s.", username=request.form.get("username", ""), next=request.form.get("next", "/")), 429
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        nxt = request.form.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if _check_creds(u, p):
            throttle.record_success(ip)
            session.clear()
            session["authenticated"] = True
            session["username"] = u
            session.permanent = True
            log.info("Inicio de sesión correcto (usuario: %s).", u)
            return redirect(nxt)
        throttle.record_failure(ip)
        log.warning("Intento de login fallido (usuario: %s).", u)
        return render_template_string(LOGIN_HTML, error="Usuario o contraseña incorrectos.", username=u, next=nxt), 401

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---- Pages ----

    @app.get("/")
    @require_login
    def index():
        return render_template_string(DASHBOARD_HTML, refresh_ms=web_cfg.refresh_seconds * 1000, username=session.get("username", ""), active="dashboard")

    @app.get("/groups")
    @require_login
    def groups_page():
        return render_template_string(GROUPS_HTML, refresh_ms=web_cfg.refresh_seconds * 1000, username=session.get("username", ""), active="groups")

    @app.get("/logs")
    @require_login
    def logs_page():
        return render_template_string(LOGS_HTML, refresh_ms=web_cfg.refresh_seconds * 1000, username=session.get("username", ""), active="logs")

    # ---- API: global status ----

    @app.get("/api/status")
    @require_auth
    def api_status():
        now = datetime.now(timezone.utc)
        groups = group_store.list()
        return jsonify({
            "server_time": now.isoformat(),
            "process_started_at": tracker.process_started_at.isoformat(),
            "total_groups": len(groups),
            "enabled_groups": sum(1 for g in groups if g.enabled),
            "any_running": tracker.is_running(),
        })

    @app.get("/api/logs")
    @require_auth
    def api_logs():
        limit = request.args.get("limit", default=300, type=int)
        level = request.args.get("level")
        return jsonify(log_buffer.snapshot(limit=limit, level=level))

    @app.get("/api/runs")
    @require_auth
    def api_runs():
        limit = request.args.get("limit", default=100, type=int)
        group_id = request.args.get("group_id")
        return jsonify([_ser_run(r) for r in tracker.snapshot(limit=limit, group_id=group_id or None)])

    # ---- API: groups CRUD ----

    @app.get("/api/groups")
    @require_auth
    def api_groups_list():
        return jsonify([_ser_group(g, tracker) for g in group_store.list()])

    @app.post("/api/groups")
    @require_auth
    def api_groups_create():
        data = request.get_json(silent=True) or {}
        try:
            group = GroupConfig(
                id=GroupStore.new_id(),
                name=str(data.get("name", "")).strip(),
                agg_type=str(data.get("agg_type", "twa")),
                target_table=str(data.get("target_table", "")).strip(),
                tags=[t.strip() for t in str(data.get("tags_text", "")).splitlines() if t.strip()],
                tbname_pattern=str(data.get("tbname_pattern") or "").strip() or None,
                schedule=ScheduleConfig.from_dict(data.get("schedule", {})),
                date_range=DateRangeConfig(
                    mode=data.get("date_range", {}).get("mode", "shift_yesterday"),
                    shift_date=data.get("date_range", {}).get("shift_date"),
                    range_start=data.get("date_range", {}).get("range_start"),
                    range_end=data.get("date_range", {}).get("range_end"),
                ),
                enabled=bool(data.get("enabled", True)),
            )
            group_store.create(group)
        except (GroupError, ScheduleError, ValueError) as exc:
            return jsonify(ok=False, message=str(exc)), 400
        log.info("Grupo creado desde el panel por %s: %s", session.get("username"), group.name)
        return jsonify(ok=True, group=_ser_group(group, tracker)), 201

    @app.get("/api/groups/<group_id>")
    @require_auth
    def api_group_get(group_id):
        g = group_store.get(group_id)
        if not g:
            return jsonify(error="Grupo no encontrado."), 404
        return jsonify(_ser_group(g, tracker))

    @app.put("/api/groups/<group_id>")
    @require_auth
    def api_group_update(group_id):
        g = group_store.get(group_id)
        if not g:
            return jsonify(error="Grupo no encontrado."), 404
        data = request.get_json(silent=True) or {}
        try:
            tags_text = data.get("tags_text")
            tags = [t.strip() for t in str(tags_text).splitlines() if t.strip()] if tags_text is not None else g.tags
            tbname_raw = data.get("tbname_pattern")
            tbname_pattern = str(tbname_raw or "").strip() or None if tbname_raw is not None else g.tbname_pattern
            updated = GroupConfig(
                id=g.id,
                name=str(data.get("name", g.name)).strip(),
                agg_type=str(data.get("agg_type", g.agg_type)),
                target_table=str(data.get("target_table", g.target_table)).strip(),
                tags=tags,
                tbname_pattern=tbname_pattern,
                schedule=ScheduleConfig.from_dict(data.get("schedule", g.schedule.to_dict())),
                date_range=DateRangeConfig(
                    mode=data.get("date_range", {}).get("mode", g.date_range.mode),
                    shift_date=data.get("date_range", {}).get("shift_date", g.date_range.shift_date),
                    range_start=data.get("date_range", {}).get("range_start", g.date_range.range_start),
                    range_end=data.get("date_range", {}).get("range_end", g.date_range.range_end),
                ),
                enabled=bool(data.get("enabled", g.enabled)),
            )
            group_store.update(updated)
        except (GroupError, ScheduleError, ValueError) as exc:
            return jsonify(ok=False, message=str(exc)), 400
        log.info("Grupo actualizado desde el panel por %s: %s", session.get("username"), updated.name)
        return jsonify(ok=True, group=_ser_group(updated, tracker))

    @app.delete("/api/groups/<group_id>")
    @require_auth
    def api_group_delete(group_id):
        g = group_store.get(group_id)
        if not g:
            return jsonify(error="Grupo no encontrado."), 404
        group_store.delete(group_id)
        log.info("Grupo eliminado desde el panel por %s: %s", session.get("username"), g.name)
        return jsonify(ok=True)

    @app.post("/api/groups/<group_id>/run-now")
    @require_auth
    def api_group_run_now(group_id):
        g = group_store.get(group_id)
        if not g:
            return jsonify(error="Grupo no encontrado."), 404
        if tracker.is_running(group_id):
            return jsonify(ok=False, message="Este grupo ya está sincronizando."), 409
        manual_queue.put(group_id)
        log.info("Ejecución manual del grupo '%s' solicitada por %s.", g.name, session.get("username"))
        return jsonify(ok=True, message=f"Sincronización de '{g.name}' solicitada.")

    return app


class WebServerHandle:
    def __init__(self, server, thread: threading.Thread):
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        try:
            self._server.close()
        except Exception:
            log.warning("Error al detener el panel web.", exc_info=True)
        self._thread.join(timeout=5)


def start_web_server(
    config: AppConfig,
    tracker: RunTracker,
    log_buffer: LogBufferHandler,
    sql_client: SqlServerClient,
    group_store: GroupStore,
    manual_queue: queue.Queue,
) -> WebServerHandle:
    app = create_app(config, tracker, log_buffer, sql_client, group_store, manual_queue)
    web_cfg = config.web_ui
    server = create_server(app, host=web_cfg.host, port=web_cfg.port)
    thread = threading.Thread(target=server.run, name="dashboard-web", daemon=True)
    thread.start()
    log.info("Panel web de monitorización escuchando en http://%s:%d", web_cfg.host, web_cfg.port)
    return WebServerHandle(server, thread)


# ---------------------------------------------------------------------------
# Design system – tema claro / oscuro con toggle
# ---------------------------------------------------------------------------

_THEME_INIT_JS = """
(function(){
  var s=localStorage.getItem('theme')||
    (window.matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
  document.documentElement.setAttribute('data-theme',s);
})();
"""

_THEME_TOGGLE_JS = """
function toggleTheme(){
  var t=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',t);
  localStorage.setItem('theme',t);
  document.getElementById('theme-icon-dark').style.display=t==='dark'?'none':'block';
  document.getElementById('theme-icon-light').style.display=t==='light'?'none':'block';
}
window.addEventListener('DOMContentLoaded',function(){
  var t=document.documentElement.getAttribute('data-theme');
  document.getElementById('theme-icon-dark').style.display=t==='dark'?'none':'block';
  document.getElementById('theme-icon-light').style.display=t==='light'?'none':'block';
});
"""

_THEME_BTN = """
<button onclick="toggleTheme()" class="btn-icon" title="Cambiar tema" aria-label="Cambiar tema">
  <svg id="theme-icon-light" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>
  <svg id="theme-icon-dark" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/></svg>
</button>
"""


SHARED_CSS = """
:root,[data-theme="dark"]{
  --bg:#0d1117;--surf:#161c27;--surf2:#1c2333;--surf3:#242e42;
  --bdr:#2a3349;--bdr2:#1e2a3e;
  --txt:#e2eaf5;--dim:#7b91ae;--sub:#46586e;
  --brand:{{ accent_color }};
  --ok:#22c55e;--warn:#f59e0b;--err:#ef4444;--info:#6366f1;
  --ok-bg:rgba(34,197,94,.1);--err-bg:rgba(239,68,68,.1);--warn-bg:rgba(245,158,11,.1);
  --sh:0 1px 3px rgba(0,0,0,.4),0 1px 2px rgba(0,0,0,.3);
  --sh-md:0 4px 16px rgba(0,0,0,.45);
  --sh-lg:0 8px 32px rgba(0,0,0,.55);
  --mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
  --r:10px;--r-sm:6px;--r-lg:14px;
}
[data-theme="light"]{
  --bg:#f0f4f8;--surf:#ffffff;--surf2:#f8fafc;--surf3:#f1f5f9;
  --bdr:#dde3ed;--bdr2:#e8eef5;
  --txt:#0f172a;--dim:#475569;--sub:#94a3b8;
  --ok:#16a34a;--warn:#d97706;--err:#dc2626;--info:#4f46e5;
  --ok-bg:rgba(22,163,74,.08);--err-bg:rgba(220,38,38,.08);--warn-bg:rgba(217,119,6,.08);
  --sh:0 1px 3px rgba(15,23,42,.06),0 1px 2px rgba(15,23,42,.04);
  --sh-md:0 4px 16px rgba(15,23,42,.08);
  --sh-lg:0 8px 32px rgba(15,23,42,.1);
}
*,*::before,*::after{box-sizing:border-box}
html{transition:color-scheme .2s}
body{margin:0;background:var(--bg);color:var(--txt);
  font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;
  transition:background .25s,color .2s}
a{color:var(--brand);text-decoration:none}
a:hover{text-decoration:underline}

/* Brand */
.brand-badge{width:36px;height:36px;border-radius:10px;
  background:linear-gradient(135deg,var(--brand),color-mix(in srgb,var(--brand) 55%,#000 45%));
  display:flex;align-items:center;justify-content:center;
  font-weight:800;font-size:13px;color:#fff;flex-shrink:0;
  box-shadow:0 2px 10px color-mix(in srgb,var(--brand) 40%,transparent)}
.brand-logo{height:32px;width:auto;max-width:180px;object-fit:contain;display:block;flex-shrink:0}

/* Header */
header.topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 24px;height:56px;
  border-bottom:1px solid var(--bdr);
  position:sticky;top:0;z-index:100;
  background:color-mix(in srgb,var(--bg) 85%,transparent);
  backdrop-filter:blur(12px) saturate(1.4);
  -webkit-backdrop-filter:blur(12px) saturate(1.4);
  gap:16px;transition:background .25s,border-color .25s}
.topbar-left{display:flex;align-items:center;gap:12px;min-width:0;flex:1}
.topbar-left h1{font-size:15px;margin:0;font-weight:700;letter-spacing:-.01em;white-space:nowrap}
.topbar-left .sub{color:var(--dim);font-size:11.5px;white-space:nowrap;letter-spacing:.01em}
.topbar-right{display:flex;align-items:center;gap:6px}

/* Nav tabs */
nav.tabs{display:flex;gap:2px;background:var(--surf2);border:1px solid var(--bdr);
  border-radius:8px;padding:3px}
nav.tabs a{
  color:var(--dim);text-decoration:none;font-size:12.5px;font-weight:600;
  padding:5px 14px;border-radius:6px;transition:all .15s;white-space:nowrap;
  letter-spacing:.01em}
nav.tabs a:hover{color:var(--txt);background:var(--surf3);text-decoration:none}
nav.tabs a.active{
  color:var(--txt);background:var(--surf);
  box-shadow:var(--sh)}

/* Buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  padding:7px 16px;border:none;border-radius:8px;font-size:13px;font-weight:600;
  cursor:pointer;transition:all .15s;text-decoration:none;line-height:1;
  font-family:inherit;letter-spacing:.01em}
.btn:hover{text-decoration:none;filter:brightness(1.08)}
.btn:active{transform:scale(.98)}
.btn:disabled{opacity:.45;cursor:not-allowed;filter:none;transform:none}
.btn-primary{background:var(--brand);color:#fff;box-shadow:0 1px 3px color-mix(in srgb,var(--brand) 35%,transparent)}
.btn-primary:hover{box-shadow:0 4px 12px color-mix(in srgb,var(--brand) 40%,transparent)}
.btn-ghost{background:var(--surf2);color:var(--dim);border:1px solid var(--bdr)}
.btn-ghost:hover{color:var(--txt);background:var(--surf3)}
.btn-danger{background:var(--err-bg);color:var(--err);border:1px solid color-mix(in srgb,var(--err) 25%,transparent)}
.btn-danger:hover{background:color-mix(in srgb,var(--err) 18%,transparent)}
.btn-icon{background:transparent;border:none;color:var(--dim);cursor:pointer;
  padding:7px;border-radius:8px;display:flex;align-items:center;justify-content:center;
  transition:all .15s;font-family:inherit}
.btn-icon:hover{color:var(--txt);background:var(--surf2)}
.btn-sm{padding:5px 11px;font-size:12px;border-radius:6px}
.btn-xs{padding:3px 8px;font-size:11px;border-radius:5px}
.chip{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;
  border-radius:20px;font-size:12px;font-weight:600;
  background:var(--surf2);border:1px solid var(--bdr);color:var(--dim)}

/* Layout */
main{padding:24px 28px;max-width:1320px;margin:0 auto}
.page-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
.page-title{font-size:17px;font-weight:700;letter-spacing:-.02em;margin:0}
.section-label{font-size:11.5px;font-weight:700;color:var(--sub);
  text-transform:uppercase;letter-spacing:.08em;margin:0 0 12px}

/* Cards */
.card{background:var(--surf);border:1px solid var(--bdr);
  border-radius:var(--r);box-shadow:var(--sh);
  transition:box-shadow .2s,border-color .2s,background .25s}
.card-body{padding:20px}
.card-hover:hover{box-shadow:var(--sh-md);border-color:color-mix(in srgb,var(--brand) 25%,var(--bdr))}
.card-header{padding:14px 20px;border-bottom:1px solid var(--bdr);
  display:flex;align-items:center;justify-content:space-between;gap:10px}
.card-footer{padding:12px 20px;border-top:1px solid var(--bdr2);background:var(--surf2);border-radius:0 0 var(--r) var(--r)}

/* Status / Badges */
.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.s-ok{background:var(--ok)}
.s-err{background:var(--err)}
.s-run{background:var(--warn);box-shadow:0 0 0 3px var(--warn-bg);animation:spin-pulse 1.2s ease-in-out infinite}
.s-off{background:var(--sub)}
@keyframes spin-pulse{0%,100%{box-shadow:0 0 0 0 var(--warn-bg)}50%{box-shadow:0 0 0 5px transparent}}

.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:5px;
  font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.badge-twa{background:color-mix(in srgb,var(--brand) 14%,var(--surf2));
  color:var(--brand);border:1px solid color-mix(in srgb,var(--brand) 22%,transparent)}
.badge-last{background:color-mix(in srgb,var(--info) 14%,var(--surf2));
  color:var(--info);border:1px solid color-mix(in srgb,var(--info) 22%,transparent)}
.badge-ok{background:var(--ok-bg);color:var(--ok)}
.badge-err{background:var(--err-bg);color:var(--err)}

/* Forms */
.form-field{margin-bottom:16px}
.form-field label{display:block;font-size:12px;font-weight:600;color:var(--dim);
  margin-bottom:5px;letter-spacing:.01em}
input,select,textarea{
  background:var(--surf2);color:var(--txt);
  border:1px solid var(--bdr);border-radius:8px;
  padding:8px 11px;font-size:13px;width:100%;outline:none;
  font-family:inherit;line-height:1.4;
  transition:border-color .15s,box-shadow .15s,background .2s}
input:focus,select:focus,textarea:focus{
  border-color:var(--brand);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--brand) 18%,transparent)}
input::placeholder{color:var(--sub)}
textarea{resize:vertical}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.form-grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}

/* Toggle switch */
.toggle-wrap{display:flex;align-items:center;gap:9px;cursor:pointer}
.toggle-wrap input{display:none}
.toggle-track{width:38px;height:22px;border-radius:11px;
  background:var(--bdr);border:1px solid var(--bdr);
  position:relative;transition:background .2s,border-color .2s;flex-shrink:0}
.toggle-track::after{content:'';position:absolute;width:16px;height:16px;
  border-radius:50%;background:#fff;top:2px;left:2px;
  transition:left .2s,box-shadow .2s;
  box-shadow:0 1px 3px rgba(0,0,0,.25)}
.toggle-wrap input:checked+.toggle-track{background:var(--brand);border-color:var(--brand)}
.toggle-wrap input:checked+.toggle-track::after{left:18px}
.toggle-label{font-size:13px;font-weight:500;color:var(--txt);user-select:none}

/* Messages */
.msg{padding:10px 14px;border-radius:8px;font-size:13px;font-weight:500;margin-top:8px;display:flex;align-items:center;gap:8px}
.msg-ok{background:var(--ok-bg);color:var(--ok)}
.msg-err{background:var(--err-bg);color:var(--err)}

/* Tables */
.table-wrap{overflow-x:auto;border-radius:var(--r);border:1px solid var(--bdr)}
table{width:100%;border-collapse:collapse}
thead{background:var(--surf2)}
th{text-align:left;font-size:11px;color:var(--sub);font-weight:700;
  text-transform:uppercase;letter-spacing:.06em;
  padding:10px 14px;border-bottom:1px solid var(--bdr);white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid var(--bdr2);font-size:13px;vertical-align:middle;
  background:var(--surf);transition:background .2s}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--surf2)}

/* Misc */
.mono{font-family:var(--mono);font-size:12px}
.dim{color:var(--dim)}
.ok{color:var(--ok)}
.err{color:var(--err)}
.warn{color:var(--warn)}
.sep{border:none;border-top:1px solid var(--bdr);margin:16px 0}
.ellipsis{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.flex{display:flex}.gap-8{gap:8px}.gap-12{gap:12px}.gap-16{gap:16px}
.items-center{align-items:center}.justify-between{justify-content:space-between}
.flex-1{flex:1;min-width:0}
"""

# Boilerplate HTML head used by all pages
def _html_head(title: str) -> str:
    return f"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {{{{ product_name }}}}</title>
<script>{_THEME_INIT_JS}</script>
<style>""" + SHARED_CSS + """</style>
</head>"""

_NAV = """
<header class="topbar">
  <div class="topbar-left">
    {% if logo_url %}<img src="{{ logo_url }}" class="brand-logo" alt="{{ company_name }}">
    {% else %}<div class="brand-badge">{{ monogram }}</div>{% endif %}
    <div><h1>{{ product_name }}</h1><div class="sub">{{ company_name }}</div></div>
  </div>
  <nav class="tabs">
    <a href="/" class="{{ 'active' if active=='dashboard' else '' }}">Dashboard</a>
    <a href="/groups" class="{{ 'active' if active=='groups' else '' }}">Grupos</a>
    <a href="/logs" class="{{ 'active' if active=='logs' else '' }}">Logs</a>
  </nav>
  <div class="topbar-right">
""" + _THEME_BTN + """
    <span class="chip">{{ username }}</span>
    <form method="post" action="/logout" style="margin:0">
      <button class="btn btn-ghost btn-sm">Salir</button>
    </form>
  </div>
</header>
"""

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acceder · {{ product_name }}</title>
<script>""" + _THEME_INIT_JS + """</script>
<style>""" + SHARED_CSS + """
body{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.login-box{width:100%;max-width:360px}
.login-header{text-align:center;margin-bottom:28px}
.login-header h2{font-size:20px;margin:14px 0 5px;font-weight:800;letter-spacing:-.03em}
.login-header p{color:var(--dim);font-size:13px;margin:0}
.login-card{background:var(--surf);border:1px solid var(--bdr);
  border-radius:var(--r-lg);padding:28px;box-shadow:var(--sh-lg)}
.login-card .btn-primary{width:100%;justify-content:center;padding:10px 16px;font-size:14px}
</style></head><body>
<div class="login-box">
  <div class="login-header">
    {% if logo_url %}<img src="{{ logo_url }}" style="height:46px;margin:0 auto;display:block" alt="">
    {% else %}<div class="brand-badge" style="width:52px;height:52px;font-size:18px;margin:0 auto;border-radius:14px">{{ monogram }}</div>{% endif %}
    <h2>{{ company_name }}</h2>
    <p>{{ product_name }}</p>
  </div>
  <div class="login-card">
    {% if error %}<div class="msg msg-err" style="margin-bottom:16px">{{ error }}</div>{% endif %}
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{{ next }}">
      <div class="form-field"><label>Usuario</label>
        <input name="username" value="{{ username }}" autocomplete="username" autofocus placeholder="admin">
      </div>
      <div class="form-field" style="margin-bottom:20px"><label>Contraseña</label>
        <input type="password" name="password" autocomplete="current-password" placeholder="••••••••">
      </div>
      <button type="submit" class="btn btn-primary">Iniciar sesión</button>
    </form>
  </div>
</div>
<script>""" + _THEME_TOGGLE_JS + """</script>
</body></html>"""

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard · {{ product_name }}</title>
<script>""" + _THEME_INIT_JS + """</script>
<style>""" + SHARED_CSS + """
.stats-bar{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
.stat-pill{background:var(--surf);border:1px solid var(--bdr);
  border-radius:8px;padding:9px 16px;font-size:13px;
  display:flex;align-items:center;gap:8px;box-shadow:var(--sh)}
.stat-pill b{font-weight:700;font-size:15px}
.group-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.group-card{
  background:var(--surf);border:1px solid var(--bdr);border-radius:var(--r);
  box-shadow:var(--sh);overflow:hidden;
  display:flex;flex-direction:column;
  transition:box-shadow .2s,border-color .2s,transform .2s}
.group-card:hover{box-shadow:var(--sh-md);transform:translateY(-1px)}
.group-card-accent{height:3px;background:var(--brand)}
.group-card-accent.ok{background:var(--ok)}
.group-card-accent.err{background:var(--err)}
.group-card-accent.run{background:var(--warn)}
.group-card-body{padding:16px 18px;flex:1;display:flex;flex-direction:column;gap:10px}
.group-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.group-card-name{font-weight:700;font-size:15px;letter-spacing:-.01em;line-height:1.3}
.group-card-table{font-family:var(--mono);font-size:11.5px;color:var(--sub);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.group-metrics{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.metric{background:var(--surf2);border:1px solid var(--bdr2);
  border-radius:7px;padding:8px 10px}
.metric-label{font-size:10.5px;color:var(--sub);font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-bottom:2px}
.metric-value{font-size:14px;font-weight:700;letter-spacing:-.01em}
.group-card-footer{
  padding:10px 18px;border-top:1px solid var(--bdr2);background:var(--surf2);
  display:flex;align-items:center;justify-content:space-between;gap:8px}
.schedule-info{font-size:12px;color:var(--dim)}
.empty-state{text-align:center;padding:72px 20px;grid-column:1/-1}
.empty-state .empty-icon{font-size:40px;margin-bottom:14px;opacity:.3}
.empty-state h3{font-size:16px;font-weight:700;margin:0 0 8px;letter-spacing:-.02em}
.empty-state p{color:var(--dim);margin:0 0 20px;font-size:14px}
</style></head><body>
""" + _NAV + """
<main>
  <div class="stats-bar" id="stats-bar"></div>
  <div class="section-label">Grupos de carga</div>
  <div class="group-grid" id="group-grid"></div>
  <div style="margin-top:32px">
    <div class="section-label">Últimas ejecuciones</div>
    <div class="table-wrap">
      <table><thead><tr>
        <th>Grupo</th><th>Estado</th><th>Disparador</th>
        <th>Filas</th><th>Tags</th><th>Duración</th><th>Inicio</th>
      </tr></thead>
      <tbody id="runs-body"></tbody></table>
    </div>
  </div>
</main>
<script>
const R={{ refresh_ms }};
function relTime(iso){
  const s=Math.round((Date.now()-new Date(iso))/1000);
  if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'min';
  if(s<86400)return Math.floor(s/3600)+'h';return Math.floor(s/86400)+'d';
}
function accentClass(g){
  if(g.is_running)return 'run';
  if(!g.last_run)return '';
  return g.last_run.status==='ok'?'ok':'err';
}
function statusHtml(g){
  if(g.is_running)return `<span style="color:var(--warn)">● Sincronizando…</span>`;
  if(!g.last_run)return `<span class="dim">Sin ejecuciones</span>`;
  if(g.last_run.status==='ok')return `<span class="ok">● OK</span> <span class="dim">${relTime(g.last_run.started_at)}</span>`;
  return `<span class="err">● ${g.last_run.error_category||'Error'}</span>`;
}
function rowsHtml(g){
  if(!g.last_run||g.last_run.status!=='ok')return '<span class="dim">—</span>';
  return `<b>${g.last_run.rows_written.toLocaleString()}</b>`;
}
function durationHtml(g){
  if(!g.last_run||g.last_run.status!=='ok')return '<span class="dim">—</span>';
  return `${g.last_run.duration_seconds}s`;
}
function renderGroups(groups){
  const el=document.getElementById('group-grid');
  if(!groups.length){
    el.innerHTML=`<div class="empty-state">
      <div class="empty-icon">⬡</div>
      <h3>No hay grupos configurados</h3>
      <p>Crea tu primer grupo de carga para empezar a sincronizar datos.</p>
      <a href="/groups" class="btn btn-primary">Crear primer grupo</a>
    </div>`;
    return;
  }
  el.innerHTML=groups.map(g=>`
    <div class="group-card">
      <div class="group-card-accent ${accentClass(g)}"></div>
      <div class="group-card-body">
        <div class="group-card-head">
          <div>
            <div class="group-card-name">${g.name}</div>
            <div class="group-card-table" title="${g.target_table}">${g.target_table}</div>
          </div>
          <span class="badge badge-${g.agg_type}">${g.agg_type}</span>
        </div>
        <div class="group-metrics">
          <div class="metric">
            <div class="metric-label">Estado</div>
            <div class="metric-value" style="font-size:13px">${statusHtml(g)}</div>
          </div>
          <div class="metric">
            <div class="metric-label">Filas escritas</div>
            <div class="metric-value">${rowsHtml(g)}</div>
          </div>
          <div class="metric">
            <div class="metric-label">Tags</div>
            <div class="metric-value">${g.tags_count.toLocaleString()}</div>
          </div>
          <div class="metric">
            <div class="metric-label">Duración</div>
            <div class="metric-value">${durationHtml(g)}</div>
          </div>
        </div>
      </div>
      <div class="group-card-footer">
        <div class="schedule-info">${g.next_run_madrid?'Próxima: <b>'+g.next_run_madrid+'</b>':g.enabled?'<span class="dim">Sin schedule</span>':'<span class="dim">Deshabilitado</span>'}</div>
        <button class="btn btn-primary btn-sm" onclick="runNow('${g.id}',this)" ${g.is_running||!g.enabled?'disabled':''}>▶ Ejecutar</button>
      </div>
    </div>`).join('');
}
function renderRuns(runs){
  document.getElementById('runs-body').innerHTML=runs.slice(0,40).map(r=>{
    const ok=r.status==='ok';
    const dot=ok?'s-ok':'s-err';
    return `<tr>
      <td><b>${r.group_name}</b></td>
      <td><span class="status-dot ${dot}" style="margin-right:6px"></span>${ok?'OK':r.error_category||'Error'}</td>
      <td><span class="dim">${r.trigger_label}</span></td>
      <td>${ok?r.rows_written.toLocaleString():'—'}</td>
      <td class="dim">${r.tags_received}/${r.tags_expected}</td>
      <td class="mono">${r.duration_seconds}s</td>
      <td class="dim">${new Date(r.started_at).toLocaleString('es-ES')}</td>
    </tr>`;
  }).join('');
}
function renderStats(st,groups){
  const running=groups.filter(g=>g.is_running).length;
  document.getElementById('stats-bar').innerHTML=
    `<div class="stat-pill"><span class="dim">Grupos activos</span><b>${st.enabled_groups}</b></div>`+
    `<div class="stat-pill"><span class="dim">Total grupos</span><b>${st.total_groups}</b></div>`+
    (running?`<div class="stat-pill"><span style="color:var(--warn)">●</span><b>${running} sincronizando…</b></div>`:'');
}
async function runNow(gid,btn){
  btn.disabled=true;btn.textContent='…';
  const d=await fetch('/api/groups/'+gid+'/run-now',{method:'POST'}).then(r=>r.json());
  if(!d.ok){alert(d.message);btn.disabled=false;btn.textContent='▶ Ejecutar';}
}
async function refresh(){
  try{
    const[gs,rs,st]=await Promise.all([
      fetch('/api/groups').then(r=>r.json()),
      fetch('/api/runs?limit=40').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()),
    ]);
    renderGroups(gs);renderRuns(rs);renderStats(st,gs);
  }catch(e){console.error(e)}
}
refresh();setInterval(refresh,R);
</script>
<script>""" + _THEME_TOGGLE_JS + """</script>
</body></html>"""

# ---------------------------------------------------------------------------
# Groups management
# ---------------------------------------------------------------------------

GROUPS_HTML = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Grupos · {{ product_name }}</title>
<script>""" + _THEME_INIT_JS + """</script>
<style>""" + SHARED_CSS + """
.groups-layout{display:grid;grid-template-columns:1fr;gap:16px}
.group-row{
  display:flex;align-items:center;gap:14px;padding:14px 18px;
  border-bottom:1px solid var(--bdr2);transition:background .15s}
.group-row:last-child{border-bottom:none}
.group-row:hover{background:var(--surf2)}
.group-row-info{flex:1;min-width:0}
.group-row-name{font-weight:700;font-size:14px;display:flex;align-items:center;gap:8px;margin-bottom:2px}
.group-row-meta{font-size:12px;color:var(--sub);font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.group-row-sched{font-size:11.5px;color:var(--dim);margin-top:2px}
/* Editor panel */
.editor-panel{
  background:var(--surf);border:1px solid var(--bdr);border-radius:var(--r-lg);
  box-shadow:var(--sh-lg);overflow:hidden;margin-bottom:16px;display:none}
.editor-panel.open{display:block}
.editor-header{
  padding:14px 20px;border-bottom:1px solid var(--bdr);
  display:flex;align-items:center;justify-content:space-between;
  background:var(--surf2)}
.editor-header h3{margin:0;font-size:15px;font-weight:700;letter-spacing:-.01em}
/* Sub-tabs inside editor */
.sub-tabs{display:flex;gap:0;border-bottom:1px solid var(--bdr);background:var(--surf2);padding:0 20px}
.sub-tab{padding:10px 16px;font-size:13px;font-weight:600;color:var(--dim);
  border:none;background:none;cursor:pointer;border-bottom:2px solid transparent;
  margin-bottom:-1px;transition:color .15s;font-family:inherit}
.sub-tab.active{color:var(--brand);border-bottom-color:var(--brand)}
.sub-tab:hover{color:var(--txt)}
.sub-panel{display:none;padding:20px 24px}
.sub-panel.active{display:block}
/* Schedule mode blocks */
.sched-block{display:none}.sched-block.active{display:block}
.editor-footer{
  padding:14px 24px;border-top:1px solid var(--bdr);
  display:flex;align-items:center;gap:10px;background:var(--surf2)}
</style></head><body>
""" + _NAV + """
<main>
<div class="page-header">
  <div>
    <h2 class="page-title">Grupos de carga</h2>
    <p style="margin:4px 0 0;font-size:13px;color:var(--dim)">Configura cada grupo de tags con su tabla destino, tipo de agregación, schedule y ventana de datos.</p>
  </div>
  <button class="btn btn-primary" onclick="openEditor(null)">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M12 5v14M5 12h14"/></svg>
    Nuevo grupo
  </button>
</div>

<!-- Editor -->
<div class="editor-panel" id="editor-panel">
  <div class="editor-header">
    <h3 id="editor-title">Nuevo grupo</h3>
    <button class="btn-icon" onclick="closeEditor()" title="Cerrar">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 6 6 18M6 6l12 12"/></svg>
    </button>
  </div>
  <div class="sub-tabs">
    <button class="sub-tab active" data-panel="info" onclick="showTab('info')">Configuración</button>
    <button class="sub-tab" data-panel="tags" onclick="showTab('tags')">Tags</button>
    <button class="sub-tab" data-panel="sched" onclick="showTab('sched')">Planificación</button>
    <button class="sub-tab" data-panel="window" onclick="showTab('window')">Ventana</button>
  </div>

  <div id="sp-info" class="sub-panel active">
    <div class="form-grid">
      <div class="form-field"><label>Nombre del grupo</label>
        <input id="f-name" placeholder="Ej: Tags TWA Molino 1"></div>
      <div class="form-field"><label>Tipo de agregación</label>
        <select id="f-agg" onchange="toggleTbnameField()">
          <option value="twa">TWA – Media ponderada en el tiempo</option>
          <option value="last">LAST – Último valor registrado (horometros)</option>
        </select></div>
      <div class="form-field"><label>Tabla destino en SQL Server</label>
        <input id="f-table" placeholder="dbo.EicTagsHourlyAvg"></div>
      <div class="form-field" style="display:flex;align-items:flex-end;padding-bottom:4px">
        <label class="toggle-wrap">
          <input type="checkbox" id="f-enabled" checked>
          <span class="toggle-track"></span>
          <span class="toggle-label">Grupo habilitado</span>
        </label>
      </div>
    </div>
  </div>

  <div id="sp-tags" class="sub-panel">
    <div id="f-tbname-wrap" class="form-field" style="display:none">
      <label>Patrón tbname (descubrimiento automático de tags)</label>
      <input id="f-tbname" placeholder="%Horometro%">
      <div style="font-size:11px;color:var(--dim);margin-top:5px">Si se define, los tags se descubren dinámicamente en cada ciclo consultando TDengine con <code>WHERE tbname LIKE \'patrón\'</code>. El listado manual de abajo se ignora.</div>
    </div>
    <div class="form-field">
      <label>Tags (uno por línea)</label>
      <textarea id="f-tags" rows="14" style="font-family:var(--mono);font-size:12.5px;line-height:1.6" placeholder="TAG1&#10;TAG2&#10;TAG3" oninput="updateTagCount()"></textarea>
    </div>
    <div style="font-size:12px;color:var(--dim)" id="tag-count">0 tags</div>
  </div>

  <div id="sp-sched" class="sub-panel">
    <div class="form-grid">
      <div class="form-field"><label>Modo de planificación</label>
        <select id="f-sched-mode" onchange="onSchedMode()">
          <option value="interval">Cada N minutos</option>
          <option value="times">A horas concretas del día (hora de Madrid)</option>
          <option value="cron">Expresión cron avanzada</option>
        </select></div>
    </div>
    <div id="sb-interval" class="sched-block active">
      <div class="form-field" style="max-width:220px">
        <label>Intervalo (minutos, 1–1440)</label>
        <input id="f-interval" type="number" min="1" max="1440" value="5">
      </div>
    </div>
    <div id="sb-times" class="sched-block">
      <div class="form-field" style="max-width:320px">
        <label>Horas de ejecución (HH:MM, una por línea o separadas por comas)</label>
        <textarea id="f-times" rows="4" placeholder="06:00&#10;14:00&#10;22:00"></textarea>
      </div>
    </div>
    <div id="sb-cron" class="sched-block">
      <div class="form-field" style="max-width:320px">
        <label>Expresión cron (5 campos, hora de Madrid)</label>
        <input id="f-cron" placeholder="*/5 * * * *">
        <div style="font-size:12px;color:var(--sub);margin-top:5px">Formato: minuto hora día-mes mes día-semana</div>
      </div>
    </div>
  </div>

  <div id="sp-window" class="sub-panel">
    <div class="form-field" style="max-width:380px">
      <label>Modo de ventana temporal</label>
      <select id="f-dr-mode" onchange="onDrMode()">
        <option value="shift_yesterday">Ventana diaria (medianoche −15h → hora actual)</option>
        <option value="all_time">Histórico completo (2020-01-01 → hora actual) — horómetros</option>
        <option value="turno_diario">Turno diario (ayer 21:00 → hoy 21:00, hora de Madrid)</option>
        <option value="shift_date">Turno de fecha concreta</option>
        <option value="range">Rango libre (UTC)</option>
      </select>
    </div>
    <div id="dr-shift_date" style="display:none;max-width:220px">
      <div class="form-field"><label>Fecha del turno</label><input id="f-dr-date" type="date"></div>
    </div>
    <div id="dr-range" style="display:none">
      <div class="form-grid" style="max-width:520px">
        <div class="form-field"><label>Inicio (UTC)</label><input id="f-dr-start" type="datetime-local"></div>
        <div class="form-field"><label>Fin (UTC)</label><input id="f-dr-end" type="datetime-local"></div>
      </div>
    </div>
  </div>

  <div class="editor-footer">
    <button class="btn btn-primary" onclick="saveGroup()">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
      Guardar grupo
    </button>
    <button class="btn btn-ghost" onclick="closeEditor()">Cancelar</button>
    <div id="editor-msg" style="flex:1"></div>
  </div>
</div>

<!-- Groups list -->
<div class="card" style="padding:0;overflow:hidden" id="groups-list">
  <div style="text-align:center;padding:48px;color:var(--dim)">Cargando…</div>
</div>
</main>
<script>
let editId=null,allGroups=[];
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function showTab(name){
  document.querySelectorAll('.sub-tab').forEach(t=>t.classList.toggle('active',t.dataset.panel===name));
  document.querySelectorAll('.sub-panel').forEach(p=>p.classList.toggle('active',p.id==='sp-'+name));
  if(name==='tags')updateTagCount();
}
function onSchedMode(){
  const m=document.getElementById('f-sched-mode').value;
  ['interval','times','cron'].forEach(k=>document.getElementById('sb-'+k).classList.toggle('active',k===m));
}
function onDrMode(){
  const m=document.getElementById('f-dr-mode').value;
  ['shift_date','range'].forEach(k=>document.getElementById('dr-'+k).style.display=k===m?'':'none');
}
function updateTagCount(){
  const n=document.getElementById('f-tags').value.split('\\n').filter(l=>l.trim()).length;
  document.getElementById('tag-count').textContent=n.toLocaleString()+' tag'+(n!==1?'s':'');
}
function toggleTbnameField(){
  const isLast=document.getElementById('f-agg').value==='last';
  document.getElementById('f-tbname-wrap').style.display=isLast?'':'none';
}
function openEditorById(id){
  const g=allGroups.find(x=>x.id===id);
  if(g)openEditor(g);
}
function openEditor(g){
  editId=g?g.id:null;
  document.getElementById('editor-title').textContent=g?'Editar: '+g.name:'Nuevo grupo';
  document.getElementById('f-name').value=g?g.name:'';
  document.getElementById('f-agg').value=g?g.agg_type:'twa';
  document.getElementById('f-table').value=g?g.target_table:'';
  document.getElementById('f-enabled').checked=g?g.enabled:true;
  document.getElementById('f-tags').value=g?(g.tags||[]).join('\\n'):'';
  document.getElementById('f-tbname').value=g?(g.tbname_pattern||''):'';
  updateTagCount();toggleTbnameField();
  const sc=g?g.schedule:{mode:'interval',interval_minutes:5,times:['06:00'],cron:'*/5 * * * *'};
  document.getElementById('f-sched-mode').value=sc.mode||'interval';
  document.getElementById('f-interval').value=sc.interval_minutes||5;
  document.getElementById('f-times').value=(sc.times||[]).join('\\n');
  document.getElementById('f-cron').value=sc.cron||'*/5 * * * *';
  onSchedMode();
  const dr=g?g.date_range:{mode:'shift_yesterday'};
  document.getElementById('f-dr-mode').value=dr.mode||'shift_yesterday';
  document.getElementById('f-dr-date').value=dr.shift_date||'';
  document.getElementById('f-dr-start').value=dr.range_start||'';
  document.getElementById('f-dr-end').value=dr.range_end||'';
  onDrMode();
  const p=document.getElementById('editor-panel');
  p.classList.add('open');
  showTab('info');setMsg('','');
  p.scrollIntoView({behavior:'smooth',block:'start'});
}
function closeEditor(){document.getElementById('editor-panel').classList.remove('open');editId=null;}
function setMsg(txt,type){
  document.getElementById('editor-msg').innerHTML=txt?`<div class="msg msg-${type}">${txt}</div>`:'';
}
async function saveGroup(){
  const mode=document.getElementById('f-sched-mode').value;
  const times=document.getElementById('f-times').value.split(/[\\n,]/).map(t=>t.trim()).filter(Boolean);
  const body={
    name:document.getElementById('f-name').value.trim(),
    agg_type:document.getElementById('f-agg').value,
    target_table:document.getElementById('f-table').value.trim(),
    enabled:document.getElementById('f-enabled').checked,
    tags_text:document.getElementById('f-tags').value,
    tbname_pattern:document.getElementById('f-tbname').value.trim()||null,
    schedule:{mode,interval_minutes:parseInt(document.getElementById('f-interval').value)||5,times,cron:document.getElementById('f-cron').value},
    date_range:{
      mode:document.getElementById('f-dr-mode').value,
      shift_date:document.getElementById('f-dr-date').value||null,
      range_start:document.getElementById('f-dr-start').value||null,
      range_end:document.getElementById('f-dr-end').value||null,
    },
  };
  const url=editId?'/api/groups/'+editId:'/api/groups';
  const d=await fetch(url,{method:editId?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  if(d.ok){setMsg('Grupo guardado correctamente.','ok');closeEditor();loadGroups();}
  else setMsg(d.message||'Error al guardar.','err');
}
async function deleteGroup(id,name){
  if(!confirm('¿Eliminar el grupo "'+name+'"?'))return;
  await fetch('/api/groups/'+id,{method:'DELETE'});
  loadGroups();
}
async function toggleGroup(id,val){
  await fetch('/api/groups/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:val})});
  loadGroups();
}
function renderList(groups){
  allGroups=groups;
  const el=document.getElementById('groups-list');
  if(!groups.length){
    el.innerHTML=`<div style="text-align:center;padding:56px;color:var(--dim)">
      <div style="font-size:32px;margin-bottom:12px;opacity:.3">⬡</div>
      <div style="font-weight:600;font-size:14px;margin-bottom:6px">No hay grupos todavía</div>
      <div style="font-size:13px">Usa el botón <b>Nuevo grupo</b> para crear el primero.</div>
    </div>`;
    return;
  }
  el.innerHTML=groups.map(g=>{
    const last=g.last_run;
    const statusStr=last?(last.status==='ok'?`<span class="ok">● OK · ${last.rows_written.toLocaleString()} filas</span>`:`<span class="err">● ${esc(last.error_category||'Error')}</span>`):'<span class="dim">Sin ejecuciones</span>';
    const dot=g.is_running?'s-run':!g.enabled?'s-off':!last?'s-off':last.status==='ok'?'s-ok':'s-err';
    return `<div class="group-row">
      <div class="status-dot ${dot}"></div>
      <div class="group-row-info ellipsis">
        <div class="group-row-name">${esc(g.name)} <span class="badge badge-${g.agg_type}">${g.agg_type}</span></div>
        <div class="group-row-meta">${esc(g.target_table)} · ${g.tags_count} tags · ${statusStr}</div>
        <div class="group-row-sched">${esc(g.schedule_description)}</div>
      </div>
      <label class="toggle-wrap" title="${g.enabled?'Deshabilitar':'Habilitar'}">
        <input type="checkbox" ${g.enabled?'checked':''} onchange="toggleGroup('${g.id}',this.checked)">
        <span class="toggle-track"></span>
      </label>
      <button class="btn btn-ghost btn-sm" onclick="openEditorById('${g.id}')">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        Editar
      </button>
      <button class="btn btn-danger btn-sm" data-gid="${g.id}" data-gname="${esc(g.name)}" onclick="deleteGroup(this.dataset.gid,this.dataset.gname)">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
      </button>
    </div>`;
  }).join('');
}
async function loadGroups(){
  const groups=await fetch('/api/groups').then(r=>r.json());
  renderList(groups);
}
loadGroups();setInterval(loadGroups,{{ refresh_ms }});
</script>
<script>""" + _THEME_TOGGLE_JS + """</script>
</body></html>"""

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

LOGS_HTML = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Logs · {{ product_name }}</title>
<script>""" + _THEME_INIT_JS + """</script>
<style>""" + SHARED_CSS + """
.log-toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.log-box{
  background:color-mix(in srgb,var(--surf) 85%,#000);
  border:1px solid var(--bdr);border-radius:var(--r);
  max-height:calc(100vh - 220px);overflow-y:auto;
  font-family:var(--mono);font-size:12px;line-height:1.7}
.log-line{display:flex;gap:10px;padding:3px 14px;border-bottom:1px solid color-mix(in srgb,var(--bdr2) 50%,transparent)}
.log-line:last-child{border-bottom:none}
.log-line:hover{background:color-mix(in srgb,var(--brand) 5%,transparent)}
.log-ts{color:var(--sub);white-space:nowrap;flex-shrink:0}
.log-lvl{width:52px;flex-shrink:0;font-weight:700}
.log-logger{color:var(--dim);flex-shrink:0;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.log-msg{color:var(--txt);white-space:pre-wrap;word-break:break-word;flex:1}
.INFO .log-lvl{color:var(--dim)}
.WARNING .log-lvl{color:var(--warn)}
.ERROR .log-lvl,.CRITICAL .log-lvl{color:var(--err)}
.DEBUG .log-lvl{color:var(--sub)}
</style></head><body>
""" + _NAV + """
<main>
  <div class="log-toolbar">
    <h2 class="page-title" style="margin:0">Logs en vivo</h2>
    <select id="f-level" onchange="refresh()" style="width:auto">
      <option value="">Todos</option>
      <option value="DEBUG">DEBUG</option>
      <option value="INFO">INFO</option>
      <option value="WARNING">WARNING</option>
      <option value="ERROR">ERROR</option>
    </select>
    <input id="f-search" placeholder="Filtrar por texto…" style="width:220px" oninput="applyFilter()">
    <label class="toggle-wrap" style="margin-left:auto">
      <input type="checkbox" id="f-scroll" checked>
      <span class="toggle-track"></span>
      <span class="toggle-label">Auto-scroll</span>
    </label>
    <button class="btn btn-ghost btn-sm" onclick="document.getElementById('log-box').innerHTML='';rawLogs=[]">Limpiar vista</button>
  </div>
  <div class="log-box" id="log-box"></div>
</main>
<script>
const R={{ refresh_ms }};
let rawLogs=[];
function applyFilter(){
  const q=document.getElementById('f-search').value.toLowerCase();
  const visible=rawLogs.filter(r=>!q||r.message.toLowerCase().includes(q)||r.logger.toLowerCase().includes(q));
  const box=document.getElementById('log-box');
  const atBot=box.scrollHeight-box.scrollTop-box.clientHeight<60;
  box.innerHTML=visible.map(r=>`<div class="log-line ${r.level}">
    <span class="log-ts">${r.timestamp.slice(0,23).replace('T',' ')}</span>
    <span class="log-lvl">${r.level}</span>
    <span class="log-logger">${r.logger}</span>
    <span class="log-msg">${r.message.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</span>
  </div>`).join('');
  if(atBot&&document.getElementById('f-scroll').checked)box.scrollTop=box.scrollHeight;
}
async function refresh(){
  const lv=document.getElementById('f-level').value;
  rawLogs=await fetch('/api/logs?limit=600'+(lv?'&level='+lv:'')).then(r=>r.json());
  applyFilter();
}
refresh();setInterval(refresh,R);
</script>
<script>""" + _THEME_TOGGLE_JS + """</script>
</body></html>"""
