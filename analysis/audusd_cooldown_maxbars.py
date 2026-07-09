"""Cooldown × max_bars sweep on the AUD_USD strategy.

For each cooldown value we re-run analyze() (which is the only thing that
depends on it — the outcome-tagging ``forward_bars`` doesn't touch the
backtest's P&L). Then for each max_bars value we re-run backtest_touches
using that cooldown's events. All other parameters match STRATEGIES["audusd"]:

  target 60p, stop 30p, entry=confirm+14 (15-min wait), 2p limit,
  60-min limit-fill window, 1p spread, path=worst.

Also reports the H1 (2016–2020) / H2 (2021–2026) split so we can see
whether any (cooldown, max_bars) combo beats the baseline in a way that
survives out of sample.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.analyze import analyze  # noqa: E402
from brn_fun.backtest import backtest_touches, summarize_trades  # noqa: E402
from brn_fun.config import load_config  # noqa: E402
from brn_fun.db import connect, fetch_candles  # noqa: E402


SPLIT = "2021-01-01"

# Baseline for comparison — matches STRATEGIES["audusd"] exactly.
BASE_COOLDOWN = 7200
BASE_MAX_BARS = 1440

# Cooldowns to try (in M1 bars).
COOLDOWNS = [3600, 7200, 14400, 21600]         # 2.5, 5, 10, 15 trading days
# Max-bars (trade timeout) to try.
MAX_BARS_LIST = [480, 720, 1440, 2880, 4320]   # 8h, 12h, 24h, 48h, 72h


def run(bars, events, max_bars):
    trades = backtest_touches(
        bars, events, pip=0.0001, filter_name="all", entry="confirm",
        entry_offset=14,
        target_pips=60, stop_pips=30, max_bars=max_bars,
        path_ambiguity="worst",
        spread_pips=1.0, limit_offset_pips=2.0, limit_fill_window=60,
    )
    s = summarize_trades(trades, pip=0.0001)
    h1 = [t for t in trades if t.entry_time < SPLIT]
    h2 = [t for t in trades if t.entry_time >= SPLIT]
    h1_pips = sum(t.pnl_price for t in h1) / 0.0001 if h1 else 0.0
    h2_pips = sum(t.pnl_price for t in h2) / 0.0001 if h2 else 0.0
    h1_exp = h1_pips / len(h1) if h1 else 0.0
    h2_exp = h2_pips / len(h2) if h2 else 0.0
    return {
        "n": s["n"],
        "exp": s["expectancy_pips"],
        "total": s["total_pips"],
        "h1_n": len(h1), "h1_exp": h1_exp, "h1_tot": h1_pips,
        "h2_n": len(h2), "h2_exp": h2_exp, "h2_tot": h2_pips,
        "target": s["target"], "stop": s["stop"], "timeout": s["timeout"],
    }


def fmt_cell(r):
    """One-line cell for the grid: 'n +total (min-half exp)'."""
    min_exp = min(r["h1_exp"], r["h2_exp"])
    return f"{r['n']:>3} {r['total']:>+5.0f}p {min_exp:>+5.2f}"


def main() -> None:
    cfg = load_config()
    with connect(cfg.db_path) as conn:
        bars = fetch_candles(conn, "AUD_USD", "M1", limit=None, order="asc",
                              complete_only=True)
    print(f"Loaded {len(bars):,} AUD_USD M1 bars "
          f"({bars[0].time[:10]} → {bars[-1].time[:10]})", flush=True)

    grid: dict[tuple[int, int], dict] = {}
    for cd in COOLDOWNS:
        print(f"analyze(cooldown_bars={cd})…", flush=True)
        events = list(analyze(bars, grid=0.01, cooldown_bars=cd,
                               forward_bars=1440, pip=0.0001))
        print(f"  {len(events)} events", flush=True)
        for mb in MAX_BARS_LIST:
            r = run(bars, events, mb)
            grid[(cd, mb)] = r
            print(f"    cooldown={cd:>5}  max_bars={mb:>4}: "
                  f"n={r['n']:>4}  exp={r['exp']:>+5.2f}  "
                  f"total={r['total']:>+6.0f}  "
                  f"H1={r['h1_exp']:>+5.2f}  H2={r['h2_exp']:>+5.2f}",
                  flush=True)

    # --- Grid table (each cell: n / total / min(H1,H2)) ---
    print()
    print("Grid — each cell: `n <total> min(H1_exp, H2_exp)`")
    header = "cooldown \\ max_bars"
    print(f"{header:<22}  " + "  ".join(f"{mb:>16}" for mb in MAX_BARS_LIST))
    for cd in COOLDOWNS:
        row = f"{cd:>5} ({cd//60/24:>4.1f} days)     "
        for mb in MAX_BARS_LIST:
            r = grid[(cd, mb)]
            row += f"  {fmt_cell(r):<16}"
        print(row)

    # --- Baseline reference ---
    base = grid[(BASE_COOLDOWN, BASE_MAX_BARS)]
    print()
    print(f"BASELINE  cooldown={BASE_COOLDOWN} max_bars={BASE_MAX_BARS}: "
          f"n={base['n']}  exp={base['exp']:+.2f}p  total={base['total']:+.0f}p  "
          f"H1={base['h1_exp']:+.2f}p H2={base['h2_exp']:+.2f}p  "
          f"min(H1,H2)={min(base['h1_exp'], base['h2_exp']):+.2f}")

    # --- Ranked ---
    ranked = sorted(grid.items(),
                     key=lambda kv: -min(kv[1]["h1_exp"], kv[1]["h2_exp"]))
    print()
    print("Top 10 by min(H1_exp, H2_exp) — the OOS-honest ranking:")
    print(f"  {'cd':>5}  {'mb':>4}  {'n':>4}  {'total':>6}  {'exp':>6}  "
          f"{'H1 exp':>6}  {'H2 exp':>6}  {'min':>6}   Δ vs baseline")
    for (cd, mb), r in ranked[:10]:
        marker = " *baseline*" if (cd, mb) == (BASE_COOLDOWN, BASE_MAX_BARS) else ""
        d_total = r["total"] - base["total"]
        d_min = min(r["h1_exp"], r["h2_exp"]) - min(base["h1_exp"], base["h2_exp"])
        print(f"  {cd:>5}  {mb:>4}  {r['n']:>4}  {r['total']:>+6.0f}  "
              f"{r['exp']:>+6.2f}  {r['h1_exp']:>+6.2f}  {r['h2_exp']:>+6.2f}  "
              f"{min(r['h1_exp'], r['h2_exp']):>+6.2f}   "
              f"Δtot={d_total:>+5.0f}  Δmin={d_min:>+5.2f}{marker}")


if __name__ == "__main__":
    main()
