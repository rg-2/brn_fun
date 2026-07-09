"""Walk-forward test of a rolling ATR-percentile filter.

Step 1: For each event, compute its ATR percentile among all events in
        the trailing 365-day window for that pair (only past data — no
        forward peeking).
Step 2: On H1 data (2016 → 2020) only, sweep percentile filter bands
        per pair; pick the band that maximizes H1 total pips.
Step 3: Apply the H1-chosen band to H2 (2021 → 2026); report the honest
        OOS expectancy and total.

If the H2 numbers match or beat the earlier in-sample-fit analysis, the
volatility-regime signal is a real, tradable pattern. If they collapse,
the earlier +35% edge boost was mostly data leakage.
"""
from __future__ import annotations

import pickle
import sys
from bisect import bisect_left, insort
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.backtest import backtest_touches, summarize_trades  # noqa: E402

SPLIT = "2021-01-01"
ROLLING_WINDOW_M1 = 365 * 24 * 60  # 1 year in M1 bars = 525,600

# Same portfolio configs.
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


def rolling_atr_percentile(events, window_bars: int) -> list[float]:
    """Return each event's ATR percentile (0..1) among prior events in window.

    Events must be in chronological order (analyze() output already is).
    An event with no prior events in window returns 0.5 (neutral).
    """
    percentiles: list[float] = []
    # Sliding window: sorted list of ATRs from events still within range,
    # plus the touch.idx of each so we can drop old ones.
    window_sorted: list[float] = []  # sorted ATRs of prior events
    window_pending: list[tuple[int, float]] = []  # (touch_idx, atr) in event order

    for t, c, cf, out in events:
        # Drop events that fell out of the window.
        while window_pending and t.idx - window_pending[0][0] > window_bars:
            _, old_atr = window_pending.pop(0)
            i = bisect_left(window_sorted, old_atr)
            if i < len(window_sorted) and window_sorted[i] == old_atr:
                window_sorted.pop(i)

        # Compute percentile of this event's ATR among prior events.
        atr = c.atr
        if window_sorted:
            rank = bisect_left(window_sorted, atr)
            # Fraction of prior events with strictly lower ATR.
            percentiles.append(rank / len(window_sorted))
        else:
            percentiles.append(0.5)

        # Add this event to the window for future events.
        insort(window_sorted, atr)
        window_pending.append((t.idx, atr))

    return percentiles


def run_backtest_with_mask(bars, events, cfg, mask):
    """Backtest with only mask[i]=True events."""
    kept = [e for e, keep in zip(events, mask) if keep]
    trades = backtest_touches(
        bars, kept, pip=cfg["pip"], filter_name="all", entry="confirm",
        entry_offset=cfg["entry_offset"],
        target_pips=cfg["target_pips"], stop_pips=cfg["stop_pips"],
        max_bars=cfg["max_bars"], path_ambiguity="worst",
    )
    return trades


BANDS = [
    ("all",            0.0, 1.01),
    ("low  (0-25)",    0.0, 0.25),
    ("mid1 (25-50)",   0.25, 0.50),
    ("mid2 (50-75)",   0.50, 0.75),
    ("high (75-100)",  0.75, 1.01),
    ("bot3 (0-75)",    0.0, 0.75),
    ("top3 (25-100)",  0.25, 1.01),
    ("mid  (25-75)",   0.25, 0.75),
    ("ext  (0-25 & 75-100)", None, None),  # both extremes
]


def apply_band(pcts, band):
    lo, hi = band
    if lo is None:
        # extremes band
        return [(p < 0.25 or p >= 0.75) for p in pcts]
    return [lo <= p < hi for p in pcts]


