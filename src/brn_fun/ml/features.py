"""Feature extraction for supervised trade-outcome prediction.

For each (touch, context, confirmation) event that produced a Trade in
the backtest, we extract a fixed-length feature vector combining:

  1. Everything already computed in Context and Confirmation.
  2. Cyclical encoding of hour and day-of-week.
  3. Aggregated summary of the ``pre_bars`` M1 bars *before* the touch
     (recent-momentum features).
  4. Aggregated summary of the ``post_bars`` M1 bars *after* the touch
     (early-reaction features — corresponds to the user's "5 or 10 M1
     bars after the touch" idea).
  5. Level and direction as categorical variables.

Features are returned as a plain dict per event so downstream code can
DataFrame-ify them however it wants without this module needing pandas.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

from ..analyze import Confirmation, Context, Touch
from ..backtest import Trade
from ..db import Candle


# All possible categorical values so one-hot columns are the same
# regardless of which subset of events is passed in.
_SHAPES = ("doji", "hammer", "shooting_star", "bullish", "bearish", "neutral")
_ALIGNMENTS = ("with", "against", "flat")
_DIRECTIONS = ("up", "down")


def _cyclical(x: float, period: float) -> tuple[float, float]:
    """Encode a bounded numeric as (sin, cos) so 0 and period-1 are neighbors."""
    angle = 2.0 * math.pi * x / period
    return math.sin(angle), math.cos(angle)


def _bar_features(bars: Sequence[Candle], pip: float, prefix: str) -> dict:
    """Aggregate a slice of bars into a small fixed feature set.

    ``pip`` scales absolute price into pips so the numbers are comparable
    to the pip-denominated Context features and to what a human eyeballs.
    """
    n = len(bars)
    if n == 0:
        return {
            f"{prefix}_n": 0,
            f"{prefix}_cum_return_pips": 0.0,
            f"{prefix}_range_pips": 0.0,
            f"{prefix}_realized_vol_pips": 0.0,
            f"{prefix}_first_half_return_pips": 0.0,
            f"{prefix}_second_half_return_pips": 0.0,
            f"{prefix}_max_up_move_pips": 0.0,
            f"{prefix}_max_down_move_pips": 0.0,
            f"{prefix}_volume_mean": 0.0,
        }

    first_close = bars[0].close
    last_close = bars[-1].close
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]

    # Cumulative close-to-close, and the two halves so momentum can flip.
    mid = max(1, n // 2)
    first_half_ret = (closes[mid - 1] - first_close) / pip
    second_half_ret = (last_close - closes[mid - 1]) / pip

    # Realized-vol proxy: mean |bar return| (open->close), scaled to pips.
    returns_pips = [(b.close - b.open) / pip for b in bars]
    realized_vol = sum(abs(r) for r in returns_pips) / n

    # Maximum excursion in each direction from the first close.
    max_up = (max(highs) - first_close) / pip
    max_down = (first_close - min(lows)) / pip

    return {
        f"{prefix}_n": n,
        f"{prefix}_cum_return_pips": (last_close - first_close) / pip,
        f"{prefix}_range_pips": (max(highs) - min(lows)) / pip,
        f"{prefix}_realized_vol_pips": realized_vol,
        f"{prefix}_first_half_return_pips": first_half_ret,
        f"{prefix}_second_half_return_pips": second_half_ret,
        f"{prefix}_max_up_move_pips": max_up,
        f"{prefix}_max_down_move_pips": max_down,
        f"{prefix}_volume_mean": sum(b.volume for b in bars) / n,
    }


def event_features(
    bars: Sequence[Candle],
    touch: Touch,
    context: Context,
    confirmation: Confirmation,
    *,
    pip: float = 0.0001,
    pre_bars: int = 30,
    post_bars: int = 10,
) -> dict:
    """Extract a single feature dict for one event."""
    f: dict = {}

    # ---- Context (mostly pip-denominated for a common scale) ----
    f["ctx_atr_pips"] = context.atr / pip
    f["ctx_approach_change_pips"] = context.approach_change / pip
    f["ctx_approach_range_pips"] = context.approach_range / pip
    f["ctx_wick_only"] = int(context.wick_only)
    f["ctx_touch_rejection"] = int(context.touch_rejection)
    f["ctx_sma_20d_pips"] = context.sma_20d / pip
    f["ctx_sma_slope_pips"] = context.sma_slope / pip
    # Distance from touch close price to the 20d SMA — a "how stretched?" proxy.
    touch_close = bars[touch.idx].close
    f["ctx_dist_from_sma_pips"] = (touch_close - context.sma_20d) / pip

    # One-hot for touch_shape and trend_alignment so no ordering is implied.
    for s in _SHAPES:
        f[f"ctx_shape_{s}"] = int(context.touch_shape == s)
    for a in _ALIGNMENTS:
        f[f"ctx_align_{a}"] = int(context.trend_alignment == a)

    # ---- Confirmation ----
    for s in _SHAPES:
        f[f"cf_shape_{s}"] = int(confirmation.shape == s)
    f["cf_engulfing"] = int(confirmation.engulfing)
    f["cf_close_away"] = int(confirmation.close_away)

    # ---- Time (cyclical) ----
    hour_sin, hour_cos = _cyclical(context.hour_utc, 24)
    dow_sin, dow_cos = _cyclical(context.dow, 7)
    f["time_hour_sin"] = hour_sin
    f["time_hour_cos"] = hour_cos
    f["time_dow_sin"] = dow_sin
    f["time_dow_cos"] = dow_cos

    # ---- Level and direction ----
    # The level itself: fraction into the range so 0.68 → 0.68 etc.
    f["level"] = touch.level
    for d in _DIRECTIONS:
        f[f"dir_{d}"] = int(touch.direction == d)

    # ---- Pre-touch bars (30 M1 bars = 30 min) ----
    pre_start = max(0, touch.idx - pre_bars)
    pre_slice = list(bars[pre_start:touch.idx])
    f.update(_bar_features(pre_slice, pip, "pre"))

    # ---- Post-touch bars (10 M1 bars = 10 min) ----
    post_end = min(len(bars), touch.idx + 1 + post_bars)
    post_slice = list(bars[touch.idx + 1:post_end])
    f.update(_bar_features(post_slice, pip, "post"))

    # Extra: signed reaction — how far did price move AWAY from the level
    # over the post window, in the direction that would be favorable for us?
    if post_slice:
        # Up-touch → favorable is DOWN (sell for the bounce).
        # Down-touch → favorable is UP (buy for the bounce).
        if touch.direction == "up":
            best_fav = touch.level - min(b.low for b in post_slice)
            worst_adv = max(b.high for b in post_slice) - touch.level
        else:
            best_fav = max(b.high for b in post_slice) - touch.level
            worst_adv = touch.level - min(b.low for b in post_slice)
        f["post_favorable_pips"] = max(0.0, best_fav) / pip
        f["post_adverse_pips"] = max(0.0, worst_adv) / pip
    else:
        f["post_favorable_pips"] = 0.0
        f["post_adverse_pips"] = 0.0

    return f


def build_dataset(
    bars: Sequence[Candle],
    events: Iterable[tuple[Touch, Context, Confirmation, object]],
    trades: Sequence[Trade],
    *,
    pip: float = 0.0001,
    pre_bars: int = 30,
    post_bars: int = 10,
) -> tuple[list[dict], list[dict]]:
    """Align events with their resulting trades, return (features, meta).

    Only events whose limit filled (i.e. that produced a Trade) are
    included. ``meta`` carries per-row entry_time and pnl_pips so training
    scripts can compute time-split labels + per-trade OOS P&L without
    re-running the backtester.
    """
    # Map (level, direction, touch_idx) → Trade if the entry corresponds
    # to that event. entry_idx = touch.idx + 1 + entry_offset (see backtest).
    # We match by the entry_time being close to the touch_time. Simpler:
    # for each trade, its entry_idx points into ``bars``; we walk backwards
    # to find the closest earlier touch, but that's fragile.
    #
    # Cleaner: iterate events, run the same offset logic, and pick the
    # matching trade by (bars[entry_idx].time == expected time). We use the
    # trade's ``level`` field as a tie-breaker.
    #
    # For our use case the mapping is 1:1 between events that filled and
    # trades, in the same chronological order. So a simple pointer walk
    # through both sequences works.

    trades_sorted = sorted(trades, key=lambda t: t.entry_time)
    ti = 0

    features: list[dict] = []
    meta: list[dict] = []

    for touch, context, confirmation, _outcome in events:
        # Fast-forward the trade pointer while its entry_time is before
        # this touch. Once it's past, this touch didn't produce a fill.
        while ti < len(trades_sorted) and trades_sorted[ti].entry_time < touch.time:
            ti += 1

        if ti >= len(trades_sorted):
            continue

        trade = trades_sorted[ti]
        # An event produced this trade if the trade's level and direction
        # match, and the entry sits within the fill window past the touch.
        matches = (
            abs(trade.level - touch.level) < pip / 2
            and trade.entry_time >= touch.time
            and trade.entry_time < _plus_bars_time(bars, touch.idx, 200)  # generous
        )
        # Direction match: up-touch → short trade, down-touch → long trade.
        expected_dir = "short" if touch.direction == "up" else "long"
        if not matches or trade.direction != expected_dir:
            continue

        row = event_features(
            bars, touch, context, confirmation,
            pip=pip, pre_bars=pre_bars, post_bars=post_bars,
        )
        features.append(row)
        meta.append({
            "entry_time": trade.entry_time,
            "level": trade.level,
            "direction": trade.direction,
            "pnl_pips": trade.pnl_price / pip,
            "exit_reason": trade.exit_reason,
        })
        ti += 1  # this trade is consumed

    return features, meta


def _plus_bars_time(bars: Sequence[Candle], idx: int, n_bars: int) -> str:
    """Return the RFC3339 time of ``bars[idx + n_bars]`` or the last bar."""
    j = min(idx + n_bars, len(bars) - 1)
    return bars[j].time
