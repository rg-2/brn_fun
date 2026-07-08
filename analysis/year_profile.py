"""Profile each year's market character to see what made 2016 and 2022 special.

Aggregate ALL 7 pairs' events per calendar year and compute base-rate
market metrics (not strategy-specific):

  - Event supply: touches per year
  - Feature distribution: %wick, %rejection, %close_away, %trend up/down/flat
  - Median ATR at touch (volatility proxy)
  - Reaction metrics: hit-rate at 10p / 15p / 20p targets within 2h;
    median max fav and adv within 2h

The idea: if 2016 and 2022 (the big-P&L years) share a signature that
2019 or 2024-26 (weak years) lack, that signature is our meta-filter.
"""
from __future__ import annotations

import pickle
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.analyze import analyze  # noqa: E402
from brn_fun.config import load_config  # noqa: E402
from brn_fun.db import connect, fetch_candles  # noqa: E402
from brn_fun.reaction import compute_reactions  # noqa: E402


PAIRS = [
    ("EUR_USD", 0.0001, 0.01),
    ("GBP_USD", 0.0001, 0.01),
    ("AUD_USD", 0.0001, 0.01),
    ("USD_CAD", 0.0001, 0.01),
    ("USD_JPY", 0.01,   1.0),
    ("EUR_JPY", 0.01,   1.0),
    ("GBP_JPY", 0.01,   1.0),
]
COOLDOWN_M1 = 7200
FORWARD_M1 = 1440
REACTION_WINDOW = 120  # 2h at M1


def load_cache() -> dict:
    cache = Path("data/events_m1_cache.pkl")
    per_pair: dict = {}
    if cache.exists():
        with cache.open("rb") as f:
            per_pair = pickle.load(f)
    cfg = load_config()
    to_do = [(p, pip, grid) for p, pip, grid in PAIRS if p not in per_pair]
    for pair, pip, grid in to_do:
        print(f"Analyzing {pair}…", flush=True)
        with connect(cfg.db_path) as conn:
            bars = fetch_candles(conn, pair, "M1", limit=None, order="asc",
                                  complete_only=True)
        events = list(analyze(bars, grid=grid, cooldown_bars=COOLDOWN_M1,
                              forward_bars=FORWARD_M1, pip=pip))
        per_pair[pair] = (bars, events, pip)
    if to_do:
        with cache.open("wb") as f:
            pickle.dump(per_pair, f)
    return per_pair


def main() -> None:
    per_pair = load_cache()

    # Aggregate per year across all pairs. For each event we record:
    #   pip (pair's pip size), touch, context, confirmation, and later the reaction.
    by_year: dict[str, list] = defaultdict(list)
    for pair, (bars, events, pip) in per_pair.items():
        # Compute reactions once per pair, then pair each with its (t, c, cf).
        reactions = compute_reactions(bars, events,
                                       forward_bars=REACTION_WINDOW,
                                       entry="confirm")
        # reactions align by index with the *filtered* events (those with
        # confirm bar present); compute_reactions preserves order but drops
        # entries. Rebuild a parallel list of (t, c, cf) to zip against.
        aligned = []
        for touch, context, confirm, out in events:
            if not confirm.present:
                continue
            if touch.idx + 1 >= len(bars):
                continue
            aligned.append((touch, context, confirm, out))
        assert len(aligned) == len(reactions), (
            f"mismatch aligning events and reactions for {pair}: "
            f"{len(aligned)} vs {len(reactions)}"
        )
        for (touch, context, confirm, out), reaction in zip(aligned, reactions):
            y = touch.time[:4]
            by_year[y].append({
                "pair": pair, "pip": pip, "touch": touch,
                "context": context, "confirm": confirm, "out": out,
                "reaction": reaction,
            })

    years = sorted(by_year.keys())

    # --- Table ---------------------------------------------------------
    print()
    print("Year-profile summary — aggregated across all 7 pairs at M1")
    print()
    header = ("year", "n", "atr_p", "aprR_p", "%wick", "%rej", "%away",
              "%upT", "%dnT", "%flat", "hit@10p", "hit@15p", "hit@20p",
              "fav@2h_p50", "adv@2h_p50")
    print(f"{header[0]:>4}  {header[1]:>5}  "
          f"{header[2]:>6} {header[3]:>7}  "
          f"{header[4]:>5} {header[5]:>5} {header[6]:>5}  "
          f"{header[7]:>5} {header[8]:>5} {header[9]:>5}  "
          f"{header[10]:>7} {header[11]:>7} {header[12]:>7}  "
          f"{header[13]:>11} {header[14]:>11}")

    for y in years:
        rows = by_year[y]
        n = len(rows)
        if n == 0:
            continue

        # For features expressed in pips, normalize per-event by that pair's pip.
        atr_pips = [r["context"].atr / r["pip"] for r in rows]
        approach_r = [r["context"].approach_range / r["pip"] for r in rows]

        wick_frac = mean(r["context"].wick_only for r in rows)
        rej_frac  = mean(r["context"].touch_rejection for r in rows)
        away_frac = mean(r["confirm"].close_away for r in rows)

        trend_up   = sum(1 for r in rows if r["context"].trend == "up") / n
        trend_dn   = sum(1 for r in rows if r["context"].trend == "down") / n
        trend_flat = sum(1 for r in rows if r["context"].trend == "flat") / n

        # Reaction hit rates (max_fav reached target within 2h)
        def hit_at(target_pips):
            hits = 0
            for r in rows:
                thresh = target_pips * r["pip"]
                if any(f >= thresh for f in r["reaction"].fav_profile):
                    hits += 1
            return hits / n * 100

        # Median max fav / adv at 2h (end of window)
        fav_2h = [max(r["reaction"].fav_profile) / r["pip"] for r in rows]
        adv_2h = [max(r["reaction"].adv_profile) / r["pip"] for r in rows]

        print(f"{y:>4}  {n:>5}  "
              f"{median(atr_pips):>5.1f}p {median(approach_r):>6.1f}p  "
              f"{wick_frac*100:>4.1f}% {rej_frac*100:>4.1f}% {away_frac*100:>4.1f}%  "
              f"{trend_up*100:>4.1f}% {trend_dn*100:>4.1f}% {trend_flat*100:>4.1f}%  "
              f"{hit_at(10):>6.1f}% {hit_at(15):>6.1f}% {hit_at(20):>6.1f}%  "
              f"{median(fav_2h):>9.1f}p {median(adv_2h):>9.1f}p")


if __name__ == "__main__":
    main()
