"""Round-trip test for the SQLite layer."""

from __future__ import annotations

from pathlib import Path

from brn_fun.db import Candle, connect, count_candles, latest_time, upsert_candles


def _candle(t: str, close: float = 1.10) -> Candle:
    return Candle(
        instrument="EUR_USD",
        granularity="M15",
        time=t,
        open=close - 0.001,
        high=close + 0.002,
        low=close - 0.002,
        close=close,
        volume=100,
        complete=True,
    )


def test_upsert_and_query(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite"

    with connect(db) as conn:
        # Two bars in, then re-write one with a different close: upsert should
        # overwrite, not duplicate.
        n = upsert_candles(
            conn,
            [
                _candle("2024-01-02T15:00:00.000000000Z", close=1.10),
                _candle("2024-01-02T15:15:00.000000000Z", close=1.11),
            ],
        )
        assert n == 2

        n = upsert_candles(
            conn, [_candle("2024-01-02T15:15:00.000000000Z", close=1.12)]
        )
        assert n == 1

        assert count_candles(conn, "EUR_USD", "M15") == 2
        assert latest_time(conn, "EUR_USD", "M15") == "2024-01-02T15:15:00.000000000Z"


def test_latest_time_none_when_empty(tmp_path: Path) -> None:
    with connect(tmp_path / "t.sqlite") as conn:
        assert latest_time(conn, "EUR_USD", "M15") is None
        assert count_candles(conn, "EUR_USD", "M15") == 0
