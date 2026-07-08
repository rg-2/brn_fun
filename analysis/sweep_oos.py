"""Robustness sweep: which target/stop/filter config generalizes across time?

Run this after backfilling ≥10 years of data. It sweeps a set of candidate
configurations, computes backtest P&L separately on the H1 (pre-split) and
H2 (post-split) halves of each pair's history, and ranks by the *worst-half*
aggregate expectancy — the honest measure of a config's time-stability.

Usage:
    uv run python analysis/sweep_oos.py

Output:
    1. Per-config aggregate table (H1 / H2 across all pairs).
    2. Ranked list by worst-half expectancy.
    3. Per-pair breakdown for the top few configs.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from repo root without installing.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.analyze import analyze  # noqa: E402
from brn_fun.backtest import backtest_touches, summarize_trades  # noqa: E402
from brn_fun.config import load_config  # noqa: E402
from brn_fun.db import connect, fetch_candles  # noqa: E402


SPLIT = "2021-01-01"


# (pair, pip_size, round-number grid)
PAIRS: list[tuple[str, float, float]] = [
    ("EUR_USD", 0.0001, 0.01),
    ("GBP_USD", 0.0001, 0.01),
    ("AUD_USD", 0.0001, 0.01),
    ("USD_CAD", 0.0001, 0.01),
    ("USD_JPY", 0.01,   1.0),
    ("EUR_JPY", 0.01,   1.0),
    ("GBP_JPY", 0.01,   1.0),
]


# Each config is a dict merged into backtest_touches kwargs.
# `name` is display-only.
CONFIGS: list[dict] = [
    dict(name="60/30 default",     filter_name="wick+drift+away", target_pips=60, stop_pips=30),
    dict(name="60/30 best-case",   filter_name="wick+drift+away", target_pips=60, stop_pips=30, path_ambiguity="best"),
    dict(name="30/30 (1:1)",       filter_name="wick+drift+away", target_pips=30, stop_pips=30),
    dict(name="90/30 (3:1)",       filter_name="wick+drift+away", target_pips=90, stop_pips=30),
    dict(name="60/15 (4:1)",       filter_name="wick+drift+away", target_pips=60, stop_pips=15),
    dict(name="3xATR/1.5xATR",     filter_name="wick+drift+away", target_atr=3.0, stop_atr=1.5),
    dict(name="2xATR/1xATR",       filter_name="wick+drift+away", target_atr=2.0, stop_atr=1.0),
    # Filter variants for context:
    dict(name="wick-only 60/30",   filter_name="wick",             target_pips=60, stop_pips=30),
    dict(name="wick+drift 60/30",  filter_name="wick+drift",       target_pips=60, stop_pips=30),
    dict(name="wick+drift 90/30",  filter_name="wick+drift",       target_pips=90, stop_pips=30),
    dict(name="all events 90/30",  filter_name="all",              target_pips=90, stop_pips=30),
]


def run_backtest(config: dict, bars, events, pip: float) -> dict:
    kwargs = dict(config)
    kwargs.pop("name", None)
    # Sensible defaults where the config didn't override.
    kwargs.setdefault("target_pips", 60)
    kwargs.setdefault("stop_pips", 30)
    kwargs.setdefault("target_atr", None)
    kwargs.setdefault("stop_atr", None)
    kwargs.setdefault("path_ambiguity", "worst")
    kwargs.setdefault("entry", "confirm")
    kwargs.setdefault("max_bars", 96)
    trades = backtest_touches(bars, events, pip=pip, **kwargs)
    return summarize_trades(trades, pip=pip)


def main() -> None:
    cfg = load_config()

    # Precompute events per pair — analyze() dominates the runtime.
    per_pair_data = {}
    print("Loading + analyzing each pair (this takes a few seconds)…")
    for pair, pip, grid in PAIRS:
        with connect(cfg.db_path) as conn:
            bars = fetch_candles(conn, pair, "M15", limit=None, order="asc",
                                  complete_only=True)
        events = list(analyze(bars, grid=grid, cooldown_bars=480,
                              forward_bars=96, pip=pip))
        per_pair_data[pair] = (bars, pip, events)
        print(f"  {pair}: {len(bars):,} bars, {len(events)} touches")

    # --- Aggregate view ---------------------------------------------------
    results: list[dict] = []
    print()
    print(f"OOS split at {SPLIT}   (entry=confirm, max_bars=96, path=worst except where noted)")
    print()
    print(f"{'config':<24}  "
          f"{'H1 n':>5} {'H1 exp':>7} {'H1 tot':>7}  |  "
          f"{'H2 n':>5} {'H2 exp':>7} {'H2 tot':>7}  |  "
          f"{'min exp':>7} {'both+':>5}")

    for config in CONFIGS:
        h1_pips = h2_pips = 0.0
        h1_n = h2_n = 0
        both_positive_pairs = 0
        per_pair_details = {}
        for pair, (bars, pip, events) in per_pair_data.items():
            e1 = [e for e in events if e[0].time < SPLIT]
            e2 = [e for e in events if e[0].time >= SPLIT]
            s1 = run_backtest(config, bars, e1, pip)
            s2 = run_backtest(config, bars, e2, pip)
            h1_pips += s1["total_pips"]
            h2_pips += s2["total_pips"]
            h1_n += s1["n"]
            h2_n += s2["n"]
            if s1["expectancy_pips"] > 0 and s2["expectancy_pips"] > 0:
                both_positive_pairs += 1
            per_pair_details[pair] = (s1, s2)

        h1_exp = h1_pips / h1_n if h1_n else 0.0
        h2_exp = h2_pips / h2_n if h2_n else 0.0
        min_exp = min(h1_exp, h2_exp)

        results.append({
            "name": config["name"],
            "h1_n": h1_n, "h1_exp": h1_exp, "h1_tot": h1_pips,
            "h2_n": h2_n, "h2_exp": h2_exp, "h2_tot": h2_pips,
            "min_exp": min_exp, "both_positive_pairs": both_positive_pairs,
            "per_pair": per_pair_details,
        })
        print(f"{config['name']:<24}  "
              f"{h1_n:>5} {h1_exp:>+7.2f} {h1_pips:>+7.0f}  |  "
              f"{h2_n:>5} {h2_exp:>+7.2f} {h2_pips:>+7.0f}  |  "
              f"{min_exp:>+7.2f} {both_positive_pairs:>4d}/7")

    # --- Ranked list -----------------------------------------------------
    print()
    print("Ranked by min(H1 exp, H2 exp) — the honest 'worst half must be positive' test:")
    for r in sorted(results, key=lambda x: -x["min_exp"]):
        stable_tag = "TIME-STABLE ✓" if r["min_exp"] > 0 else "regime-flipped"
        print(f"  {r['name']:<24}  min_exp={r['min_exp']:+.2f}  "
              f"both+={r['both_positive_pairs']}/7 pairs   [{stable_tag}]")

    # --- Per-pair detail for the top 3 -----------------------------------
    top = sorted(results, key=lambda x: -x["min_exp"])[:3]
    print()
    print("Per-pair detail for top 3 configs:")
    for r in top:
        print(f"\n  === {r['name']} ===")
        print(f"    {'pair':<9}  "
              f"{'H1 n':>5} {'H1 exp':>7} {'H1 tot':>7}  |  "
              f"{'H2 n':>5} {'H2 exp':>7} {'H2 tot':>7}  |  verdict")
        for pair in [p[0] for p in PAIRS]:
            s1, s2 = r["per_pair"][pair]
            if s1["expectancy_pips"] > 0 and s2["expectancy_pips"] > 0:
                v = "✓ both+"
            elif s1["expectancy_pips"] < 0 and s2["expectancy_pips"] < 0:
                v = "✗ both-"
            else:
                v = "⚠ flip"
            print(f"    {pair:<9}  "
                  f"{s1['n']:>5} {s1['expectancy_pips']:>+7.2f} {s1['total_pips']:>+7.0f}  |  "
                  f"{s2['n']:>5} {s2['expectancy_pips']:>+7.2f} {s2['total_pips']:>+7.0f}  |  {v}")


if __name__ == "__main__":
    main()
