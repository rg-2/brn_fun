"""Non-round-number level signals: previous trading day / week high & low.

Emits the same ``(Touch, Context, Confirmation, Outcome_)`` event tuples
as ``analyze.analyze`` so the backtester consumes them identically.

A trading day boundary is **22:00 UTC** (forex convention). A bar at
21:59:00 UTC belongs to today's trading day; a bar at 22:00:00 UTC belongs
to tomorrow's. Weekends inherit the same rule — Friday 22:00 UTC to Sunday
22:00 UTC is essentially empty for majors, so those days just produce no
new levels.

Level lifecycle (for prev-day-high, symmetric for low):

1. At start of trading day D, record the just-ended day (D-1)'s high H.
2. ``level_h = H`` starts unarmed.
3. **Armed** once price has stayed at least ``arm_atr_mult × ATR`` *below*
   the level for at least ``arm_bars`` consecutive bars.
4. **Fires** on the first subsequent bar whose ``high >= level_h``.
5. Level expires at the next day's 22:00 UTC (whether it fired or not).

Rationale for the arming filter: if the day opens sitting right on the
prev-day-high, we don't want to trade the immediate "touch" — that's not
a bounce, it's just noise around the level. We want price to leave the
level, spend some time away, then come back and test it. That's the
classic bounce setup.

Prev-week H/L use the same logic with 22:00 UTC Sunday as the week
boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterator, Literal, Sequence

from .analyze import (
    Confirmation,
    Context,
    Outcome_,
    Touch,
    characterize_touch,
    compute_confirmation,
    compute_context,
)
from .db import Candle

LevelType = Literal["prev_day_high", "prev_day_low",
                    "prev_week_high", "prev_week_low"]


def _parse_utc(s: str) -> datetime:
    """Parse Oanda RFC3339 without the nanosecond overflow."""
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def _trading_day(bar_time: str) -> str:
    """Return the forex trading-day date for a bar (22:00 UTC boundary).

    Bars at 22:00 UTC or later shift to the *next* calendar day's trading
    day. Result is a YYYY-MM-DD string for cheap comparison.
    """
    hour = int(bar_time[11:13])
    if hour >= 22:
        return (_parse_utc(bar_time) + timedelta(days=1)).strftime("%Y-%m-%d")
    return bar_time[:10]


def _trading_week(bar_time: str) -> str:
    """Return a week key (year-week) using the same 22:00 UTC shift."""
    dt = _parse_utc(bar_time)
    if dt.hour >= 22:
        dt = dt + timedelta(days=1)
    # ISO week: Monday-Sunday. Forex week Sun-Fri roughly matches; using
    # ISO week is close enough and stable across the years.
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _rolling_atr(bars: Sequence[Candle], period: int) -> list[float]:
    """One ATR estimate per bar over the trailing ``period`` bars.

    Uses the standard Wilder-style mean of True Range. For indices with
    less than ``period`` bars of history, falls back to whatever's
    available (never NaN).
    """
    n = len(bars)
    out = [0.0] * n
    if n == 0:
        return out
    out[0] = bars[0].high - bars[0].low
    tr_window: list[float] = [out[0]]
    for i in range(1, n):
        prev_close = bars[i - 1].close
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - prev_close),
            abs(bars[i].low - prev_close),
        )
        tr_window.append(tr)
        if len(tr_window) > period:
            tr_window.pop(0)
        out[i] = sum(tr_window) / len(tr_window)
    return out


def _period_extremes(
    bars: Sequence[Candle], period_key_fn,
) -> tuple[dict[str, float], dict[str, float]]:
    """Return (period_high, period_low) dicts keyed by period label."""
    highs: dict[str, float] = {}
    lows: dict[str, float] = {}
    for bar in bars:
        k = period_key_fn(bar.time)
        if k not in highs or bar.high > highs[k]:
            highs[k] = bar.high
        if k not in lows or bar.low < lows[k]:
            lows[k] = bar.low
    return highs, lows


def find_prev_level_touches(
    bars: Sequence[Candle],
    *,
    period: Literal["day", "week"] = "day",
    arm_atr_mult: float = 2.0,
    arm_bars: int = 60,
    atr_period: int = 210,
    pip: float = 0.0001,
) -> Iterator[tuple[Touch, LevelType]]:
    """Yield (Touch, level_type) pairs for prev-period high & low touches.

    ``period`` = ``"day"`` → previous trading day's H/L.
    ``period`` = ``"week"`` → previous ISO week's H/L.
    """
    if not bars:
        return

    key_fn = _trading_day if period == "day" else _trading_week
    highs, lows = _period_extremes(bars, key_fn)
    atr_series = _rolling_atr(bars, atr_period)

    # Sorted period order — first appearance in the bar stream.
    ordered_periods: list[str] = []
    seen: set[str] = set()
    for bar in bars:
        k = key_fn(bar.time)
        if k not in seen:
            seen.add(k)
            ordered_periods.append(k)

    # Map each period → its predecessor (or None for first).
    prev_of: dict[str, str | None] = {}
    for i, p in enumerate(ordered_periods):
        prev_of[p] = ordered_periods[i - 1] if i > 0 else None

    # State that resets when a new period starts.
    current_period: str | None = None
    level_h: float | None = None
    level_l: float | None = None
    h_arm_counter = 0
    l_arm_counter = 0
    h_armed = False
    l_armed = False
    h_fired = False
    l_fired = False

    for i, bar in enumerate(bars):
        period_key = key_fn(bar.time)

        if period_key != current_period:
            # New period — activate prev period's H/L as fresh levels.
            prev = prev_of[period_key]
            if prev is None:
                level_h = level_l = None
            else:
                level_h = highs.get(prev)
                level_l = lows.get(prev)
            h_arm_counter = l_arm_counter = 0
            h_armed = l_armed = False
            h_fired = l_fired = False
            current_period = period_key

        atr = atr_series[i] if atr_series[i] > 0 else pip  # avoid div-by-zero
        arm_dist = arm_atr_mult * atr

        # --- level_h (previous period high, sits above current price) ---
        if level_h is not None and not h_fired:
            # "Away" = closed strictly below the level by at least arm_dist.
            if bar.close <= level_h - arm_dist:
                h_arm_counter += 1
                if h_arm_counter >= arm_bars:
                    h_armed = True
            else:
                # Reset if we drift within the arm-distance envelope.
                h_arm_counter = 0

            if h_armed and bar.high >= level_h:
                # Touch fires from below (up-approach → SHORT bet on rejection).
                yield Touch(
                    idx=i, time=bar.time, level=level_h,
                    direction="up",
                    cooldown_bars=None,  # not applicable to daily levels
                ), ("prev_week_high" if period == "week" else "prev_day_high")
                h_fired = True  # one bite per period

        # --- level_l (previous period low, sits below current price) ---
        if level_l is not None and not l_fired:
            if bar.close >= level_l + arm_dist:
                l_arm_counter += 1
                if l_arm_counter >= arm_bars:
                    l_armed = True
            else:
                l_arm_counter = 0

            if l_armed and bar.low <= level_l:
                yield Touch(
                    idx=i, time=bar.time, level=level_l,
                    direction="down",
                    cooldown_bars=None,
                ), ("prev_week_low" if period == "week" else "prev_day_low")
                l_fired = True


def analyze_prev_levels(
    bars: Sequence[Candle],
    *,
    period: Literal["day", "week"] = "day",
    arm_atr_mult: float = 2.0,
    arm_bars: int = 60,
    forward_bars: int = 1440,
    bounce_pips: float = 30.0,
    break_pips: float = 30.0,
    pip: float = 0.0001,
    atr_period: int = 210,
    approach_bars: int = 300,
    sma_period: int = 28800,
    slope_lookback: int = 7200,
    trend_flat_pips: float = 50.0,
) -> Iterator[tuple[Touch, Context, Confirmation, Outcome_, LevelType]]:
    """Full event tuples for prev-period H/L touches, matching analyze.analyze."""
    # Precompute the SMA once — matches analyze() for consistency.
    from .analyze import _rolling_mean_close  # local, avoids export

    sma_series = _rolling_mean_close(bars, sma_period)

    for touch, level_type in find_prev_level_touches(
        bars, period=period, arm_atr_mult=arm_atr_mult, arm_bars=arm_bars,
        atr_period=atr_period, pip=pip,
    ):
        context = compute_context(
            bars, touch,
            atr_period=atr_period, approach_bars=approach_bars,
            sma_period=sma_period, slope_lookback=slope_lookback,
            trend_flat_pips=trend_flat_pips, pip=pip,
            sma_series=sma_series,
        )
        confirmation = compute_confirmation(bars, touch)
        outcome = characterize_touch(
            bars, touch, forward_bars=forward_bars,
            bounce_pips=bounce_pips, break_pips=break_pips, pip=pip,
        )
        yield touch, context, confirmation, outcome, level_type
