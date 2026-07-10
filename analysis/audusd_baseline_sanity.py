"""Fine-grained sanity sweeps around the AUD_USD baseline.

Three independent 1D sweeps, everything else at STRATEGIES['audusd']:

  1. limit_offset_pips: fractional resolution around the 2p sweet spot.
  2. entry_offset (confirmation wait): 5, 10, 15, 20, 30, 45, 60 min.
  3. target_pips: 40..80 in 5-pip steps, stop fixed at 30p.

Reports per-trade expectancy, total pips, and H1/H2 split so we can
tell whether any deviation is regime-driven or a genuine improvement.
Every other parameter matches the settled strategy config.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.analyze import analyze  # noqa: E402
from brn_fun.backtest import backtest_touches, summarize_trades  # noqa: E402
from brn_fun.config import load_config  # noqa: E402
from brn_fun.db import connect, fetch_candles  # noqa: E402
from brn_fun.strategy import get_strategy  # noqa: E402

SPLIT = "2021-01-01"


def load_events():
    """Cache-friendly: analyze once, reuse across sweeps (independent of
    the backtest knobs we vary here)."""
    cache = Path("data/audusd_ml_cache.pkl")
    if cache.exists():
        with cache.open("rb") as f:
            bars, events, _trades = pickle.load(f)
        return bars, events

    cfg = load_config()
    with connect(cfg.db_path) as conn:
        bars = fetch_candles(conn, "AUD_USD", "M1", limit=None, order="asc",
                              complete_only=True)
    print(f"Loaded {len(bars):,} bars", flush=True)
    print("Running analyze()…", flush=True)
    events = list(analyze(bars, grid=0.01, cooldown_bars=7200,
                          forward_bars=1440, pip=0.0001))
    return bars, events


def run(bars, events, *, limit=2.0, entry_offset=14, target=60.0, stop=30.0):
    """Backtest with the settled config, overriding the swept knob."""
    trades = backtest_touches(
        bars, events, pip=0.0001, filter_name="all", entry="confirm",
        entry_offset=entry_offset,
        target_pips=target, stop_pips=stop, max_bars=1440,
        path_ambiguity="worst",
        spread_pips=1.0, limit_offset_pips=limit, limit_fill_window=60,
    )
    s = summarize_trades(trades, pip=0.0001)
    h1 = [t for t in trades if t.entry_time < SPLIT]
    h2 = [t for t in trades if t.entry_time >= SPLIT]
    h1_tot = sum(t.pnl_price for t in h1) / 0.0001
    h2_tot = sum(t.pnl_price for t in h2) / 0.0001
    h1_exp = h1_tot / len(h1) if h1 else 0.0
    h2_exp = h2_tot / len(h2) if h2 else 0.0
    return dict(
        n=s["n"],
        win=s["win_rate"],
        exp=s["expectancy_pips"],
        total=s["total_pips"],
        h1_exp=h1_exp, h1_tot=h1_tot,
        h2_exp=h2_exp, h2_tot=h2_tot,
        target=s["target"], stop=s["stop"], timeout=s["timeout"],
    )


def print_row(row, label):
    print(
        f"  {label:<15}  n={row['n']:>4}  win={row['win']:>4.1f}%  "
        f"exp={row['exp']:>+6.2f}p  total={row['total']:>+6.0f}p  "
        f"H1={row['h1_exp']:>+5.2f} H2={row['h2_exp']:>+5.2f}   "
        f"T/S/O={row['target']}/{row['stop']}/{row['timeout']}"
    )


def main() -> None:
    bars, events = load_events()
    strat = get_strategy("audusd")
    print(f"\nBaseline strategy (STRATEGIES['audusd']):")
    print(f"  limit={strat.limit_offset_pips}p  entry_offset={strat.entry_offset}"
           f" (=15 min total)  target={strat.target_pips}p  stop={strat.stop_pips}p")
    baseline = run(bars, events)
    print()
    print_row(baseline, "BASELINE")
    print(f"  (total pips reference: {baseline['total']:+.0f})")

    # -------- Sweep 1: limit_offset_pips --------
    print()
    print("=== Sweep 1: limit_offset_pips (fractional) ===")
    print("  15-min entry offset, 60p target, 30p stop, 1p spread, 60m fill window")
    for lo in [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]:
        r = run(bars, events, limit=lo)
        print_row(r, f"limit={lo:>3.1f}p")

    # -------- Sweep 2: entry_offset --------
    print()
    print("=== Sweep 2: confirmation wait (entry_offset in extra bars) ===")
    print("  2p limit, 60p target, 30p stop; baseline is 14 extra bars = 15 min total")
    for extra_bars, total_min in [(4, 5), (9, 10), (14, 15), (19, 20),
                                   (29, 30), (44, 45), (59, 60)]:
        r = run(bars, events, entry_offset=extra_bars)
        print_row(r, f"wait={total_min:>2d} min")

    # -------- Sweep 3: target_pips (stop fixed at 30) --------
    print()
    print("=== Sweep 3: target_pips (stop=30, so R:R varies) ===")
    print("  2p limit, 15-min entry offset, 30p stop, 24h max hold")
    for tgt in [40, 45, 50, 55, 60, 65, 70, 75, 80]:
        r = run(bars, events, target=tgt)
        rr = tgt / 30
        print_row(r, f"tgt={tgt:>3d}p R:R={rr:.2f}")


if __name__ == "__main__":
    main()
