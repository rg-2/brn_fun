"""M1 vs M15: does path-ambiguity resolution unlock the 'unknown' pairs?

Analogous to sweep_oos.py but runs at M1 granularity with time-rescaled
parameters. Prints:

  1. Path-ambiguity gap per config (worst vs best) — should be tiny at M1
     if the M1-resolution hypothesis is right.
  2. Per-config aggregate H1/H2 expectancy under worst-case.
  3. Per-pair breakdown for the top few configs.
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.analyze import analyze  # noqa: E402
from brn_fun.backtest import backtest_touches, summarize_trades  # noqa: E402
from brn_fun.config import load_config  # noqa: E402
from brn_fun.db import connect, fetch_candles  # noqa: E402


SPLIT = "2021-01-01"

# (pair, pip, grid)  — same as sweep_oos.py so results are directly comparable.
PAIRS: list[tuple[str, float, float]] = [
    ("EUR_USD", 0.0001, 0.01),
    ("GBP_USD", 0.0001, 0.01),
    ("AUD_USD", 0.0001, 0.01),
    ("USD_CAD", 0.0001, 0.01),
    ("USD_JPY", 0.01,   1.0),
    ("EUR_JPY", 0.01,   1.0),
    ("GBP_JPY", 0.01,   1.0),
]

# Same M1-scaled defaults as CLI:
#   cooldown 7200 M1 = 5 trading days
#   forward  1440 M1 = 24 h
COOLDOWN_M1 = 7200
FORWARD_M1 = 1440


# Configs: (name, target_pips, stop_pips, max_bars_M1, filter, path)
CONFIGS = [
    # Slow trades (matches earlier M15 finding "wick+drift+away 60/30/24h"):
    ("60/30/24h w+d+a",    60, 30, 1440, "wick+drift+away", "worst"),
    ("60/30/24h all",      60, 30, 1440, "all", "worst"),
    ("90/30/24h w+d+a",    90, 30, 1440, "wick+drift+away", "worst"),
    ("90/30/24h all",      90, 30, 1440, "all", "worst"),
    # Quick trades — the ones that suffered from M15 path ambiguity:
    ("15/20/2h all",       15, 20, 120,  "all", "worst"),
    ("15/20/4h all",       15, 20, 240,  "all", "worst"),
    ("20/25/4h all",       20, 25, 240,  "all", "worst"),
    ("30/15/2h all",       30, 15, 120,  "all", "worst"),
    ("25/25/4h all",       25, 25, 240,  "all", "worst"),
]


def run(config_row, bars, events, pip):
    _name, tp, sp, mb, filt, path = config_row
    return summarize_trades(
        backtest_touches(bars, events, pip=pip,
                         target_pips=tp, stop_pips=sp, max_bars=mb,
                         filter_name=filt, entry="confirm",
                         path_ambiguity=path),
        pip=pip,
    )


def main() -> None:
    cfg = load_config()

    # Cache analyze() results in a pickle so we don't re-run the 50s scan
    # if this script is invoked repeatedly during exploration.
    cache_path = Path("data/events_m1_cache.pkl")
    per_pair: dict[str, tuple] = {}
    if cache_path.exists():
        print(f"Loading cached events from {cache_path}…")
        with cache_path.open("rb") as f:
            per_pair = pickle.load(f)

    to_compute = [(p, pip, grid) for p, pip, grid in PAIRS if p not in per_pair]
    for pair, pip, grid in to_compute:
        t0 = time.time()
        print(f"Analyzing {pair} at M1 (cooldown={COOLDOWN_M1}, forward={FORWARD_M1})…", flush=True)
        with connect(cfg.db_path) as conn:
            bars = fetch_candles(conn, pair, "M1", limit=None, order="asc",
                                  complete_only=True)
        events = list(analyze(bars, grid=grid, cooldown_bars=COOLDOWN_M1,
                              forward_bars=FORWARD_M1, pip=pip))
        per_pair[pair] = (bars, events, pip)
        print(f"  {len(bars):,} bars, {len(events)} touches   ({time.time()-t0:.1f}s)")
    if to_compute:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("wb") as f:
            pickle.dump(per_pair, f)
        print(f"Cached to {cache_path}")

    # --- (1) Path-ambiguity gap per config ------------------------------
    print()
    print("=== Path-ambiguity gap at M1 (worst vs best), aggregated across pairs ===")
    print(f"{'config':<20}  {'worst H1':>9} {'worst H2':>9}   "
          f"{'best H1':>9} {'best H2':>9}   {'gap H1':>7} {'gap H2':>7}")
    for row in CONFIGS:
        name, tp, sp, mb, filt, _path = row
        w1_pips = w2_pips = 0.0
        b1_pips = b2_pips = 0.0
        n1 = n2 = 0
        for pair in [p[0] for p in PAIRS]:
            bars, events, pip = per_pair[pair]
            e1 = [e for e in events if e[0].time < SPLIT]
            e2 = [e for e in events if e[0].time >= SPLIT]
            w1 = run((name, tp, sp, mb, filt, "worst"), bars, e1, pip)
            w2 = run((name, tp, sp, mb, filt, "worst"), bars, e2, pip)
            b1 = run((name, tp, sp, mb, filt, "best"),  bars, e1, pip)
            b2 = run((name, tp, sp, mb, filt, "best"),  bars, e2, pip)
            w1_pips += w1["total_pips"]; w2_pips += w2["total_pips"]
            b1_pips += b1["total_pips"]; b2_pips += b2["total_pips"]
            n1 += w1["n"]; n2 += w2["n"]
        we1 = w1_pips / n1 if n1 else 0.0
        we2 = w2_pips / n2 if n2 else 0.0
        be1 = b1_pips / n1 if n1 else 0.0
        be2 = b2_pips / n2 if n2 else 0.0
        print(f"{name:<20}  {we1:>+8.2f}p {we2:>+8.2f}p   "
              f"{be1:>+8.2f}p {be2:>+8.2f}p   "
              f"{be1-we1:>+6.2f}p {be2-we2:>+6.2f}p")

    # --- (2) Per-config aggregate expectancy (worst case) ---------------
    print()
    print("=== Aggregate H1/H2 expectancy, worst-case path ambiguity ===")
    print(f"{'config':<20}  {'H1 n':>5} {'H1 exp':>7} {'H1 tot':>7}   "
          f"{'H2 n':>5} {'H2 exp':>7} {'H2 tot':>7}   {'min exp':>7}")
    results: list[dict] = []
    for row in CONFIGS:
        name, tp, sp, mb, filt, _path = row
        h1_pips = h2_pips = 0.0
        h1_n = h2_n = 0
        per_pair_data: dict[str, tuple] = {}
        for pair in [p[0] for p in PAIRS]:
            bars, events, pip = per_pair[pair]
            e1 = [e for e in events if e[0].time < SPLIT]
            e2 = [e for e in events if e[0].time >= SPLIT]
            s1 = run((name, tp, sp, mb, filt, "worst"), bars, e1, pip)
            s2 = run((name, tp, sp, mb, filt, "worst"), bars, e2, pip)
            h1_pips += s1["total_pips"]; h2_pips += s2["total_pips"]
            h1_n += s1["n"]; h2_n += s2["n"]
            per_pair_data[pair] = (s1, s2)
        h1_exp = h1_pips / h1_n if h1_n else 0.0
        h2_exp = h2_pips / h2_n if h2_n else 0.0
        results.append({
            "name": name, "h1_n": h1_n, "h1_exp": h1_exp, "h1_tot": h1_pips,
            "h2_n": h2_n, "h2_exp": h2_exp, "h2_tot": h2_pips,
            "min_exp": min(h1_exp, h2_exp),
            "per_pair": per_pair_data,
        })
        print(f"{name:<20}  {h1_n:>5} {h1_exp:>+7.2f} {h1_pips:>+7.0f}   "
              f"{h2_n:>5} {h2_exp:>+7.2f} {h2_pips:>+7.0f}   "
              f"{results[-1]['min_exp']:>+7.2f}")

    # --- (3) Per-pair breakdown for the top 3 -------------------------
    top = sorted(results, key=lambda x: -x["min_exp"])[:3]
    print()
    print("=== Per-pair detail for top 3 configs (by min(H1, H2)) ===")
    for r in top:
        print(f"\n  --- {r['name']}   min_exp={r['min_exp']:+.2f} ---")
        print(f"    {'pair':<9}  {'H1 n':>5} {'H1 exp':>7} {'H1 tot':>7}   "
              f"{'H2 n':>5} {'H2 exp':>7} {'H2 tot':>7}  verdict")
        for pair in [p[0] for p in PAIRS]:
            s1, s2 = r["per_pair"][pair]
            if s1["expectancy_pips"] > 0 and s2["expectancy_pips"] > 0:
                v = "✓ both+"
            elif s1["expectancy_pips"] < 0 and s2["expectancy_pips"] < 0:
                v = "✗ both-"
            else:
                v = "⚠ flip"
            print(f"    {pair:<9}  "
                  f"{s1['n']:>5} {s1['expectancy_pips']:>+7.2f} {s1['total_pips']:>+7.0f}   "
                  f"{s2['n']:>5} {s2['expectancy_pips']:>+7.2f} {s2['total_pips']:>+7.0f}  {v}")


if __name__ == "__main__":
    main()
