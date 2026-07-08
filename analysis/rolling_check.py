"""Rolling-year and monthly performance check on the M1 4-pair portfolio.

Each pair runs its own OOS-stable config (from the M1 sweep). We collect all
trades, then bucket by calendar year and calendar month to see how the
portfolio would have performed over time.

Also generates an equity-curve PDF at data/plots/portfolio_equity.pdf:
per-pair cumulative-pips lines, combined-portfolio line, split-date
marker, and a monthly-P&L bar chart.
"""
from __future__ import annotations

import pickle
import sys
from collections import defaultdict
from pathlib import Path

from datetime import datetime

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.analyze import analyze  # noqa: E402
from brn_fun.backtest import backtest_touches, summarize_trades  # noqa: E402
from brn_fun.config import load_config  # noqa: E402
from brn_fun.db import connect, fetch_candles  # noqa: E402


SPLIT = "2021-01-01"

# The M1 portfolio from findings.md.
# entry_offset is *extra* bars past base entry (confirm=+1 bar), so
# entry_offset=14 → total wait 15 min, entry_offset=59 → 60 min, etc.
PORTFOLIO = [
    dict(pair="AUD_USD", pip=0.0001, grid=0.01,
         target_pips=60, stop_pips=30, max_bars=1440, entry_offset=14),
    dict(pair="USD_CAD", pip=0.0001, grid=0.01,
         target_pips=15, stop_pips=20, max_bars=240,  entry_offset=0),
    dict(pair="EUR_JPY", pip=0.01,   grid=1.0,
         target_pips=30, stop_pips=15, max_bars=120,  entry_offset=59),
    dict(pair="GBP_USD", pip=0.0001, grid=0.01,
         target_pips=30, stop_pips=15, max_bars=120,  entry_offset=59),
]
COOLDOWN_M1 = 7200
FORWARD_M1 = 1440


def bucket_year(t: str) -> str:
    return t[:4]


def bucket_month(t: str) -> str:
    return t[:7]  # YYYY-MM


def load_or_analyze() -> dict:
    """Reuse the M1 sweep's cached analyze() results if present."""
    cache = Path("data/events_m1_cache.pkl")
    per_pair: dict = {}
    if cache.exists():
        with cache.open("rb") as f:
            per_pair = pickle.load(f)
    cfg = load_config()
    to_do = [p for p in {row["pair"] for row in PORTFOLIO} if p not in per_pair]
    for pair in to_do:
        row = next(r for r in PORTFOLIO if r["pair"] == pair)
        print(f"Analyzing {pair} at M1…", flush=True)
        with connect(cfg.db_path) as conn:
            bars = fetch_candles(conn, pair, "M1", limit=None, order="asc",
                                  complete_only=True)
        events = list(analyze(bars, grid=row["grid"], cooldown_bars=COOLDOWN_M1,
                              forward_bars=FORWARD_M1, pip=row["pip"]))
        per_pair[pair] = (bars, events, row["pip"])
    return per_pair


def run_portfolio(per_pair: dict) -> dict[str, list]:
    """Run each pair's config and return {pair: [Trade, ...]}."""
    out: dict[str, list] = {}
    for row in PORTFOLIO:
        bars, events, pip = per_pair[row["pair"]]
        trades = backtest_touches(
            bars, events, pip=pip, filter_name="all", entry="confirm",
            entry_offset=row["entry_offset"],
            target_pips=row["target_pips"], stop_pips=row["stop_pips"],
            target_atr=None, stop_atr=None,
            max_bars=row["max_bars"], path_ambiguity="worst",
        )
        out[row["pair"]] = trades
    return out


def per_bucket_pnl(trades, bucket_fn, pip):
    """Return {bucket_key: (n_trades, total_pips, win_rate)} sorted by key."""
    by_bucket = defaultdict(list)
    for t in trades:
        by_bucket[bucket_fn(t.entry_time)].append(t)
    result = {}
    for k, ts in sorted(by_bucket.items()):
        wins = sum(1 for t in ts if t.pnl_price > 0)
        result[k] = (
            len(ts),
            sum(t.pnl_price for t in ts) / pip,
            wins / len(ts) if ts else 0.0,
        )
    return result


