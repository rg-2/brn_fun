"""Tests for the target/stop simulator."""

from __future__ import annotations

from brn_fun.backtest import (
    FILTERS,
    backtest_touches,
    simulate_trade,
    summarize_trades,
)
from brn_fun.db import Candle


def _bar(t: str, o: float, h: float, lo: float, c: float) -> Candle:
    """Explicit-OHLC candle for exit tests."""
    return Candle(
        instrument="X", granularity="M15", time=t,
        open=o, high=h, low=lo, close=c,
        volume=100, complete=True,
    )


# --- simulate_trade ---------------------------------------------------------

def test_long_hits_target() -> None:
    """A long trade whose target sits inside a future bar's high → target exit."""
    entry_price = 1.1000
    bars = [
        _bar("t0", 1.099, 1.100, 1.098, 1.1000),      # entry bar
        _bar("t1", 1.100, 1.104, 1.099, 1.103),       # future bar tags 1.104
    ]
    exit_idx, _time, exit_price, reason = simulate_trade(
        bars, entry_idx=0, direction="long", entry_price=entry_price,
        target_price=1.1030, stop_price=1.0970, max_bars=5,
    )
    assert reason == "target"
    assert exit_price == 1.1030
    assert exit_idx == 1


def test_short_hits_stop() -> None:
    """A short trade whose stop sits inside a future bar's high → stop exit."""
    bars = [
        _bar("t0", 1.1000, 1.1002, 1.0998, 1.1000),
        _bar("t1", 1.1000, 1.1050, 1.0995, 1.1040),    # tags 1.1030 stop
    ]
    exit_idx, _time, exit_price, reason = simulate_trade(
        bars, entry_idx=0, direction="short", entry_price=1.1000,
        target_price=1.0970, stop_price=1.1030, max_bars=5,
    )
    assert reason == "stop"
    assert exit_price == 1.1030
    assert exit_idx == 1


def test_ambiguous_bar_defaults_to_stop() -> None:
    """A single bar range spanning both target and stop → conservative stop exit."""
    bars = [
        _bar("t0", 1.1000, 1.1002, 1.0998, 1.1000),
        _bar("t1", 1.1000, 1.1050, 1.0950, 1.1020),    # covers both 1.1030 & 1.0970
    ]
    _idx, _time, exit_price, reason = simulate_trade(
        bars, entry_idx=0, direction="long", entry_price=1.1000,
        target_price=1.1030, stop_price=1.0970, max_bars=5,
    )
    assert reason == "stop"
    assert exit_price == 1.0970


def test_ambiguous_bar_best_case_takes_target() -> None:
    """path_ambiguity='best' → optimistic target exit when a bar spans both."""
    bars = [
        _bar("t0", 1.1000, 1.1002, 1.0998, 1.1000),
        _bar("t1", 1.1000, 1.1050, 1.0950, 1.1020),
    ]
    _idx, _time, exit_price, reason = simulate_trade(
        bars, entry_idx=0, direction="long", entry_price=1.1000,
        target_price=1.1030, stop_price=1.0970, max_bars=5,
        path_ambiguity="best",
    )
    assert reason == "target"
    assert exit_price == 1.1030


def test_timeout_closes_at_last_bar() -> None:
    """Neither target nor stop hits inside max_bars → close at last bar's close."""
    bars = [
        _bar("t0", 1.1000, 1.1000, 1.1000, 1.1000),
        _bar("t1", 1.1000, 1.1005, 1.0995, 1.1002),
        _bar("t2", 1.1002, 1.1006, 1.0998, 1.1005),
        _bar("t3", 1.1005, 1.1008, 1.1003, 1.1006),   # never reaches ±30p
    ]
    exit_idx, _time, exit_price, reason = simulate_trade(
        bars, entry_idx=0, direction="long", entry_price=1.1000,
        target_price=1.1030, stop_price=1.0970, max_bars=3,
    )
    assert reason == "timeout"
    # Last bar of the window (max_bars=3 from entry_idx=0) is index 3.
    assert exit_idx == 3
    assert exit_price == 1.1006


# --- backtest_touches -------------------------------------------------------

def test_backtest_end_to_end_long_win() -> None:
    """Wire analyze() into backtest_touches with a manufactured event."""
    from brn_fun.analyze import analyze

    # A down-touch of 1.10 (approaching from above), then price bounces up
    # 40+ pips (long trade, target=30p hit).
    bars = [
        _bar("2024-01-02T00:00:00.000000000Z", 1.105, 1.107, 1.104, 1.106),  # prior
        # Touch bar: dips into 1.10, closes bullish above it → confirmation.
        _bar("2024-01-02T00:15:00.000000000Z", 1.1030, 1.1035, 1.0999, 1.1010),
        # Confirmation bar: closes further away from 1.10 (up).
        _bar("2024-01-02T00:30:00.000000000Z", 1.1010, 1.1030, 1.1010, 1.1025),
        # Forward bars: price runs up.
        _bar("2024-01-02T00:45:00.000000000Z", 1.1025, 1.1055, 1.1020, 1.1050),
        _bar("2024-01-02T01:00:00.000000000Z", 1.1050, 1.1070, 1.1045, 1.1065),
    ]

    events = list(analyze(bars, grid=0.01, cooldown_bars=100, forward_bars=5))
    trades = backtest_touches(
        bars, events,
        filter_name="wick",       # relax to wick_only (still qualifies)
        entry="confirm",
        target_pips=30, stop_pips=30, pip=0.0001,
        max_bars=10,
    )
    assert len(trades) == 1
    t = trades[0]
    assert t.direction == "long"
    # Entry at close of confirm bar (index 2) = 1.1025.
    assert t.entry_price == 1.1025
    # Target at 1.1025 + 30p = 1.1055; bar 3's high = 1.1055 → target hit there.
    assert t.exit_reason == "target"
    assert t.pnl_price > 0


def test_filter_registry_covers_expected_names() -> None:
    """Sanity: expected named filters are present."""
    for name in ("all", "wick", "wick+drift", "wick+drift+away"):
        assert name in FILTERS


def test_summarize_empty_trades() -> None:
    s = summarize_trades([], pip=0.0001)
    assert s["n"] == 0
    assert s["expectancy_pips"] == 0.0
    assert s["max_drawdown_pips"] == 0.0
