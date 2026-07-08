"""SQLite storage for candles.

One table, ``candles``, keyed by (instrument, granularity, time). Time is
stored as an ISO-8601 UTC string — Oanda returns RFC3339, we normalize to that
same form on the way in, so lexicographic order == chronological order and
range queries stay fast on the primary key.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    instrument   TEXT    NOT NULL,
    granularity  TEXT    NOT NULL,
    time         TEXT    NOT NULL,   -- RFC3339 UTC, e.g. 2024-01-02T15:30:00Z
    open         REAL    NOT NULL,
    high         REAL    NOT NULL,
    low          REAL    NOT NULL,
    close        REAL    NOT NULL,
    volume       INTEGER NOT NULL,
    complete     INTEGER NOT NULL,   -- 1 if the bar is closed, 0 if forming
    PRIMARY KEY (instrument, granularity, time)
);

CREATE INDEX IF NOT EXISTS idx_candles_time
    ON candles(instrument, granularity, time);
"""


@dataclass(frozen=True, slots=True)
class Candle:
    instrument: str
    granularity: str
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    complete: bool


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a connection, ensure the parent dir + schema exist."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        yield conn
    finally:
        conn.close()


def upsert_candles(conn: sqlite3.Connection, candles: Iterable[Candle]) -> int:
    """Insert or replace bars. Returns the count written."""
    rows = [
        (
            c.instrument, c.granularity, c.time,
            c.open, c.high, c.low, c.close, c.volume, int(c.complete),
        )
        for c in candles
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO candles
            (instrument, granularity, time, open, high, low, close, volume, complete)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(instrument, granularity, time) DO UPDATE SET
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            volume=excluded.volume,
            complete=excluded.complete
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def latest_time(
    conn: sqlite3.Connection, instrument: str, granularity: str
) -> str | None:
    """Return the max stored bar time for (instrument, granularity), or None."""
    cur = conn.execute(
        """
        SELECT MAX(time) FROM candles
        WHERE instrument = ? AND granularity = ?
        """,
        (instrument, granularity),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def count_candles(
    conn: sqlite3.Connection, instrument: str, granularity: str
) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE instrument=? AND granularity=?",
        (instrument, granularity),
    )
    return int(cur.fetchone()[0])
