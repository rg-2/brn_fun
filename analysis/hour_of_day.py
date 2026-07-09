"""Time-of-day P&L breakdown for the 4-pair portfolio.

Buckets each pair's trades by UTC hour of ENTRY (that's when a real trader
would have to be watching / letting an algo fire). Reports count, win rate,
expectancy, and total pips per hour, then rolls up into sessions:

    Asian        22-07 UTC   (Tokyo core, mid-Asian, Sydney overlap)
    London-only  07-12 UTC   (London morning before NY open)
    Overlap      12-17 UTC   (London/NY overlap — highest liquidity)
    NY-only      17-22 UTC   (NY afternoon after London close)

Also runs an H1/H2 split so we can see whether the hour pattern is stable
or an artifact of a single regime.
"""
from __future__ import annotations

import pickle
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.backtest import backtest_touches  # noqa: E402


SPLIT = "2021-01-01"

PORTFOLIO = [
    dict(pair="AUD_USD", pip=0.0001, target_pips=60, stop_pips=30,
         max_bars=1440, entry_offset=14),
    dict(pair="USD_CAD", pip=0.0001, target_pips=15, stop_pips=20,
         max_bars=240,  entry_offset=0),
    dict(pair="EUR_JPY", pip=0.01,   target_pips=30, stop_pips=15,
         max_bars=120,  entry_offset=59),
    dict(pair="GBP_USD", pip=0.0001, target_pips=30, stop_pips=15,
         max_bars=120,  entry_offset=59),
]


def session_for(hour: int) -> str:
    if hour >= 22 or hour < 7:
        return "Asian    (22-07)"
    if hour < 12:
        return "London   (07-12)"
    if hour < 17:
        return "Overlap  (12-17)"
    return "NY       (17-22)"


def entry_hour(entry_time: str) -> int:
    """Extract UTC hour from an RFC-3339 timestamp string."""
    return int(entry_time[11:13])


def run_pair(cfg, per_pair):
    bars, events, pip = per_pair[cfg["pair"]]
    trades = backtest_touches(
        bars, events, pip=pip, filter_name="all", entry="confirm",
        entry_offset=cfg["entry_offset"],
        target_pips=cfg["target_pips"], stop_pips=cfg["stop_pips"],
        max_bars=cfg["max_bars"], path_ambiguity="worst",
    )
    return trades, pip


def bucketize(trades, key_fn, pip):
    """Return {key: dict of stats} sorted by key."""
    by_key = defaultdict(list)
    for t in trades:
        by_key[key_fn(t)].append(t)
    result = {}
    for k in sorted(by_key.keys()):
        ts = by_key[k]
        n = len(ts)
        wins = sum(1 for t in ts if t.pnl_price > 0)
        total = sum(t.pnl_price for t in ts) / pip
        result[k] = {
            "n": n,
            "win_rate": wins / n * 100 if n else 0.0,
            "expectancy": total / n if n else 0.0,
            "total": total,
        }
    return result


def print_hour_table(pair: str, trades, pip: float) -> None:
    """24-row hour breakdown."""
    hour_stats = bucketize(trades, lambda t: entry_hour(t.entry_time), pip)
    print(f"\n{pair}   ({len(trades)} total trades)")
    print(f"  {'hour':>4}  {'session':<18}  {'n':>4} {'win%':>4} "
          f"{'exp':>7} {'total':>7}")
    for h in range(24):
        s = hour_stats.get(h)
        if s is None:
            continue
        sess = session_for(h)
        print(f"  {h:>2}:00  {sess:<18}  "
              f"{s['n']:>4} {s['win_rate']:>3.0f}%  "
              f"{s['expectancy']:>+6.2f}p {s['total']:>+7.0f}p")