def print_yearly_tables(trades_by_pair: dict) -> None:
    """Per-pair yearly P&L, plus combined portfolio."""
    all_years = set()
    yearly: dict[str, dict[str, tuple]] = {}
    for pair, trades in trades_by_pair.items():
        pip = next(r["pip"] for r in PORTFOLIO if r["pair"] == pair)
        y = per_bucket_pnl(trades, bucket_year, pip)
        yearly[pair] = y
        all_years.update(y.keys())

    years = sorted(all_years)
    print("\n=== Yearly P&L (pips) ===")
    print(f"{'year':>6}  ", end="")
    for pair in [r["pair"] for r in PORTFOLIO]:
        print(f"{pair:>16}  ", end="")
    print(f"{'PORTFOLIO':>12}")
    print(f"{'':>6}  ", end="")
    for _ in PORTFOLIO:
        print(f"{'n':>5} {'pips':>7} {'w%':>3}  ", end="")
    print(f"{'pips':>7} {'trades':>7}")

    port_total = 0.0
    port_trades = 0
    for y in years:
        line = f"{y:>6}  "
        row_total = 0.0
        row_trades = 0
        for row in PORTFOLIO:
            pair = row["pair"]
            n, p, w = yearly[pair].get(y, (0, 0.0, 0.0))
            line += f"{n:>5} {p:>+7.0f} {w*100:>3.0f}  "
            row_total += p
            row_trades += n
        line += f"{row_total:>+7.0f} {row_trades:>7}"
        port_total += row_total
        port_trades += row_trades
        print(line)

    print(f"{'TOTAL':>6}  ", end="")
    for pair in [r["pair"] for r in PORTFOLIO]:
        n = sum(yearly[pair].get(y, (0, 0.0, 0.0))[0] for y in years)
        p = sum(yearly[pair].get(y, (0, 0.0, 0.0))[1] for y in years)
        wt = sum(yearly[pair].get(y, (0, 0.0, 0.0))[2] * yearly[pair].get(y, (0, 0.0, 0.0))[0] for y in years)
        w = wt / n if n else 0.0
        print(f"{n:>5} {p:>+7.0f} {w*100:>3.0f}  ", end="")
    print(f"{port_total:>+7.0f} {port_trades:>7}")


def print_monthly_summary(trades_by_pair: dict) -> None:
    """Combined portfolio monthly rollup, and worst-5 / best-5 months."""
    combined_monthly: dict[str, float] = defaultdict(float)
    combined_trades: dict[str, int] = defaultdict(int)
    for pair, trades in trades_by_pair.items():
        pip = next(r["pip"] for r in PORTFOLIO if r["pair"] == pair)
        m = per_bucket_pnl(trades, bucket_month, pip)
        for k, (n, p, _) in m.items():
            combined_monthly[k] += p
            combined_trades[k] += n

    months = sorted(combined_monthly.keys())
    print(f"\n=== Portfolio monthly summary ===   ({len(months)} months)")
    winning_months = sum(1 for m in months if combined_monthly[m] > 0)
    losing_months = sum(1 for m in months if combined_monthly[m] < 0)
    print(f"Winning months: {winning_months} / {len(months)} "
          f"({winning_months/len(months)*100:.1f}%)")
    print(f"Losing months:  {losing_months} / {len(months)} "
          f"({losing_months/len(months)*100:.1f}%)")

    sorted_by_pnl = sorted(combined_monthly.items(), key=lambda kv: kv[1])
    print(f"\nWorst 5 months: ", end="")
    for k, v in sorted_by_pnl[:5]:
        print(f"{k}: {v:+.0f}p ({combined_trades[k]} trades)   ", end="")
    print()
    print(f"Best  5 months: ", end="")
    for k, v in sorted_by_pnl[-5:]:
        print(f"{k}: {v:+.0f}p ({combined_trades[k]} trades)   ", end="")
    print()


