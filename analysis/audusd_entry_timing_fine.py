"""Super fine-grain (1-minute) entry-timing sweep on AUD_USD.

Sweeps ``entry_offset`` from 0 to 59 (total post-touch wait: 1 to 60
minutes), everything else at STRATEGIES['audusd']:

  2p limit, 60p target, 30p stop, 24h max hold, 1p spread, 60-min fill window.

Reports per-offset trade count, per-trade expectancy, total pips, and
H1/H2 split. Also renders ``docs/audusd/plots/entry_timing_fine.png``
showing both total P&L and per-trade edge vs wait-time so the shape
is obvious at a glance.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.analyze import analyze  # noqa: E402
from brn_fun.backtest import backtest_touches, summarize_trades  # noqa: E402
from brn_fun.config import load_config  # noqa: E402
from brn_fun.db import connect, fetch_candles  # noqa: E402

SPLIT = "2021-01-01"


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


def bt(bars, events, entry_offset: int):
    trades = backtest_touches(
        bars, events, pip=0.0001, filter_name="all", entry="confirm",
        entry_offset=entry_offset,
        target_pips=60, stop_pips=30, max_bars=1440,
        path_ambiguity="worst",
        spread_pips=1.0, limit_offset_pips=2.0, limit_fill_window=60,
    )
    s = summarize_trades(trades, pip=0.0001)
    h1 = [t for t in trades if t.entry_time < SPLIT]
    h2 = [t for t in trades if t.entry_time >= SPLIT]
    h1_tot = sum(t.pnl_price for t in h1) / 0.0001
    h2_tot = sum(t.pnl_price for t in h2) / 0.0001
    h1_exp = h1_tot / len(h1) if h1 else 0.0
    h2_exp = h2_tot / len(h2) if h2 else 0.0
    return dict(
        n=s["n"], win=s["win_rate"],
        exp=s["expectancy_pips"], total=s["total_pips"],
        h1_exp=h1_exp, h2_exp=h2_exp,
        h1_tot=h1_tot, h2_tot=h2_tot,
    )


def main() -> None:
    bars, events = load_events()

    print("Sweeping entry_offset 0..59 (total post-touch wait 1..60 min)")
    print("Baseline: entry_offset=14 → 15-min total wait")
    print()
    print(f"  {'wait':>4}  {'n':>4}  {'win':>5}  "
          f"{'exp':>7}  {'total':>7}  {'H1 exp':>7}  {'H2 exp':>7}")
    rows = []
    for off in range(60):
        r = bt(bars, events, off)
        r["wait_min"] = off + 1
        rows.append(r)
        marker = " *" if off == 14 else ""
        print(f"  {off + 1:>3}m   {r['n']:>4}  {r['win']:>4.1f}%  "
              f"{r['exp']:>+6.2f}p  {r['total']:>+6.0f}p  "
              f"{r['h1_exp']:>+5.2f}  {r['h2_exp']:>+5.2f}{marker}")

    # -------- Ranked --------
    print()
    print("Ranked by total pips:")
    for r in sorted(rows, key=lambda x: -x["total"])[:10]:
        print(f"  {r['wait_min']:>2d}m  n={r['n']:>3}  "
              f"exp={r['exp']:>+5.2f}  total={r['total']:>+5.0f}p")

    print()
    print("Ranked by min(H1_exp, H2_exp):")
    for r in sorted(rows, key=lambda x: -min(x['h1_exp'], x['h2_exp']))[:10]:
        m = min(r['h1_exp'], r['h2_exp'])
        print(f"  {r['wait_min']:>2d}m  H1={r['h1_exp']:>+5.2f}  "
              f"H2={r['h2_exp']:>+5.2f}  min={m:>+5.2f}")

    # -------- Plot --------
    out = Path("docs/audusd/plots/entry_timing_fine.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    waits = [r["wait_min"] for r in rows]
    totals = [r["total"] for r in rows]
    exps = [r["exp"] for r in rows]
    ns = [r["n"] for r in rows]

    fig, ax1 = plt.subplots(figsize=(12, 6))
    color_total = "#1f77b4"
    ax1.set_xlabel("Total wait after touch (minutes)")
    ax1.set_ylabel("Total P&L over 10y (pips)", color=color_total)
    ax1.plot(waits, totals, marker="o", ms=3, lw=1.5,
              color=color_total, label="Total pips")
    ax1.tick_params(axis="y", labelcolor=color_total)
    ax1.axhline(0, color="black", lw=0.4, alpha=0.5)
    ax1.grid(True, alpha=0.3)
    # Baseline marker
    baseline_total = rows[14]["total"]
    ax1.axvline(15, color="gray", ls="--", lw=1, alpha=0.7)
    ax1.text(15, ax1.get_ylim()[1] * 0.95, "  baseline (15 min)",
              fontsize=9, color="gray", va="top")

    ax2 = ax1.twinx()
    color_exp = "#d62728"
    ax2.set_ylabel("Per-trade expectancy (pips)", color=color_exp)
    ax2.plot(waits, exps, marker="s", ms=3, lw=1.5, ls=":",
              color=color_exp, label="Per-trade edge")
    ax2.tick_params(axis="y", labelcolor=color_exp)

    ax1.set_title("AUD_USD entry timing — 1-min resolution sweep\n"
                    "(2p limit, 60/30 target/stop, 1p spread)")
    ax1.set_xticks(range(0, 61, 5))
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print(f"\nWrote plot → {out}")

    # Second plot: trade count vs wait time
    out2 = Path("docs/audusd/plots/entry_timing_fine_counts.png")
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.bar(waits, ns, color="#2ca02c", alpha=0.7)
    ax.axvline(15, color="gray", ls="--", lw=1, alpha=0.7)
    ax.set_xlabel("Total wait after touch (minutes)")
    ax.set_ylabel("Trade count")
    ax.set_title("Trade count vs entry wait")
    ax.set_xticks(range(0, 61, 5))
    ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out2, dpi=120)
    plt.close(fig)
    print(f"Wrote plot → {out2}")


if __name__ == "__main__":
    main()
