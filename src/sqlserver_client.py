"""Cliente SQL Server: upsert de datos horarios por tag.

Usa el driver ODBC oficial de Microsoft (msodbcsql18) vía pyodbc — requerido por este
SQL Server concreto, que solo completa el handshake TDS con el driver oficial de Microsoft.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, List, Optional, TypedDict

import pyodbc
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import SqlServerConfig
from tdengine_client import HourlyAvgRow, HourlyLastRow

log = logging.getLogger(__name__)

_ODBC_DRIVER = "ODBC Driver 18 for SQL Server"


class TagFreshness(TypedDict):
    tagname: str
    window_start: datetime
    agg_type: str
    value: float
    synced_at: datetime


class SqlServerClient:
    def __init__(self, config: SqlServerConfig):
        self._config = config

    def _connection_string(self) -> str:
        cfg = self._config
        return (
            f"DRIVER={{{_ODBC_DRIVER}}};"
            f"SERVER={cfg.host},{cfg.port};"
            f"DATABASE={cfg.database};"
            f"UID={cfg.user};"
            f"PWD={cfg.password};"
            f"Encrypt={cfg.encrypt};"
            f"TrustServerCertificate={cfg.trust_server_certificate};"
            f"Connection Timeout={cfg.connect_timeout};"
        )

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type(pyodbc.Error),
    )
    def _connect(self) -> pyodbc.Connection:
        return pyodbc.connect(self._connection_string(), timeout=self._config.connect_timeout)

    @contextmanager
    def _connection(self) -> Iterator[pyodbc.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def upsert_hourly_averages(self, rows: List[HourlyAvgRow], target_table: str) -> None:
        if not rows:
            return
        deduped = {(r.tagname, r.window_start): r for r in rows}
        if len(deduped) < len(rows):
            log.warning("Descartadas %d filas TWA duplicadas.", len(rows) - len(deduped))
        unique_rows = list(deduped.values())

        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE #stg (TagName VARCHAR(128) COLLATE DATABASE_DEFAULT NOT NULL, "
                "WindowStart DATETIME2(0) NOT NULL, AggType VARCHAR(4) COLLATE DATABASE_DEFAULT NOT NULL, Value FLOAT NULL)"
            )
            cursor.fast_executemany = True
            cursor.executemany(
                "INSERT INTO #stg VALUES (?, ?, ?, ?)",
                [(r.tagname, r.window_start.replace(tzinfo=None), "twa", r.avg_val) for r in unique_rows],
            )
            cursor.execute(f"""
                MERGE {target_table} AS target
                USING #stg AS src
                    ON target.TagName = src.TagName
                    AND target.WindowStart = src.WindowStart
                    AND target.AggType = src.AggType
                WHEN MATCHED THEN UPDATE SET Value = src.Value, SyncedAt = SYSUTCDATETIME()
                WHEN NOT MATCHED THEN INSERT (TagName, WindowStart, AggType, Value, SyncedAt)
                    VALUES (src.TagName, src.WindowStart, src.AggType, src.Value, SYSUTCDATETIME());
            """)
            conn.commit()
            cursor.execute("DROP TABLE #stg")
            conn.commit()
        log.info("Upsert TWA: %d filas en %s", len(unique_rows), target_table)

    def upsert_horometros(self, rows: List[HourlyLastRow], target_table: str) -> None:
        if not rows:
            return
        deduped = {(r.tagname, r.window_start): r for r in rows}
        if len(deduped) < len(rows):
            log.warning("Descartadas %d filas LAST duplicadas.", len(rows) - len(deduped))
        unique_rows = list(deduped.values())

        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE #stg (TagName VARCHAR(128) COLLATE DATABASE_DEFAULT NOT NULL, "
                "WindowStart DATETIME2(0) NOT NULL, AggType VARCHAR(4) COLLATE DATABASE_DEFAULT NOT NULL, Value FLOAT NULL)"
            )
            cursor.fast_executemany = True
            cursor.executemany(
                "INSERT INTO #stg VALUES (?, ?, ?, ?)",
                [(r.tagname, r.window_start.replace(tzinfo=None), "last", r.last_val) for r in unique_rows],
            )
            cursor.execute(f"""
                MERGE {target_table} AS target
                USING #stg AS src
                    ON target.TagName = src.TagName
                    AND target.WindowStart = src.WindowStart
                    AND target.AggType = src.AggType
                WHEN MATCHED THEN UPDATE SET Value = src.Value, SyncedAt = SYSUTCDATETIME()
                WHEN NOT MATCHED THEN INSERT (TagName, WindowStart, AggType, Value, SyncedAt)
                    VALUES (src.TagName, src.WindowStart, src.AggType, src.Value, SYSUTCDATETIME());
            """)
            conn.commit()
            cursor.execute("DROP TABLE #stg")
            conn.commit()
        log.info("Upsert LAST: %d filas en %s", len(unique_rows), target_table)

    def get_tag_freshness(self, tags: List[str], source_table: str, agg_type: str) -> List[TagFreshness]:
        if not tags:
            return []
        placeholders = ", ".join("?" for _ in tags)
        query = (
            f"SELECT t.TagName, t.WindowStart, t.AggType, t.Value, t.SyncedAt "
            f"FROM {source_table} AS t "
            f"INNER JOIN ("
            f"  SELECT TagName, MAX(WindowStart) AS MaxWindowStart "
            f"  FROM {source_table} WHERE TagName IN ({placeholders}) AND AggType = ? GROUP BY TagName"
            f") AS latest ON latest.TagName = t.TagName AND latest.MaxWindowStart = t.WindowStart"
            f" AND t.AggType = ?"
        )
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, [*tags, agg_type, agg_type])
            rows = cursor.fetchall()
        return [
            {"tagname": row.TagName, "window_start": row.WindowStart,
             "agg_type": row.AggType, "value": row.Value, "synced_at": row.SyncedAt}
            for row in rows
        ]
