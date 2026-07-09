"""Tests for the paper-mode live signal detector's pure logic.

Only tests the deterministic bits (level-in-range detection, cooldown
math, state file round-trip). The Oanda-facing polling loop is not
exercised here — that would need network mocking. The heavy lifting
inside the trader (limit-fill simulation, target/stop resolution) is
already covered by the backtester's tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from brn_fun.live.paper import (
    OpenTrade,
    PaperState,
    PendingSignal,
    _cooldown_bars_between,
    _round_levels_in,
)


def test_round_levels_within_range() -> None:
    """0.01 grid inside a range that straddles 0.69 → returns [0.69]."""
    assert _round_levels_in(0.6890, 0.6912, 0.01) == [0.69]
    # Range from 0.69 to 0.70 inclusive: both levels present.
    assert _round_levels_in(0.6900, 0.7000, 0.01) == [0.69, 0.7]
    # Range strictly between two levels returns empty.
    assert _round_levels_in(0.6901, 0.6999, 0.01) == []
    # Wide range covering multiple levels.
    assert _round_levels_in(0.6801, 0.7099, 0.01) == [0.69, 0.7]


def test_cooldown_bars_between_minute_gap() -> None:
    """Two RFC3339 timestamps 90 minutes apart yield 90 bars at M1."""
    assert _cooldown_bars_between(
        "2026-07-01T12:00:00.000000000Z",
        "2026-07-01T13:30:00.000000000Z",
    ) == 90


def test_cooldown_bars_between_across_day_boundary() -> None:
    assert _cooldown_bars_between(
        "2026-07-01T23:45:00.000000000Z",
        "2026-07-02T00:15:00.000000000Z",
    ) == 30


def test_state_roundtrip(tmp_path: Path) -> None:
    """PaperState survives a save + load with all fields intact."""
    s = PaperState(
        strategy_name="audusd",
        instrument="AUD_USD",
        granularity="M1",
        last_processed_time="2026-07-08T22:00:00.000000000Z",
        last_touch={"0.69000": "2026-07-05T16:56:00.000000000Z"},
        pending=[PendingSignal(
            signal_time="2026-07-06T09:00:00.000000000Z",
            signal_idx=42,
            level=0.69,
            direction="up",
        ).__dict__],
        open_trades=[OpenTrade(
            entry_time="2026-07-06T09:15:00.000000000Z",
            entry_idx=57,
            entry_price=0.68978,
            direction="long",
            level=0.69,
            target_price=0.69578,
            stop_price=0.68678,
            exit_by_idx=1497,
        ).__dict__],
    )
    path = tmp_path / "state.json"
    s.save(path)
    loaded = PaperState.load_or_new(path, "audusd", "AUD_USD", "M1")
    assert loaded.last_processed_time == s.last_processed_time
    assert loaded.last_touch == s.last_touch
    assert loaded.pending == s.pending
    assert loaded.open_trades == s.open_trades


def test_state_load_wrong_strategy_raises(tmp_path: Path) -> None:
    """Loading a state file written for a different strategy is a clear error."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "strategy_name": "somethingelse",
        "instrument": "EUR_USD",
        "granularity": "M1",
    }))
    import pytest
    with pytest.raises(ValueError, match="written for strategy"):
        PaperState.load_or_new(p, "audusd", "AUD_USD", "M1")


def test_state_load_missing_file_returns_fresh(tmp_path: Path) -> None:
    p = tmp_path / "does_not_exist.json"
    s = PaperState.load_or_new(p, "audusd", "AUD_USD", "M1")
    assert s.strategy_name == "audusd"
    assert s.last_processed_time == ""
    assert s.last_touch == {}
    assert s.pending == []
    assert s.open_trades == []
