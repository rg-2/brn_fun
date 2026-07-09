"""Bar-by-bar backtester for round-number touch events.

Takes a stream of ``(touch, context, confirmation, outcome)`` events from
:func:`brn_fun.analyze.analyze` and simulates simple target-and-stop trades
against the same bars, so we can see what an actual strategy would earn
under given entry/target/stop rules.

Key design choices:

- **Entry** at the close of a chosen bar (touch bar or the confirmation
  bar). Entering at the touch bar's close while filtering on Confirmation
  features would peek at future info — the CLI defaults to ``confirm`` to
  avoid that trap.
- **Direction** follows the physical mean-reversion bet: an ``up`` touch
  (price rejected at a level from below) is a **short**; a ``down`` touch
  is a **long**.
- **Path ambiguity** — when a single M15 bar's range spans both target and
  stop, we don't know from the bar alone which was hit first. We assume
  the stop hit first (worst case, conservative). Selectable later.
- **Timeout** — if neither target nor stop fires within ``max_bars`` bars,
  close at that bar's close.

Positions are independent — overlapping trades are allowed. Position
management (sizing, exposure caps, one-at-a-time) is a strategy layer
concern, not a backtester concern.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import mean
from typing import Callable, Iterable, Literal, Sequence

from .analyze import Confirmation, Context, Touch
from .db import Candle

Direction = Literal["long", "short"]
ExitReason = Literal["target", "stop", "timeout"]
PathAmbiguity = Literal["worst", "best"]


@dataclass(frozen=True, slots=True)
class Trade:
    """One simulated trade from entry to exit.

    When ``limit_offset_pips > 0``, ``signal_price`` is what a market order
    would have paid at close of the signal bar and ``entry_price`` is what
    the resting limit actually filled at. When 0, the two are equal.
    """

    entry_idx: int             # bar the limit filled on (or signal bar if market)
    entry_time: str
    entry_price: float         # actual fill price (may be better than signal)
    signal_price: float        # close of the signal bar (what market would pay)
    direction: Direction
    level: float               # the round-number level that triggered the entry
    target_price: float
    stop_price: float
    exit_idx: int
    exit_time: str
    exit_price: float
    exit_reason: ExitReason
    pnl_price: float           # signed, in price units (positive = win)
    hold_bars: int


# --- Named filter registry --------------------------------------------------
#
# Filters take a (Touch, Context, Confirmation) triple and a ``pip`` size,
# and return True if the event should be traded. Keeping them here (not in
# the CLI) makes them versionable: a run reproducing an old result can
# check out the same commit and get the same filter definition.

FilterFn = Callable[[Touch, Context, Confirmation, float], bool]


def _filter_all(t: Touch, c: Context, cf: Confirmation, pip: float) -> bool:
    return True


def _filter_wick(t: Touch, c: Context, cf: Confirmation, pip: float) -> bool:
    return c.wick_only


def _filter_wick_drift(t: Touch, c: Context, cf: Confirmation, pip: float) -> bool:
    # "Not sprint" = approach change (over 20 bars) at most ~65 pips.
    return c.wick_only and abs(c.approach_change) <= 65 * pip


def _filter_wick_drift_away(
    t: Touch, c: Context, cf: Confirmation, pip: float,
) -> bool:
    # The cross-pair-validated combo. Requires the confirmation bar to exist.
    return (
        c.wick_only
        and abs(c.approach_change) <= 65 * pip
        and cf.present
        and cf.close_away
    )


FILTERS: dict[str, FilterFn] = {
    "all": _filter_all,
    "wick": _filter_wick,
    "wick+drift": _filter_wick_drift,
    "wick+drift+away": _filter_wick_drift_away,
}


def uses_confirmation(filter_name: str) -> bool:
    """Return True if the filter reads Confirmation fields.

    Used by the CLI to default the entry mode to ``confirm`` when the
    filter needs the confirmation bar to have closed.
    """
    return filter_name in ("wick+drift+away",)


# --- Single-trade simulation -----------------------------------------------


def simulate_trade(
    bars: Sequence[Candle],
    entry_idx: int,
    direction: Direction,
    entry_price: float,
    target_price: float,
    stop_price: float,
    max_bars: int,
    path_ambiguity: PathAmbiguity = "worst",
    breakeven_trigger: float = 0.0,
    trail_trigger: float = 0.0,
    trail_distance: float = 0.0,
) -> tuple[int, str, float, ExitReason]:
    """Walk forward from ``entry_idx + 1``; return (exit_idx, exit_time, exit_price, reason).

    Stop management (all in price units, disabled by 0):

    - ``breakeven_trigger``: once max favorable excursion reaches this,
      snap the stop to ``entry_price``. Worst-case outcome becomes 0.
    - ``trail_trigger`` + ``trail_distance``: once max favorable reaches
      the trigger, keep the stop at ``trail_distance`` behind the running
      favorable peak (only ever tightens; never loosens).

    Stop updates are applied AFTER the current bar's exit check — a
    single-bar move that both crosses the trigger and reverses to the
    old stop is resolved by the standard path_ambiguity rule (worst-case
    means old stop wins). Cleaner and matches how a live limit-order
    stop would behave (broker can't act mid-bar).
    """
    current_stop = stop_price
    max_fav = 0.0

    for offset in range(1, max_bars + 1):
        j = entry_idx + offset
        if j >= len(bars):
            break
        bar = bars[j]

        if direction == "long":
            hit_target = bar.high >= target_price
            hit_stop = bar.low <= current_stop
        else:  # short
            hit_target = bar.low <= target_price
            hit_stop = bar.high >= current_stop

        if hit_target and hit_stop:
            # Same bar contains both prices. We can't tell from OHLC alone
            # which fired first; the conservative assumption is stop-first.
            if path_ambiguity == "worst":
                return j, bar.time, current_stop, "stop"
            else:
                return j, bar.time, target_price, "target"
        if hit_target:
            return j, bar.time, target_price, "target"
        if hit_stop:
            return j, bar.time, current_stop, "stop"

        # Update running max favorable, then adjust stop for the *next* bar.
        # Using the bar's high/low as the max extreme reached (worst-case
        # timing means adverse fires before any stop tighten inside a bar).
        if direction == "long":
            fav_this_bar = max(0.0, bar.high - entry_price)
        else:
            fav_this_bar = max(0.0, entry_price - bar.low)
        if fav_this_bar > max_fav:
            max_fav = fav_this_bar
            # Breakeven snap.
            if breakeven_trigger > 0 and max_fav >= breakeven_trigger:
                if direction == "long":
                    current_stop = max(current_stop, entry_price)
                else:
                    current_stop = min(current_stop, entry_price)
            # Trail behind the running peak (only ever tightens).
            if trail_trigger > 0 and max_fav >= trail_trigger:
                if direction == "long":
                    trailed = entry_price + max_fav - trail_distance
                    current_stop = max(current_stop, trailed)
                else:
                    trailed = entry_price - max_fav + trail_distance
                    current_stop = min(current_stop, trailed)

    # Neither fired within max_bars — close at the last bar's close.
    last_j = min(entry_idx + max_bars, len(bars) - 1)
    return last_j, bars[last_j].time, bars[last_j].close, "timeout"


# --- Event-stream driver ---------------------------------------------------


def backtest_touches(
    bars: Sequence[Candle],
    events: Iterable[tuple[Touch, Context, Confirmation, object]],
    *,
    pip: float = 0.0001,
    filter_name: str = "wick+drift+away",
    entry: Literal["touch", "confirm"] = "confirm",
    entry_offset: int = 0,
    target_pips: float = 60.0,
    stop_pips: float = 30.0,
    target_atr: float | None = None,
    stop_atr: float | None = None,
    max_bars: int = 96,
    path_ambiguity: PathAmbiguity = "worst",
    spread_pips: float = 0.0,
    limit_offset_pips: float = 0.0,
    limit_fill_window: int = 60,
    breakeven_trigger_pips: float = 0.0,
    trail_trigger_pips: float = 0.0,
    trail_distance_pips: float = 0.0,
    max_sma_slope_pips: float | None = None,
) -> list[Trade]:
    """Run a target/stop simulation over the filtered events.

    Thresholds are pip-based by default; set ``target_atr`` / ``stop_atr`` to
    an ATR multiplier to scale per-touch. Mixing is allowed.

    Stop management (all pip-denominated, disabled by 0):
      ``breakeven_trigger_pips`` — snap stop to entry once trade shows this
      much profit. ``trail_trigger_pips`` + ``trail_distance_pips`` — trail
      the stop this far behind the running peak once the trigger is reached.

    Trend filter: ``max_sma_slope_pips`` skips events whose Context.sma_slope
    magnitude (in pips) exceeds the threshold — the trade is dropped before
    it enters. Use to avoid trading fades in strong directional markets.
    """
    if filter_name not in FILTERS:
        raise ValueError(
            f"unknown filter {filter_name!r}; known: {sorted(FILTERS)}"
        )
    filter_fn = FILTERS[filter_name]
    trades: list[Trade] = []

    for touch, context, confirmation, _outcome in events:
        if not filter_fn(touch, context, confirmation, pip):
            continue

        # Trend-strength filter: skip trades in markets with strong
        # directional flow (context.sma_slope is signed price change of
        # the 20-day SMA over the last 5 trading days).
        if max_sma_slope_pips is not None:
            slope_pips = abs(context.sma_slope) / pip
            if slope_pips > max_sma_slope_pips:
                continue

        # Decide the entry bar. "confirm" needs bar touch+1 to exist.
        # ``entry_offset`` adds additional bars of waiting after the base
        # entry bar — useful on fine granularities where 1 bar is a very
        # short confirmation (e.g. 1 min at M1 vs 15 min at M15).
        base_offset = 0 if entry == "touch" else 1
        signal_idx = touch.idx + base_offset + entry_offset
        if signal_idx >= len(bars):
            continue

        signal_price = bars[signal_idx].close

        # Physical bet: up-touch means we expect price to REJECT from above,
        # so short; down-touch means bounce up, so long.
        direction: Direction = "short" if touch.direction == "up" else "long"

        # --- Entry: market at signal close, or wait for a favorable limit ---
        if limit_offset_pips > 0:
            # Limit order at offset pips *favorable* to signal — below for
            # longs (buy dips), above for shorts (sell rallies). We watch
            # bars for up to ``limit_fill_window`` bars past signal; if the
            # low (long) / high (short) touches our limit, we fill at limit
            # price and the trade starts from that bar. If not, we skip.
            if direction == "long":
                limit_price = signal_price - limit_offset_pips * pip
            else:
                limit_price = signal_price + limit_offset_pips * pip
            fill_idx: int | None = None
            for lookahead in range(1, limit_fill_window + 1):
                j = signal_idx + lookahead
                if j >= len(bars):
                    break
                bar = bars[j]
                if direction == "long" and bar.low <= limit_price:
                    fill_idx = j
                    break
                if direction == "short" and bar.high >= limit_price:
                    fill_idx = j
                    break
            if fill_idx is None:
                # Limit never touched — no trade.
                continue
            entry_idx = fill_idx
            entry_price = limit_price
        else:
            entry_idx = signal_idx
            entry_price = signal_price

        # Per-trade thresholds — from FILL price, so a better fill shifts
        # target and stop symmetrically. Standard trader behavior.
        target_dist = (
            target_atr * context.atr if target_atr is not None
            else target_pips * pip
        )
        stop_dist = (
            stop_atr * context.atr if stop_atr is not None
            else stop_pips * pip
        )
        if direction == "long":
            target_price = entry_price + target_dist
            stop_price = entry_price - stop_dist
        else:
            target_price = entry_price - target_dist
            stop_price = entry_price + stop_dist

        exit_idx, exit_time, exit_price, reason = simulate_trade(
            bars, entry_idx, direction, entry_price,
            target_price, stop_price, max_bars, path_ambiguity,
            breakeven_trigger=breakeven_trigger_pips * pip,
            trail_trigger=trail_trigger_pips * pip,
            trail_distance=trail_distance_pips * pip,
        )

        gross_pnl = (
            exit_price - entry_price if direction == "long"
            else entry_price - exit_price
        )
        # Round-trip spread cost applied once per completed trade.
        pnl = gross_pnl - spread_pips * pip

        trades.append(Trade(
            entry_idx=entry_idx,
            entry_time=bars[entry_idx].time,
            entry_price=entry_price,
            signal_price=signal_price,
            direction=direction,
            level=touch.level,
            target_price=target_price,
            stop_price=stop_price,
            exit_idx=exit_idx,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=reason,
            pnl_price=pnl,
            hold_bars=exit_idx - entry_idx,
        ))

    return trades


# --- Summary stats ---------------------------------------------------------


def summarize_trades(trades: list[Trade], pip: float = 0.0001) -> dict:
    """Aggregate win rate, expectancy, and drawdown (all in pips)."""
    if not trades:
        return {
            "n": 0, "win_rate": 0.0,
            "avg_win_pips": 0.0, "avg_loss_pips": 0.0,
            "expectancy_pips": 0.0, "total_pips": 0.0,
            "max_drawdown_pips": 0.0,
            "target": 0, "stop": 0, "timeout": 0,
            "avg_hold_bars": 0.0,
        }

    wins = [t for t in trades if t.pnl_price > 0]
    losses = [t for t in trades if t.pnl_price < 0]

    total_price = sum(t.pnl_price for t in trades)
    expectancy_price = total_price / len(trades)

    # Running equity curve → peak-to-trough drawdown.
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_price
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown

    reasons = Counter(t.exit_reason for t in trades)
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_win_pips": mean(t.pnl_price for t in wins) / pip if wins else 0.0,
        "avg_loss_pips": mean(t.pnl_price for t in losses) / pip if losses else 0.0,
        "expectancy_pips": expectancy_price / pip,
        "total_pips": total_price / pip,
        "max_drawdown_pips": max_dd / pip,
        "target": reasons["target"],
        "stop": reasons["stop"],
        "timeout": reasons["timeout"],
        "avg_hold_bars": mean(t.hold_bars for t in trades),
    }
