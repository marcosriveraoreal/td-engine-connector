"""Traduce excepciones del ciclo de sincronización a explicaciones legibles para el panel.

Para cada fallo se calcula una categoría corta (qué ha pasado), una sugerencia de qué
revisar (por qué probablemente ha pasado y qué hacer) y el detalle técnico original (para
quien necesite el mensaje exacto de la excepción).
"""
from __future__ import annotations

from dataclasses import dataclass

import pyodbc
import requests

from tdengine_client import TDengineError


@dataclass(frozen=True)
class ErrorInfo:
    category: str
    hint: str
    detail: str


def classify_error(exc: Exception) -> ErrorInfo:
    detail = str(exc).strip() or exc.__class__.__name__

    if isinstance(exc, requests.exceptions.Timeout):
        return ErrorInfo(
            category="TDengine no respondió a tiempo",
            hint="Comprueba la carga de TDengine o aumenta TD_REQUEST_TIMEOUT_SECONDS.",
            detail=detail,
        )
    if isinstance(exc, requests.exceptions.ConnectionError):
        return ErrorInfo(
            category="No se pudo conectar con TDengine",
            hint="Verifica TD_URL y que el contenedor tenga red hacia TDengine.",
            detail=detail,
        )
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status == 401:
            return ErrorInfo(
                category="TDengine rechazó las credenciales",
                hint="Revisa TD_USER y TD_PASSWORD.",
                detail=detail,
            )
        return ErrorInfo(
            category=f"TDengine respondió con error HTTP {status}" if status else "TDengine respondió con error HTTP",
            hint="Revisa el estado del servicio TDengine.",
            detail=detail,
        )
    if isinstance(exc, TDengineError):
        return ErrorInfo(
            category="TDengine devolvió un error de consulta",
            hint="Revisa la base/tabla configuradas (TD_DATABASE, TD_TABLE) y la lista de tags.",
            detail=detail,
        )
    if isinstance(exc, pyodbc.OperationalError):
        return ErrorInfo(
            category="No se pudo conectar con SQL Server",
            hint="Verifica MSSQL_HOST, MSSQL_PORT y que el contenedor tenga red hacia SQL Server.",
            detail=detail,
        )
    if isinstance(exc, pyodbc.InterfaceError):
        return ErrorInfo(
            category="SQL Server rechazó la conexión o las credenciales",
            hint="Revisa MSSQL_USER, MSSQL_PASSWORD y el driver ODBC instalado.",
            detail=detail,
        )
    if isinstance(exc, pyodbc.ProgrammingError):
        return ErrorInfo(
            category="Error de SQL Server al escribir los datos",
            hint="Revisa MSSQL_TARGET_TABLE y los permisos del usuario sobre esa tabla.",
            detail=detail,
        )
    if isinstance(exc, pyodbc.Error):
        return ErrorInfo(
            category="Error de SQL Server",
            hint="Revisa la conexión y los permisos del usuario de base de datos.",
            detail=detail,
        )
    if isinstance(exc, requests.exceptions.RequestException):
        return ErrorInfo(
            category="Error de red hacia TDengine",
            hint="Revisa la conectividad de red del contenedor.",
            detail=detail,
        )
    return ErrorInfo(
        category="Error inesperado en el ciclo de sincronización",
        hint="Revisa el detalle técnico y los logs completos del contenedor.",
        detail=detail,
    )
