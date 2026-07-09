"""Test breakeven / trailing stops + trend-slope filter on AUD_USD.

Baseline: 328 trades, +2,203 pips, 47.9% win, max DD 225p.
Each variant is compared to that baseline with the same signal +
2p limit + 1p spread, so any change reflects the stop-management
or filter effect alone.
"""
from __future__ import annotations

import pickle
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.backtest import backtest_touches, summarize_trades  # noqa: E402


SPLIT = "2021-01-01"

BASE = dict(
    pip=0.0001,
    filter_name="all",
    entry="confirm",
    entry_offset=14,
    target_pips=60,
    stop_pips=30,
    max_bars=1440,
    path_ambiguity="worst",
    spread_pips=1.0,
    limit_offset_pips=2.0,
    limit_fill_window=60,
)


def run(bars, events, **overrides):
    kwargs = {**BASE, **overrides}
    trades = backtest_touches(bars, events, **kwargs)
    s = summarize_trades(trades, pip=BASE["pip"])
    reasons = Counter(t.exit_reason for t in trades)
    # Split for H1/H2
    h1 = [t for t in trades if t.entry_time < SPLIT]
    h2 = [t for t in trades if t.entry_time >= SPLIT]
    h1_tot = sum(t.pnl_price for t in h1) / BASE["pip"] if h1 else 0
    h2_tot = sum(t.pnl_price for t in h2) / BASE["pip"] if h2 else 0
    return {
        "n": s["n"],
        "win": s["win_rate"],
        "exp": s["expectancy_pips"],
        "total": s["total_pips"],
        "max_dd": s["max_drawdown_pips"],
        "target": reasons["target"],
        "stop": reasons["stop"],
        "timeout": reasons["timeout"],
        "h1_n": len(h1), "h1_tot": h1_tot,
        "h2_n": len(h2), "h2_tot": h2_tot,
    }


def row(label, r, baseline_total=None):
    delta = f"{r['total'] - baseline_total:+.0f}" if baseline_total is not None else ""
    return (f"{label:<35}  {r['n']:>4}  {r['win']:>3.0f}%  "
            f"{r['exp']:>+5.2f}p  {r['total']:>+6.0f}p  "
            f"DD {r['max_dd']:>4.0f}p  "
            f"T {r['target']:>3}/S {r['stop']:>3}/O {r['timeout']:>3}  "
            f"H1 {r['h1_tot']:>+5.0f} / H2 {r['h2_tot']:>+5.0f}  {delta}")


def main() -> None:
    with open("data/events_m1_cache.pkl", "rb") as f:
        per_pair = pickle.load(f)
    bars, events, _ = per_pair["AUD_USD"]

    print("AUD_USD stop-management + trend-filter sweep")
    print("Config: 60/30/24h, 2p limit, 1p spread, 15-min entry_offset")
    print()
    print(f"{'variant':<35}  {'n':>4}  {'win':>4}  {'exp':>6}  {'total':>7}  "
          f"{'DD':>7}  {'exits (T/S/O)':<19}  {'H1 / H2 splits':<28}  Δtotal")
    print("-" * 145)

    # --- Baseline ---
    base = run(bars, events)
    print(row("baseline (no BE, no trail)", base))
    print()

    # --- Breakeven-only sweep ---
    print("Breakeven snap (stop → entry) at N pips of profit:")
    for be in [10, 15, 20, 25, 30, 40, 50]:
        r = run(bars, events, breakeven_trigger_pips=be)
        print(row(f"  BE at +{be}p", r, base["total"]))
    print()

    # --- Trailing-only sweep ---
    print("Trailing stop only (trigger, distance):")
    for trig, dist in [(20, 10), (30, 15), (30, 20), (40, 20),
                        (40, 30), (50, 20), (50, 30), (60, 30)]:
        r = run(bars, events,
                trail_trigger_pips=trig, trail_distance_pips=dist)
        print(row(f"  trail trig+{trig}p / trail {dist}p", r, base["total"]))
    print()

    # --- BE + trailing combined ---
    print("Breakeven + trailing combined:")
    for be, trig, dist in [(15, 30, 20), (20, 40, 20), (20, 40, 30),
                            (25, 45, 25), (30, 50, 30)]:
        r = run(bars, events,
                breakeven_trigger_pips=be,
                trail_trigger_pips=trig, trail_distance_pips=dist)
        print(row(f"  BE +{be}p / trail +{trig}p @ {dist}p", r, base["total"]))
    print()

    # --- Trend-slope filter alone ---
    print("Trend-slope filter (skip trades where |20d SMA slope| > threshold):")
    for thresh in [30, 50, 80, 100, 150, 200]:
        r = run(bars, events, max_sma_slope_pips=thresh)
        print(row(f"  max slope {thresh}p", r, base["total"]))
    print()

    # --- Best-of-each combined ---
    print("Stacking candidate combos (best BE + trailing + trend):")
    for be, thresh in [(20, 100), (20, 80), (15, 100), (25, 100), (0, 100)]:
        r = run(bars, events,
                breakeven_trigger_pips=be,
                max_sma_slope_pips=thresh)
        print(row(f"  BE +{be}p + max slope {thresh}p", r, base["total"]))


if __name__ == "__main__":
    main()