def _parse_time(s: str) -> datetime:
    """Parse Oanda RFC-3339 back into a naive datetime for plotting."""
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def plot_equity_curves(trades_by_pair: dict, out_path: Path) -> None:
    """PDF: per-pair equity curves, portfolio curve, monthly P&L bars."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    split_dt = datetime.strptime(SPLIT, "%Y-%m-%d")

    with PdfPages(out_path) as pdf:
        # Page 1: equity curves on a real datetime x-axis.
        fig, ax = plt.subplots(figsize=(12, 7))
        colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]

        # Per-pair cumulative curves (trade by trade in real time).
        combined_trades = []  # (dt, pnl_pips)
        for i, row in enumerate(PORTFOLIO):
            pair = row["pair"]
            pip = row["pip"]
            trades = sorted(trades_by_pair[pair], key=lambda t: t.entry_time)
            dts = [_parse_time(t.entry_time) for t in trades]
            pnls = [t.pnl_price / pip for t in trades]
            cum = []
            running = 0.0
            for p in pnls:
                running += p
                cum.append(running)
            ax.plot(dts, cum, color=colors[i], lw=1.2, label=pair, alpha=0.85)
            for dt, p in zip(dts, pnls):
                combined_trades.append((dt, p))

        # Combined portfolio: sort merged trades by time and cumsum.
        combined_trades.sort()
        c_dts = [dt for dt, _ in combined_trades]
        c_cum = []
        running = 0.0
        for _, p in combined_trades:
            running += p
            c_cum.append(running)
        ax.plot(c_dts, c_cum, color="black", lw=2.0, label="PORTFOLIO",
                alpha=0.95)

        ax.axvline(split_dt, color="gray", ls="--", lw=1, alpha=0.8)
        ax.text(split_dt, ax.get_ylim()[1] * 0.95, "  H1/H2 split",
                fontsize=9, color="gray", va="top")

        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax.set_ylabel("Cumulative pips")
        ax.set_title("brn_fun M1 portfolio — cumulative pips per pair + combined")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper left")
        ax.axhline(0, color="black", lw=0.5, alpha=0.5)
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        # Page 2: monthly P&L bars (combined portfolio).
        monthly = defaultdict(float)
        for pair, trades in trades_by_pair.items():
            pip = next(r["pip"] for r in PORTFOLIO if r["pair"] == pair)
            for t in trades:
                monthly[t.entry_time[:7]] += t.pnl_price / pip
        months = sorted(monthly.keys())
        month_dts = [datetime.strptime(m, "%Y-%m") for m in months]
        values = [monthly[m] for m in months]
        fig, ax = plt.subplots(figsize=(14, 6))
        bar_colors = ["#2ca02c" if v >= 0 else "#d62728" for v in values]
        ax.bar(month_dts, values, color=bar_colors, width=25)
        ax.axvline(split_dt, color="gray", ls="--", lw=1, alpha=0.8)
        ax.axhline(0, color="black", lw=0.5)
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax.set_ylabel("Monthly P&L (pips)")
        ax.set_title("brn_fun M1 portfolio — monthly P&L "
                     "(green = winning month, red = losing)")
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        # Page 3: yearly P&L bars per pair
        fig, ax = plt.subplots(figsize=(11, 6))
        years = sorted({t.entry_time[:4]
                        for trades in trades_by_pair.values() for t in trades})
        x = list(range(len(years)))
        bar_width = 0.2
        for i, row in enumerate(PORTFOLIO):
            pair = row["pair"]; pip = row["pip"]
            per_year = defaultdict(float)
            for t in trades_by_pair[pair]:
                per_year[t.entry_time[:4]] += t.pnl_price / pip
            values = [per_year.get(y, 0.0) for y in years]
            offset = (i - 1.5) * bar_width
            ax.bar([xi + offset for xi in x], values, bar_width,
                   color=colors[i], label=pair, alpha=0.85)
        ax.axhline(0, color="black", lw=0.5)
        ax.set_xticks(x)
        ax.set_xticklabels(years, rotation=0)
        ax.set_ylabel("Yearly P&L (pips)")
        ax.set_title("brn_fun M1 portfolio — yearly P&L by pair")
        ax.legend()
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)


def main() -> None:
    per_pair = load_or_analyze()
    print("\nRunning portfolio backtests…")
    trades_by_pair = run_portfolio(per_pair)
    for pair, trades in trades_by_pair.items():
        row = next(r for r in PORTFOLIO if r["pair"] == pair)
        s = summarize_trades(trades, pip=row["pip"])
        print(f"  {pair}: {s['n']} trades, "
              f"expectancy {s['expectancy_pips']:+.2f}p, "
              f"total {s['total_pips']:+.0f}p, "
              f"win {s['win_rate']:.1f}%, DD {s['max_drawdown_pips']:.0f}p")

    print_yearly_tables(trades_by_pair)
    print_monthly_summary(trades_by_pair)

    out_pdf = Path("data/plots/portfolio_equity.pdf")
    print(f"\nWriting equity-curve PDF → {out_pdf}")
    plot_equity_curves(trades_by_pair, out_pdf)
    print("Done.")


if __name__ == "__main__":
    main()
