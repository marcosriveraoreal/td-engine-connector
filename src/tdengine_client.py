"""Cliente REST para TDengine: TWA horario y LAST horario por tag."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests
from dateutil import parser as dateutil_parser
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import TDengineConfig

log = logging.getLogger(__name__)


class TDengineError(RuntimeError):
    pass


@dataclass(frozen=True)
class HourlyAvgRow:
    tagname: str
    window_start: datetime
    avg_val: Optional[float]


@dataclass(frozen=True)
class HourlyLastRow:
    tagname: str
    window_start: datetime
    last_val: Optional[float]


class TDengineClient:
    def __init__(self, config: TDengineConfig):
        self._config = config
        self._session = requests.Session()
        self._session.auth = (config.user, config.password)

    def close(self) -> None:
        self._session.close()

    def _ts(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")

    def _tag_filter(self, tags: List[str]) -> str:
        return "AND tagname IN (" + ", ".join(f"'{t}'" for t in tags) + ") " if tags else ""

    def _post(self, sql: str) -> dict:
        cfg = self._config
        response = self._session.post(cfg.url, data=sql.encode("utf-8"), timeout=cfg.request_timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise TDengineError(f"TDengine error: {payload}")
        return payload

    def _parse_rows(self, payload: dict, value_col: str) -> List[tuple]:
        columns = [col[0] for col in payload.get("column_meta", [])]
        try:
            idx_tag = columns.index("tagname")
            idx_wstart = columns.index("_wstart")
            idx_val = columns.index(value_col)
        except ValueError as exc:
            raise TDengineError(f"Columnas inesperadas: {columns}") from exc

        result = []
        for raw_row in payload.get("data", []):
            ws = raw_row[idx_wstart]
            window_start = ws if isinstance(ws, datetime) else dateutil_parser.parse(str(ws))
            if window_start.tzinfo is None:
                window_start = window_start.replace(tzinfo=timezone.utc)
            result.append((raw_row[idx_tag].strip(), window_start, raw_row[idx_val]))
        return result

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type((requests.RequestException, TDengineError)),
    )
    def fetch_hourly_averages(self, start: datetime, end: datetime, tags: Optional[List[str]] = None) -> List[HourlyAvgRow]:
        cfg = self._config
        tag_filter = self._tag_filter(tags or [])
        sql = (
            f"SELECT _wstart, TWA(`val`) AS avg_val, tagname "
            f"FROM `{cfg.database}`.`{cfg.table}` "
            f"WHERE ts >= '{self._ts(start)}' AND ts <= '{self._ts(end)}' "
            f"{tag_filter}"
            f"PARTITION BY tbname INTERVAL(1h) ORDER BY _wstart"
        )
        log.info("Consultando TDengine (TWA): %s", sql)
        payload = self._post(sql)
        rows = [HourlyAvgRow(tagname=t, window_start=ws, avg_val=v) for t, ws, v in self._parse_rows(payload, "avg_val")]
        log.info("TDengine devolvió %d filas de TWA horario", len(rows))
        return rows

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type((requests.RequestException, TDengineError)),
    )
    def fetch_hourly_last(
        self,
        start: datetime,
        end: datetime,
        tags: Optional[List[str]] = None,
        tbname_pattern: Optional[str] = None,
    ) -> List[HourlyLastRow]:
        cfg = self._config
        tag_filter = self._tag_filter(tags or [])
        tbname_filter = f"AND tbname LIKE '{tbname_pattern}' " if tbname_pattern else ""
        sql = (
            f"SELECT _wstart, LAST(`val`) AS last_val, tagname "
            f"FROM `{cfg.database}`.`{cfg.table}` "
            f"WHERE ts >= '{self._ts(start)}' AND ts <= '{self._ts(end)}' "
            f"{tag_filter}{tbname_filter}"
            f"PARTITION BY tbname INTERVAL(1h) ORDER BY _wstart"
        )
        log.info("Consultando TDengine (LAST): %s", sql)
        payload = self._post(sql)
        rows = [HourlyLastRow(tagname=t, window_start=ws, last_val=v) for t, ws, v in self._parse_rows(payload, "last_val")]
        log.info("TDengine devolvió %d filas de LAST horario", len(rows))
        return rows
