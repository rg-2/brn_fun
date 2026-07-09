"""Spread cost + limit-entry sweep across the 4-pair portfolio.

For each pair, sweep limit_offset_pips from 0 (no limit — market at close)
up through 8 pips of favorable improvement. Apply realistic spread costs
per pair (majors ~1p, JPY-crosses ~1.5p) as round-trip fees.

Two questions:
  1. What's baseline P&L after spread cost only, no limit?
  2. What's the sweet-spot limit offset per pair? What does the trade-off
     curve look like (more edge per fill vs fewer fills)?
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.backtest import backtest_touches, summarize_trades  # noqa: E402


SPLIT = "2021-01-01"
LIMIT_FILL_WINDOW = 60  # 60 M1 bars = 1 hour to wait for the limit

# Same portfolio configs. Spread estimates per pair (round-trip).
#   Majors (EUR/USD, AUD/USD, USD/CAD): ~1.0 pip
#   GBP/USD: ~1.2 pip  (higher spread)
#   JPY crosses (EUR/JPY): ~1.5 pips
PORTFOLIO = [
    dict(pair="AUD_USD", pip=0.0001, target_pips=60, stop_pips=30,
         max_bars=1440, entry_offset=14, spread_pips=1.0),
    dict(pair="USD_CAD", pip=0.0001, target_pips=15, stop_pips=20,
         max_bars=240,  entry_offset=0,  spread_pips=1.0),
    dict(pair="EUR_JPY", pip=0.01,   target_pips=30, stop_pips=15,
         max_bars=120,  entry_offset=59, spread_pips=1.5),
    dict(pair="GBP_USD", pip=0.0001, target_pips=30, stop_pips=15,
         max_bars=120,  entry_offset=59, spread_pips=1.2),
]

LIMIT_OFFSETS = [0, 1, 2, 3, 4, 5, 6, 8]


def main() -> None:
    print("Loading events cache…", flush=True)
    with open("data/events_m1_cache.pkl", "rb") as f:
        per_pair = pickle.load(f)

    for cfg in PORTFOLIO:
        pair = cfg["pair"]
        bars, events, pip = per_pair[pair]
        print(f"\n=== {pair} "
              f"(target={cfg['target_pips']}p, stop={cfg['stop_pips']}p, "
              f"spread={cfg['spread_pips']}p) ===")
        print(f"  {'limit':>5}  {'trades':>6}  {'fill%':>5}  "
              f"{'win%':>4}  {'exp':>7}  {'total':>7}  "
              f"{'H1 exp':>7} {'H1 tot':>7}  {'H2 exp':>7} {'H2 tot':>7}")

        max_events = len(events)
        for offset in LIMIT_OFFSETS:
            trades = backtest_touches(
                bars, events, pip=pip, filter_name="all", entry="confirm",
                entry_offset=cfg["entry_offset"],
                target_pips=cfg["target_pips"], stop_pips=cfg["stop_pips"],
                max_bars=cfg["max_bars"], path_ambiguity="worst",
                spread_pips=cfg["spread_pips"],
                limit_offset_pips=float(offset),
                limit_fill_window=LIMIT_FILL_WINDOW,
            )
            s = summarize_trades(trades, pip=pip)
            fill_pct = s["n"] / max_events * 100 if max_events else 0.0

            # H1 / H2 split
            h1 = [t for t in trades if t.entry_time < SPLIT]
            h2 = [t for t in trades if t.entry_time >= SPLIT]
            def stat(subset):
                if not subset:
                    return (0, 0.0, 0.0)
                total_pips = sum(t.pnl_price for t in subset) / pip
                return (len(subset), total_pips / len(subset), total_pips)
            h1_n, h1_exp, h1_tot = stat(h1)
            h2_n, h2_exp, h2_tot = stat(h2)

            print(f"  {offset:>4}p  {s['n']:>6}  {fill_pct:>4.0f}%  "
                  f"{s['win_rate']:>3.0f}%  "
                  f"{s['expectancy_pips']:>+6.2f}p {s['total_pips']:>+6.0f}p  "
                  f"{h1_exp:>+6.2f}p {h1_tot:>+6.0f}p  "
                  f"{h2_exp:>+6.2f}p {h2_tot:>+6.0f}p")


if __name__ == "__main__":
    main()
