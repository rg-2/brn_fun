"""Tests for round-number touch detection and outcome tagging."""

from __future__ import annotations

from brn_fun.analyze import (
    _round_levels_in,
    analyze,
    characterize_touch,
    compute_context,
    find_first_touches,
)
from brn_fun.db import Candle


def _c(
    t: str, low: float, high: float, close: float | None = None, open_: float | None = None
) -> Candle:
    """Terse candle builder for fixtures."""
    return Candle(
        instrument="EUR_USD",
        granularity="M15",
        time=t,
        open=open_ if open_ is not None else (low + high) / 2,
        high=high,
        low=low,
        close=close if close is not None else (low + high) / 2,
        volume=100,
        complete=True,
    )


# --- _round_levels_in --------------------------------------------------------

def test_round_levels_basic() -> None:
    # 0.01 grid across a range that straddles three levels.
    assert _round_levels_in(1.095, 1.115, 0.01) == [1.1, 1.11]


def test_round_levels_endpoints_inclusive() -> None:
    # Range endpoints landing exactly on levels should be included.
    assert _round_levels_in(1.10, 1.12, 0.01) == [1.1, 1.11, 1.12]


def test_round_levels_no_match() -> None:
    # Tight range strictly between two levels returns empty.
    assert _round_levels_in(1.1010, 1.1090, 0.01) == []


def test_round_levels_handle_tier() -> None:
    # 0.10 grid: only 1.10 sits inside 1.05–1.15.
    assert _round_levels_in(1.05, 1.15, 0.10) == [1.1]


# --- find_first_touches ------------------------------------------------------

def test_first_touch_direction_from_below() -> None:
    """Prior close below the level → direction 'up'."""
    bars = [
        _c("t0", 1.09, 1.095, close=1.094),        # prior close = 1.094
        _c("t1", 1.099, 1.101, close=1.1005),      # crosses 1.10 from below
    ]
    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=100))
    assert len(touches) == 1
    assert touches[0].idx == 1
    assert touches[0].level == 1.1
    assert touches[0].direction == "up"


def test_first_touch_direction_from_above() -> None:
    """Prior close above the level → direction 'down'."""
    bars = [
        _c("t0", 1.105, 1.11, close=1.108),        # prior close above 1.10
        _c("t1", 1.099, 1.101, close=1.0995),      # crosses 1.10 from above
    ]
    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=100))
    assert touches[0].direction == "down"


def test_cooldown_suppresses_re_touch() -> None:
    """A second touch within cooldown_bars must NOT fire; after cooldown it should."""
    # Build 5 bars: touch at 0, hover away, touch again at 2 (inside cooldown=5),
    # then a much later touch at 10 (outside cooldown).
    bars = [
        _c("t0", 1.099, 1.101, close=1.100),   # first touch of 1.10
        _c("t1", 1.098, 1.099, close=1.0985),
        _c("t2", 1.099, 1.101, close=1.100),   # re-touch inside cooldown
        _c("t3", 1.095, 1.098, close=1.096),
        _c("t4", 1.094, 1.097, close=1.095),
    ]
    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=5))
    # First-ever touch always fires (i=0 is skipped by the `i > 0` guard, so
    # index 0 doesn't emit; we get exactly one from the setup).
    # Bar 2 is at cooldown distance 2, so must be suppressed.
    assert [t.idx for t in touches] == []  # bar 0 has no prior close to infer direction

    # Now push the sequence so we have a well-defined prior close before the touch.
    bars2 = [_c("pre", 1.09, 1.095, close=1.094)] + bars
    touches2 = list(find_first_touches(bars2, grid=0.01, cooldown_bars=5))
    # After shifting: touch at index 1 fires; re-touch at index 3 (dist=2) suppressed.
    assert [t.idx for t in touches2] == [1]


def test_cooldown_allows_after_expiry() -> None:
    """After cooldown_bars have passed, another touch of the same level fires."""
    bars = [_c("pre", 1.09, 1.095, close=1.094)]
    # First touch
    bars.append(_c("t1", 1.099, 1.101, close=1.100))
    # Fill with untouched bars: range 1.052–1.058 doesn't hit any 0.01 level.
    for i in range(6):
        bars.append(_c(f"f{i}", 1.052, 1.058, close=1.055))
    # Cooldown 5 expired; another touch should fire.
    bars.append(_c("t_late", 1.099, 1.101, close=1.100))

    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=5))
    assert len(touches) == 2
    assert touches[0].idx == 1
    assert touches[1].idx == 8


def test_multiple_levels_in_one_bar() -> None:
    """A wide bar can touch several levels; each emits a separate event."""
    bars = [
        _c("pre", 1.08, 1.085, close=1.084),
        _c("wide", 1.099, 1.121, close=1.115),   # straddles 1.10, 1.11, 1.12
    ]
    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=100))
    assert sorted(t.level for t in touches) == [1.1, 1.11, 1.12]


# --- characterize_touch ------------------------------------------------------

def test_bounce_when_from_below() -> None:
    """Upward approach, then a clean pullback → 'bounce'."""
    bars = [
        _c("pre", 1.09, 1.095, close=1.094),
        _c("touch", 1.099, 1.101, close=1.100),  # touch 1.10 from below
        _c("f1", 1.095, 1.100, close=1.097),
        _c("f2", 1.093, 1.098, close=1.094),     # low 1.093 → favorable = 1.10 - 1.093 = 0.007 = 70 pips
    ]
    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=100))
    assert len(touches) == 1
    outcome = characterize_touch(
        bars, touches[0],
        forward_bars=10, bounce_pips=30, break_pips=30, pip=0.0001,
    )
    assert outcome.tag == "bounce"
    assert outcome.favorable > 0.005          # 50+ pips of pullback
    assert outcome.adverse < 0.0002           # negligible break-through