def main() -> None:
    with open("data/events_m1_cache.pkl", "rb") as f:
        per_pair = pickle.load(f)

    print(f"Rolling window: {ROLLING_WINDOW_M1:,} M1 bars = 1 calendar year")
    print(f"H1: pre-{SPLIT}   H2: post-{SPLIT}")
    print()

    portfolio_h1 = portfolio_h2 = 0.0
    portfolio_h1_n = portfolio_h2_n = 0

    for cfg in PORTFOLIO:
        pair = cfg["pair"]
        bars, events, pip = per_pair[pair]

        pcts = rolling_atr_percentile(events, ROLLING_WINDOW_M1)
        # Split events into H1 / H2.
        h1_idx = [i for i, e in enumerate(events) if e[0].time < SPLIT]
        h2_idx = [i for i, e in enumerate(events) if e[0].time >= SPLIT]

        # For each band, backtest H1 events and record total pips.
        # Choose the band with the best H1 total. Then apply to H2.
        print(f"--- {pair} ---   {len(h1_idx)} H1 events, {len(h2_idx)} H2 events")
        best_band = None
        best_h1_total = float("-inf")
        band_results = []

        for name, lo, hi in BANDS:
            mask_all = apply_band(pcts, (lo, hi))
            # Restrict to H1 events
            h1_events = [events[i] for i in h1_idx if mask_all[i]]
            trades = backtest_touches(
                bars, h1_events, pip=pip, filter_name="all", entry="confirm",
                entry_offset=cfg["entry_offset"],
                target_pips=cfg["target_pips"], stop_pips=cfg["stop_pips"],
                max_bars=cfg["max_bars"], path_ambiguity="worst",
            )
            s = summarize_trades(trades, pip=pip)
            band_results.append((name, s))
            if s["total_pips"] > best_h1_total and s["n"] >= 20:
                # Require at least 20 trades to trust the band.
                best_h1_total = s["total_pips"]
                best_band = (name, (lo, hi))

        # Print all H1 band results
        for name, s in band_results:
            marker = "  *" if best_band and name == best_band[0] else "   "
            print(f"    H1 band {name:<22}  n={s['n']:>4}  "
                  f"exp={s['expectancy_pips']:>+6.2f}  "
                  f"total={s['total_pips']:>+7.0f}{marker}")

        if best_band is None:
            print(f"    (no band produced 20+ trades in H1; using 'all')")
            best_band = ("all", (0.0, 1.01))

        # Apply chosen band to H2
        mask_all = apply_band(pcts, best_band[1])
        h1_events = [events[i] for i in h1_idx if mask_all[i]]
        h2_events = [events[i] for i in h2_idx if mask_all[i]]
        s_h1 = summarize_trades(
            backtest_touches(bars, h1_events, pip=pip, filter_name="all",
                              entry="confirm", entry_offset=cfg["entry_offset"],
                              target_pips=cfg["target_pips"], stop_pips=cfg["stop_pips"],
                              max_bars=cfg["max_bars"], path_ambiguity="worst"),
            pip=pip,
        )
        s_h2 = summarize_trades(
            backtest_touches(bars, h2_events, pip=pip, filter_name="all",
                              entry="confirm", entry_offset=cfg["entry_offset"],
                              target_pips=cfg["target_pips"], stop_pips=cfg["stop_pips"],
                              max_bars=cfg["max_bars"], path_ambiguity="worst"),
            pip=pip,
        )
        print(f"    → chosen band: {best_band[0]}   "
              f"H1: n={s_h1['n']} exp={s_h1['expectancy_pips']:+.2f} tot={s_h1['total_pips']:+.0f}  "
              f"H2 (OOS): n={s_h2['n']} exp={s_h2['expectancy_pips']:+.2f} tot={s_h2['total_pips']:+.0f}")

        portfolio_h1 += s_h1["total_pips"]
        portfolio_h2 += s_h2["total_pips"]
        portfolio_h1_n += s_h1["n"]
        portfolio_h2_n += s_h2["n"]
        print()

    print("=== Combined portfolio ===")
    if portfolio_h1_n and portfolio_h2_n:
        print(f"H1 (in-sample): n={portfolio_h1_n} "
              f"exp={portfolio_h1/portfolio_h1_n:+.2f} tot={portfolio_h1:+.0f}")
        print(f"H2 (OOS):       n={portfolio_h2_n} "
              f"exp={portfolio_h2/portfolio_h2_n:+.2f} tot={portfolio_h2:+.0f}")
    print()
    print("Compare unfiltered portfolio: H1 n=1129 exp=+2.79 tot=+3149,  H2 n=1164 exp=+2.02 tot=+2352,  total=+4253")
    print("Compare in-sample fit filter: H1 n=622  exp=+3.10 tot=+1926,  H2 n=722  exp=+2.00 tot=+1441,  total=+3367")


if __name__ == "__main__":
    main()
