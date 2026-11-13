"""Carga y validación de configuración desde variables de entorno."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import List


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"La variable de entorno '{name}' es obligatoria y no está definida.")
    return value


def _optional(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value if value else default


def _optional_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"La variable de entorno '{name}' debe ser un entero, recibido: '{value}'") from exc


def _optional_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class TDengineConfig:
    url: str
    user: str
    password: str
    database: str
    table: str
    request_timeout: int


@dataclass(frozen=True)
class SqlServerConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    connect_timeout: int
    encrypt: str
    trust_server_certificate: str


@dataclass(frozen=True)
class WebUiConfig:
    enabled: bool
    host: str
    port: int
    user: str
    password: str
    password_generated: bool
    secret_key: str
    max_runs_history: int
    max_log_lines: int
    refresh_seconds: int


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool
    rabbitmq_host: str
    rabbitmq_port: int
    rabbitmq_user: str
    rabbitmq_password: str
    rabbitmq_vhost: str
    destinatarios: List[str]
    cc: str
    modelo_correo: int
    cuenta_correo: int
    auth_token: str


@dataclass(frozen=True)
class BrandingConfig:
    company_name: str
    product_name: str
    accent_color: str
    logo_url: str
    assets_dir: str


@dataclass(frozen=True)
class AppConfig:
    tdengine: TDengineConfig
    sqlserver: SqlServerConfig
    groups_state_file: str
    run_once: bool
    sync_on_startup: bool
    dry_run: bool
    log_level: str
    heartbeat_file: str
    heartbeat_max_age_seconds: int
    web_ui: WebUiConfig
    branding: BrandingConfig
    notification: NotificationConfig


def load_config() -> AppConfig:
    tdengine = TDengineConfig(
        url=_require("TD_URL"),
        user=_require("TD_USER"),
        password=_require("TD_PASSWORD"),
        database=_optional("TD_DATABASE", "eic_data"),
        table=_optional("TD_TABLE", "eic_tags_float"),
        request_timeout=_optional_int("TD_REQUEST_TIMEOUT_SECONDS", 120),
    )

    sqlserver = SqlServerConfig(
        host=_require("MSSQL_HOST"),
        port=_optional_int("MSSQL_PORT", 1433),
        database=_require("MSSQL_DATABASE"),
        user=_require("MSSQL_USER"),
        password=_require("MSSQL_PASSWORD"),
        connect_timeout=_optional_int("MSSQL_CONNECT_TIMEOUT_SECONDS", 15),
        encrypt=_optional("MSSQL_ENCRYPT", "no"),
        trust_server_certificate=_optional("MSSQL_TRUST_SERVER_CERTIFICATE", "yes"),
    )

    web_user = _optional("WEB_UI_USER", "")
    web_password = _optional("WEB_UI_PASSWORD", "")
    password_generated = False
    if not web_user and not web_password:
        web_user = "admin"
        web_password = secrets.token_urlsafe(12)
        password_generated = True
    elif bool(web_user) != bool(web_password):
        raise ConfigError(
            "WEB_UI_USER y WEB_UI_PASSWORD deben definirse juntos (o ninguno de los dos)."
        )

    web_ui = WebUiConfig(
        enabled=_optional_bool("WEB_UI_ENABLED", True),
        host=_optional("WEB_UI_HOST", "0.0.0.0"),
        port=_optional_int("WEB_UI_PORT", 3892),
        user=web_user,
        password=web_password,
        password_generated=password_generated,
        secret_key=_optional("WEB_UI_SECRET_KEY", "") or secrets.token_hex(32),
        max_runs_history=_optional_int("WEB_UI_MAX_RUNS_HISTORY", 200),
        max_log_lines=_optional_int("WEB_UI_MAX_LOG_LINES", 1000),
        refresh_seconds=_optional_int("WEB_UI_REFRESH_SECONDS", 10),
    )

    branding = BrandingConfig(
        company_name=_optional("COMPANY_NAME", "Atalaya Mining"),
        product_name=_optional("PRODUCT_NAME", "connector-td-engine"),
        accent_color=_optional("BRAND_ACCENT_COLOR", "#c8763a"),
        logo_url=_optional("BRAND_LOGO_URL", ""),
        assets_dir=_optional("ASSETS_DIR", "/app/assets"),
    )

    destinatarios_raw = _optional("EMAIL_DESTINATARIOS", "")
    destinatarios = [e.strip() for e in destinatarios_raw.split(",") if e.strip()]

    notification = NotificationConfig(
        enabled=_optional_bool("EMAIL_ENABLED", False),
        rabbitmq_host=_optional("RABBITMQ_HOST", ""),
        rabbitmq_port=_optional_int("RABBITMQ_PORT", 5672),
        rabbitmq_user=_optional("RABBITMQ_USER", ""),
        rabbitmq_password=_optional("RABBITMQ_PASSWORD", ""),
        rabbitmq_vhost=_optional("RABBITMQ_VHOST", "/"),
        destinatarios=destinatarios,
        cc=_optional("EMAIL_CC", ""),
        modelo_correo=_optional_int("EMAIL_MODELO_CORREO", 3),
        cuenta_correo=_optional_int("EMAIL_CUENTA_CORREO", 12),
        auth_token=_optional("EMAIL_AUTH_TOKEN", ""),
    )

    return AppConfig(
        tdengine=tdengine,
        sqlserver=sqlserver,
        groups_state_file=_optional("GROUPS_STATE_FILE", "/app/state/groups.json"),
        run_once=_optional_bool("RUN_ONCE", False),
        sync_on_startup=_optional_bool("SYNC_ON_STARTUP", True),
        dry_run=_optional_bool("DRY_RUN", False),
        log_level=_optional("LOG_LEVEL", "INFO"),
        heartbeat_file=_optional("HEARTBEAT_FILE", "/app/state/heartbeat"),
        heartbeat_max_age_seconds=_optional_int("HEARTBEAT_MAX_AGE_SECONDS", 900),
        web_ui=web_ui,
        branding=branding,
        notification=notification,
    )