def test_break_when_from_below() -> None:
    """Upward approach, then continuation → 'break'."""
    bars = [
        _c("pre", 1.09, 1.095, close=1.094),
        _c("touch", 1.099, 1.101, close=1.100),
        _c("f1", 1.100, 1.108, close=1.107),     # keeps going up
    ]
    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=100))
    outcome = characterize_touch(
        bars, touches[0],
        forward_bars=10, bounce_pips=30, break_pips=30, pip=0.0001,
    )
    assert outcome.tag == "break"
    assert outcome.adverse >= 0.0030


def test_chop_when_flat() -> None:
    """Small moves both ways → 'chop'."""
    bars = [
        _c("pre", 1.09, 1.095, close=1.094),
        _c("touch", 1.099, 1.101, close=1.100),
        _c("f1", 1.0995, 1.1005, close=1.100),
        _c("f2", 1.0998, 1.1002, close=1.100),
    ]
    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=100))
    outcome = characterize_touch(
        bars, touches[0],
        forward_bars=10, bounce_pips=30, break_pips=30, pip=0.0001,
    )
    assert outcome.tag == "chop"


def test_analyze_iterates_triples() -> None:
    """The convenience `analyze` iterator returns (touch, context, outcome)."""
    bars = [
        _c("2024-01-02T09:00:00.000000000Z", 1.09, 1.095, close=1.094),
        _c("2024-01-02T09:15:00.000000000Z", 1.099, 1.101, close=1.100, open_=1.0995),
        _c("2024-01-02T09:30:00.000000000Z", 1.093, 1.099, close=1.094),
    ]
    triples = list(analyze(bars, grid=0.01, cooldown_bars=100, forward_bars=5))
    assert len(triples) == 1
    t, c, o = triples[0]
    assert t.level == 1.10
    assert o.tag == "bounce"
    # Context has been populated with real values.
    assert c.hour_utc == 9
    assert c.dow == 1  # Tuesday
    assert c.atr > 0


# --- compute_context ---------------------------------------------------------

def test_context_atr_from_bar_ranges() -> None:
    """ATR is roughly the average of bar high-low over the lookback."""
    # Keep the priming bars strictly between 1.10 and 1.11 so nothing else
    # gets registered as a first-touch (which would suppress our real touch
    # via the cooldown map).
    bars = [
        _c("2024-01-02T00:00:00.000000000Z", 1.101, 1.104, close=1.102),
        _c("2024-01-02T00:15:00.000000000Z", 1.101, 1.106, close=1.105),
        _c("2024-01-02T00:30:00.000000000Z", 1.103, 1.108, close=1.106),
        _c("2024-01-02T00:45:00.000000000Z", 1.099, 1.101, close=1.100),  # touch bar
    ]
    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=100))
    assert len(touches) == 1
    ctx = compute_context(bars, touches[0], atr_period=3, approach_bars=3)
    # Roughly 50 pips of range on average. Not asserting exact — depends on
    # true-range vs simple-range with gaps — just want positive, sane order.
    assert 0.003 < ctx.atr < 0.008


def test_context_wick_only_vs_body() -> None:
    """Wick-only when level sits outside the bar's [open, close] body."""
    bars = [
        _c("2024-01-02T00:00:00.000000000Z", 1.09, 1.095, close=1.094),
        # Body 1.095-1.099, wick up to 1.101 → level 1.10 is above body.
        _c("2024-01-02T00:15:00.000000000Z", 1.094, 1.101, open_=1.095, close=1.099),
    ]
    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=100))
    ctx = compute_context(bars, touches[0])
    assert ctx.wick_only is True

    # Now a body-touch: body straddles the level.
    bars2 = [
        _c("2024-01-02T00:00:00.000000000Z", 1.09, 1.095, close=1.094),
        _c("2024-01-02T00:15:00.000000000Z", 1.098, 1.102, open_=1.099, close=1.101),
    ]
    touches2 = list(find_first_touches(bars2, grid=0.01, cooldown_bars=100))
    ctx2 = compute_context(bars2, touches2[0])
    assert ctx2.wick_only is False


def test_context_approach_change_signed() -> None:
    """approach_change is signed close-of-touch minus close-N-back."""
    # Priming bars stay strictly inside (1.09, 1.10) so no premature touches.
    bars = [
        _c("2024-01-02T00:00:00.000000000Z", 1.091, 1.093, close=1.092),
        _c("2024-01-02T00:15:00.000000000Z", 1.093, 1.096, close=1.095),
        _c("2024-01-02T00:30:00.000000000Z", 1.096, 1.099, close=1.098),
        _c("2024-01-02T00:45:00.000000000Z", 1.099, 1.101, close=1.100),  # touch
    ]
    touches = list(find_first_touches(bars, grid=0.01, cooldown_bars=100))
    assert len(touches) == 1 and touches[0].level == 1.10
    ctx = compute_context(bars, touches[0], approach_bars=3)
    # Close moved from 1.092 → 1.100 over the 3-bar approach window: +80 pips.
    assert ctx.approach_change > 0
    assert abs(ctx.approach_change - 0.008) < 1e-6
