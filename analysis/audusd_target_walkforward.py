"""Walk-forward check on target=80p vs target=60p for AUD_USD.

For each calendar year 2016-2026:
  - Backtest every trade that entered that year at the settled config
    (2p limit, 15-min entry, 30p stop, 24h max, 1p spread).
  - Once with target=60p (baseline), once with target=80p (alternative).
  - Report per-year totals, per-trade edge, and which target wins.

We're not "training" anything, so this is a per-year consistency check
rather than a train/test split. But it answers the same question:
does the +146p 10-year advantage of 80p over 60p come from a few
outlier years, or is it broad-based? Simple robustness rule of thumb:
if 80p wins in ≥60% of years, adopt. If it's driven by 1-2 years,
leave the baseline alone.
"""
from __future__ import annotations

import pickle
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.analyze import analyze  # noqa: E402
from brn_fun.backtest import backtest_touches  # noqa: E402
from brn_fun.config import load_config  # noqa: E402
from brn_fun.db import connect, fetch_candles  # noqa: E402


def load_events():
    cache = Path("data/audusd_ml_cache.pkl")
    if cache.exists():
        with cache.open("rb") as f:
            bars, events, _trades = pickle.load(f)
        return bars, events
    cfg = load_config()
    with connect(cfg.db_path) as conn:
        bars = fetch_candles(conn, "AUD_USD", "M1", limit=None, order="asc",
                              complete_only=True)
    events = list(analyze(bars, grid=0.01, cooldown_bars=7200,
                          forward_bars=1440, pip=0.0001))
    return bars, events


def run_trades(bars, events, target_pips: int):
    return backtest_touches(
        bars, events, pip=0.0001, filter_name="all", entry="confirm",
        entry_offset=14,
        target_pips=target_pips, stop_pips=30, max_bars=1440,
        path_ambiguity="worst",
        spread_pips=1.0, limit_offset_pips=2.0, limit_fill_window=60,
    )


def main() -> None:
    bars, events = load_events()

    trades_60 = run_trades(bars, events, 60)
    trades_80 = run_trades(bars, events, 80)

    # Group both by calendar year of entry.
    by_year_60: dict[str, list] = defaultdict(list)
    by_year_80: dict[str, list] = defaultdict(list)
    for t in trades_60:
        by_year_60[t.entry_time[:4]].append(t)
    for t in trades_80:
        by_year_80[t.entry_time[:4]].append(t)

    years = sorted(set(by_year_60) | set(by_year_80))

    print("Per-year comparison — target=60p (baseline) vs target=80p")
    print()
    print(f"  {'year':<5}  "
          f"{'n':>4}  {'60p exp':>7} {'60p tot':>7}  "
          f"{'n':>4}  {'80p exp':>7} {'80p tot':>7}  "
          f"{'Δ exp':>6} {'Δ tot':>6}  winner")

    win_count = 0
    year_count = 0
    delta_totals = []

    for y in years:
        t60 = by_year_60[y]
        t80 = by_year_80[y]
        # Skip if fewer than 15 trades — noise dominates.
        if len(t60) < 15:
            print(f"  {y:<5}  {len(t60):>4}  (skipped — too few trades)")
            continue

        pnl_60 = sum(t.pnl_price for t in t60) / 0.0001
        pnl_80 = sum(t.pnl_price for t in t80) / 0.0001
        exp_60 = pnl_60 / len(t60)
        exp_80 = pnl_80 / len(t80)
        d_exp = exp_80 - exp_60
        d_tot = pnl_80 - pnl_60
        delta_totals.append(d_tot)
        year_count += 1
        winner = "80p" if pnl_80 > pnl_60 else "60p"
        if winner == "80p":
            win_count += 1
        print(f"  {y:<5}  "
              f"{len(t60):>4}  {exp_60:>+6.2f}p {pnl_60:>+6.0f}p  "
              f"{len(t80):>4}  {exp_80:>+6.2f}p {pnl_80:>+6.0f}p  "
              f"{d_exp:>+5.2f} {d_tot:>+5.0f}    {winner}")

    print()
    print("=== Summary ===")
    print(f"  Years compared:              {year_count}")
    print(f"  Years where 80p won:         {win_count} / {year_count}")
    print(f"  Median Δ tot (80p - 60p):    {sorted(delta_totals)[len(delta_totals)//2]:+.0f} pips/year")
    print(f"  Mean Δ tot per year:         {sum(delta_totals) / year_count:+.0f} pips/year")
    print(f"  Cumulative Δ tot over 10y:   {sum(delta_totals):+.0f} pips")

    total_60 = sum(t.pnl_price for t in trades_60) / 0.0001
    total_80 = sum(t.pnl_price for t in trades_80) / 0.0001
    print(f"\n  60p all-years total: {total_60:+.0f} pips")
    print(f"  80p all-years total: {total_80:+.0f} pips")
    print(f"  Δ: {total_80 - total_60:+.0f} pips")


if __name__ == "__main__":
    main()