def print_session_table(trades_by_pair: dict) -> None:
    """Session-level rollup across all pairs + combined portfolio."""
    print("\n=== Session summary ===")
    print(f"  {'session':<18}  ", end="")
    for row in PORTFOLIO:
        print(f"{row['pair']:>18}  ", end="")
    print(f"{'PORTFOLIO':>16}")
    print(f"  {'':<18}  ", end="")
    for _ in PORTFOLIO:
        print(f"{'n':>4} {'exp':>6} {'total':>6}  ", end="")
    print(f"{'n':>4} {'exp':>6} {'total':>6}")

    # Precompute session breakdown per pair.
    sessions = ["Asian    (22-07)", "London   (07-12)",
                "Overlap  (12-17)", "NY       (17-22)"]
    per_pair_sess = {}
    for row in PORTFOLIO:
        stats = bucketize(trades_by_pair[row["pair"]],
                          lambda t: session_for(entry_hour(t.entry_time)),
                          row["pip"])
        per_pair_sess[row["pair"]] = stats

    for sess in sessions:
        line = f"  {sess:<18}  "
        port_n = 0
        port_total = 0.0
        for row in PORTFOLIO:
            s = per_pair_sess[row["pair"]].get(sess, {"n": 0, "expectancy": 0.0, "total": 0.0})
            line += f"{s['n']:>4} {s['expectancy']:>+5.1f}p {s['total']:>+5.0f}p  "
            port_n += s["n"]
            port_total += s["total"]
        exp = port_total / port_n if port_n else 0.0
        line += f"{port_n:>4} {exp:>+5.1f}p {port_total:>+5.0f}p"
        print(line)


def print_hour_oos(trades_by_pair: dict) -> None:
    """H1 vs H2 win rate and expectancy per hour, portfolio-aggregated."""
    print("\n=== Hour-of-day OOS check (portfolio aggregated) ===")
    print(f"  {'hour':>4}  {'session':<18}  "
          f"{'H1 n':>4} {'H1 exp':>7} {'H1 tot':>7}   "
          f"{'H2 n':>4} {'H2 exp':>7} {'H2 tot':>7}   verdict")

    # Aggregate across pairs; normalize to pips per pair's own pip size.
    port_trades = []
    for row in PORTFOLIO:
        for t in trades_by_pair[row["pair"]]:
            port_trades.append((t.entry_time, entry_hour(t.entry_time),
                                t.pnl_price / row["pip"]))
    for h in range(24):
        h1 = [(t, p) for t, hr, p in port_trades if hr == h and t < SPLIT]
        h2 = [(t, p) for t, hr, p in port_trades if hr == h and t >= SPLIT]
        if not h1 and not h2:
            continue
        n1 = len(h1); n2 = len(h2)
        t1 = sum(p for _, p in h1)
        t2 = sum(p for _, p in h2)
        e1 = t1 / n1 if n1 else 0.0
        e2 = t2 / n2 if n2 else 0.0
        if n1 < 10 or n2 < 10:
            v = "thin"
        elif e1 > 0 and e2 > 0:
            v = "✓ both+"
        elif e1 < 0 and e2 < 0:
            v = "✗ both-"
        else:
            v = "⚠ flip"
        print(f"  {h:>2}:00  {session_for(h):<18}  "
              f"{n1:>4} {e1:>+6.1f}p {t1:>+6.0f}p   "
              f"{n2:>4} {e2:>+6.1f}p {t2:>+6.0f}p   {v}")


def main() -> None:
    print("Loading events cache…", flush=True)
    with open("data/events_m1_cache.pkl", "rb") as f:
        per_pair = pickle.load(f)
    trades_by_pair = {}
    print("Running portfolio backtests…", flush=True)
    for cfg in PORTFOLIO:
        trades, _pip = run_pair(cfg, per_pair)
        trades_by_pair[cfg["pair"]] = trades
        print(f"  {cfg['pair']}: {len(trades)} trades", flush=True)

    # Per-pair hourly tables
    for cfg in PORTFOLIO:
        print_hour_table(cfg["pair"], trades_by_pair[cfg["pair"]], cfg["pip"])

    # Session-level rollup
    print_session_table(trades_by_pair)

    # OOS check per hour
    print_hour_oos(trades_by_pair)


if __name__ == "__main__":
    main()
