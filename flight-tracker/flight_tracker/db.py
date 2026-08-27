"""SQLite storage for flight price observations.

The whole point of this file is persistence: a price is only interesting
relative to what it was yesterday, so the history database is the asset here,
not the script. Back it up / commit it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at   TEXT NOT NULL,          -- ISO-8601 UTC timestamp
    observed_date TEXT NOT NULL,          -- YYYY-MM-DD (local report date)
    trip_id       TEXT NOT NULL,
    query_id      TEXT NOT NULL,
    status        TEXT NOT NULL,          -- 'ok' | 'error' | 'no_results'
    price         REAL,
    currency      TEXT,
    airline       TEXT,
    flight_numbers TEXT,
    stops         INTEGER,
    duration_min  INTEGER,
    price_level   TEXT,                   -- Google's own 'low'/'typical'/'high'
    typical_low   REAL,
    typical_high  REAL,
    error         TEXT,
    raw           TEXT
);

CREATE INDEX IF NOT EXISTS idx_obs_query_time
    ON observations(query_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_trip
    ON observations(trip_id, observed_at);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    searches_used INTEGER NOT NULL DEFAULT 0,
    ok_count      INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    mock          INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------------- writes

    def record(
        self,
        *,
        trip_id: str,
        query_id: str,
        result: dict[str, Any],
        observed_at: Optional[str] = None,
        observed_date: Optional[str] = None,
    ) -> int:
        observed_at = observed_at or utcnow()
        observed_date = observed_date or observed_at[:10]
        cur = self.conn.execute(
            """
            INSERT INTO observations (
                observed_at, observed_date, trip_id, query_id, status,
                price, currency, airline, flight_numbers, stops, duration_min,
                price_level, typical_low, typical_high, error, raw
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                observed_at,
                observed_date,
                trip_id,
                query_id,
                result.get("status", "ok"),
                result.get("price"),
                result.get("currency"),
                result.get("airline"),
                result.get("flight_numbers"),
                result.get("stops"),
                result.get("duration_min"),
                result.get("price_level"),
                result.get("typical_low"),
                result.get("typical_high"),
                result.get("error"),
                json.dumps(result.get("raw")) if result.get("raw") else None,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def start_run(self, mock: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, mock) VALUES (?,?)", (utcnow(), int(mock))
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, searches: int, ok: int, fail: int, notes: str = "") -> None:
        self.conn.execute(
            """UPDATE runs SET finished_at=?, searches_used=?, ok_count=?,
               fail_count=?, notes=? WHERE id=?""",
            (utcnow(), searches, ok, fail, notes, run_id),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- reads

    def latest(self, query_id: str, *, offset: int = 0) -> Optional[sqlite3.Row]:
        """Most recent successful observation for a query (offset=1 -> the one before)."""
        cur = self.conn.execute(
            """SELECT * FROM observations
               WHERE query_id=? AND status='ok' AND price IS NOT NULL
               ORDER BY observed_at DESC LIMIT 1 OFFSET ?""",
            (query_id, offset),
        )
        return cur.fetchone()

    def history(self, query_id: str, limit: int = 90) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            """SELECT * FROM observations
               WHERE query_id=? AND status='ok' AND price IS NOT NULL
               ORDER BY observed_at DESC LIMIT ?""",
            (query_id, limit),
        )
        return list(reversed(cur.fetchall()))

    def stats(self, query_id: str) -> dict[str, Any]:
        cur = self.conn.execute(
            """SELECT COUNT(*) n, MIN(price) lo, MAX(price) hi, AVG(price) avg
               FROM observations
               WHERE query_id=? AND status='ok' AND price IS NOT NULL""",
            (query_id,),
        )
        row = cur.fetchone()
        out = {"n": row["n"], "low": row["lo"], "high": row["hi"], "avg": row["avg"]}
        if row["n"]:
            lo = self.conn.execute(
                """SELECT observed_date FROM observations
                   WHERE query_id=? AND status='ok' AND price=?
                   ORDER BY observed_at DESC LIMIT 1""",
                (query_id, row["lo"]),
            ).fetchone()
            hi = self.conn.execute(
                """SELECT observed_date FROM observations
                   WHERE query_id=? AND status='ok' AND price=?
                   ORDER BY observed_at DESC LIMIT 1""",
                (query_id, row["hi"]),
            ).fetchone()
            out["low_date"] = lo["observed_date"] if lo else None
            out["high_date"] = hi["observed_date"] if hi else None
        return out

    def searches_this_month(self, month: Optional[str] = None) -> int:
        month = month or utcnow()[:7]
        cur = self.conn.execute(
            """SELECT COALESCE(SUM(searches_used),0) n FROM runs
               WHERE mock=0 AND substr(started_at,1,7)=?""",
            (month,),
        )
        return int(cur.fetchone()["n"])

    def close(self) -> None:
        self.conn.close()
