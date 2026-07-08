# Analysis findings

Running log of what the touch analyzer has revealed. Add sections chronologically.

## 2026-07-07 — First cross-pair validation

**Data:** 5 years of M15 (~125K bars each) across 7 pairs.
**Method:** `brn touches --tier figure` per pair (JPY pairs use `--pip 0.01 --grid 1.0`).
**Filter parameters:** cooldown=480 bars (5 trading days), forward=96 bars (24h), bounce/break thresholds=30 pips.

### Baseline (no filter)

| Pair | Touches | Bounce% | Edge |
|---|---:|---:|---:|
| EUR_USD | 224 | 31% | −8 |
| GBP_USD | 282 | 30% | −11 |
| **AUD_USD** | 192 | **44%** | **+11** |
| USD_CAD | 228 | 33% | −1 |
| USD_JPY | 383 | 21% | −7 |
| EUR_JPY | 397 | 28% | +6 |
| GBP_JPY | 485 | 23% | −5 |

- Baseline is negative or breakeven for most pairs — naive "trade every touch" doesn't work.
- AUD_USD is the standout baseline.
- JPY pairs are dominated by whipsaw ("both" outcomes 44–55%): their volatility routinely takes out 30-pip thresholds in both directions inside 24h.

### Signals that generalize across pairs

**`wick_only`** — touch that only wicks the level (body stays away) — **positive edge on all 7 pairs**. Range: +2 (GBP_JPY) to +25 (EUR_JPY).

**`confirm_close_away`** — bar after the touch closes further from the level — **positive edge on all 7**. Range: +0.1 (GBP_USD) to +28 (AUD_USD).

**`touch_rejection`** — touch bar is a pin against the approach direction, or a doji — **positive edge on 6/7**. Only USD_CAD is mildly negative (−2).

### Signals that DON'T generalize

**Trend direction (`trend = down` → bounce)** was strong on EUR_USD (+18) but **did not hold** on other pairs. USD_CAD, USD_JPY, GBP_JPY all show the opposite (trend=up better). My EUR_USD hypothesis was regime-specific.

**`alignment = against`** (counter-trend touch): mostly positive but weak — 6/7 positive, only GBP_USD negative. Not strong enough to build on alone.

**Approach speed** (drift vs sprint): drift is generally better than sprint, but exceptions exist (AUD_USD sprint = **+43** edge, 67% bounce). Pair-dependent.

### Combined filter: `wick_only AND NOT sprint AND confirm_close_away`

| Pair | n | Bounce% | Edge |
|---|---:|---:|---:|
| USD_JPY | 51 | 41% | **+41** |
| AUD_USD | 35 | **63%** | +34 |
| GBP_USD | 37 | 51% | +32 |
| EUR_JPY | 63 | 51% | +26 |
| EUR_USD | 52 | 46% | +15 |
| USD_CAD | 43 | 37% | +14 |
| GBP_JPY | 52 | 27% | −5 |

- **6/7 pairs positive edge under the combined filter.**
- GBP_JPY is the exception — likely the 30-pip threshold is too tight for its typical bar sizes; needs pair-specific tuning.
- USD_JPY, AUD_USD, GBP_USD, EUR_JPY look like the pairs to start a strategy on.

### Open questions / next work

- ~~Are these filters stable across time (e.g. does 2021–2023 look like 2024–2026)?~~ **Answered below in the time-stability sweep.**
- Does the edge survive with realistic entry, stop, and target rules (task: backtester skeleton)?
- What if we tune bounce/break thresholds per pair (e.g. use ATR-scaled thresholds instead of fixed 30 pips)?
- Add candlestick pattern detection for **bar N+2** (a second confirmation).
- A rolling-year edge chart would be more rigorous than a single midpoint split.

## 2026-07-07 — Time-stability sweep

Split each pair's touches at **2024-01-01** (calendar midpoint of the 5y window). Compute edges per half.

### Individual signals

- **`wick_only`** — same-sign positive in both halves for 6/7 pairs. Only GBP_JPY flipped (−10 → +14). Robust.
- **`confirm_close_away`** — same-sign positive in both halves for 6/7. Only GBP_USD flipped (−4 → +5, marginal). Robust.
- **`touch_rejection`** — 4/7 flipped signs, but per-half n=6–36. Noise-dominated at these sample sizes; only trust it inside combined filters.
- **Baseline** — 4/7 flipped signs. Confirms that unfiltered "touch every level" is coin-flip.

### Combined filter (wick + not-sprint + close_away)

| Pair | H1 edge | H2 edge | Note |
|---|---:|---:|---|
| EUR_USD | +27 | +9 | positive, decayed |
| GBP_USD | +54 | +4 | positive, big decay |
| AUD_USD | +41 | +29 | strong both halves |
| USD_CAD | +20 | +4 | positive, decayed |
| USD_JPY | +23 | **+63** | strong; better recently |
| EUR_JPY | +4 | **+51** | weak H1, strong H2 |
| GBP_JPY | **−39** | **+38** | **FLIPPED** |

- **Aggregate H1: n=173, bounce 42%, edge +14. H2: n=160, bounce 47%, edge +30.** Both halves positive; H2 is stronger.
- **6/7 pairs same-sign positive** in both halves. Only GBP_JPY flipped.

### Regime observation

- All USD-quoted pairs (EUR_USD, GBP_USD, USD_CAD) show **H2 edge < H1 edge** (decay).
- All JPY-quoted pairs (USD_JPY, EUR_JPY, GBP_JPY) show **H2 edge > H1 edge** (strengthening).

Plausible macro trigger: **BoJ policy normalization began March 2024** after decades of near-zero rates. JPY pairs took on more directional character afterward — bigger H2 sample sizes with cleaner rejections at round levels. The GBP_JPY H1→H2 flip is the extreme case.

### Verdict

- The **wick_only + close_away** core is a time-stable pattern, not a curve-fit artifact.
- Individual pair edges drift with regime — expected.
- Confidence to build a backtester on top of these signals is warranted.

### Open questions

- Rolling-year edge over the 5y window would be more granular than a single midpoint split — worth doing next.
- If backfilled to 10y we could split into 4 quarters and see how deep the stability goes.
- GBP_JPY's flip demands a pair-specific explanation before including it in any live strategy.

## 2026-07-07 — ATR-scaled thresholds and a metric-caveat learning

Added `--bounce-atr` / `--break-atr` multipliers so bounce/break thresholds scale
with per-touch ATR instead of being fixed pips. Then re-ran the full filter
across all pairs at 2×ATR.

### The finding: our `edge` metric is threshold-invariant

Under the full filter, edges at 2×ATR are **identical** to 30p-fixed edges,
pair by pair. Reason: `edge = avg_favorable − avg_adverse` measures the raw
max-excursion magnitudes in the forward window, which are independent of the
classification threshold. Threshold only decides how each event is *labeled*
(bounce/break/both/chop) — it doesn't change the underlying magnitudes we're
averaging.

### What ATR-scaling *did* affect

- **Cross-pair bounce% spread narrowed** — 27–63% at fixed 30p → 31–57% at
  2×ATR. Some real normalization.
- **GBP_JPY improved** — bounce rate 27% → 31%. Small lift, still lowest.
- **Classification distribution shifted** — much more "both", almost no
  "chop" at 2×ATR (tighter thresholds catch more events on both sides).

### Bigger lesson

The `edge` metric has been *descriptive* (average size of moves), not
*evaluative* (what a real strategy would earn). Two-and-a-half sessions
of "edge" numbers all reflected pure signal in the events *selected*, not
in any particular threshold choice.

The real test is a backtester with actual entry/target/stop rules — the
threshold matters when the target/stop are *simulated*, because path
dependency (which one hits first) then matters.

### Verdict

- ATR-scaled thresholds are available in the CLI now and useful for
  cross-pair distribution comparison.
- They do **not** change our confidence in the previous edge findings —
  those were already about the events, not the thresholds.
- Next real step to evaluate the filter is a backtester.

## 2026-07-07 — Backtester reality check

Built a bar-by-bar target/stop simulator. Applied to our best cross-pair
filter (`wick + not-sprint + close_away`) with defaults 60p target,
30p stop, entry at close of the confirmation bar, max_bars=96 (24h),
worst-case path assumption.

### Descriptive edge did NOT translate directly to strategy P&L

| Pair | Descriptive edge | Backtest expectancy (60/30) |
|---|---:|---:|
| EUR_USD | +15 | −0.6 |
| GBP_USD | +32 | +7.1 |
| AUD_USD | +34 | +9.9 |
| USD_CAD | +14 | −1.5 |
| USD_JPY | +41 | −0.4 |
| EUR_JPY | +26 | −2.5 |
| GBP_JPY | −5 | −4.0 |

Only 2/7 pairs profitable at the default 60/30. Path dependency + worst-case
ambiguity assumption cost us most of the theoretical edge — the descriptive
metric was measuring max excursion in the 24h window, not what you actually
capture with fixed target/stop rules.

### Config sweep across pairs

| Config (wick+drift+away filter, path=worst) | Pairs profitable | Agg expect | Total pips |
|---|:-:|---:|---:|
| 60/30 (default) | 2/7 | +0.39 | +129 |
| 60/30 best-case ambiguity | 2/7 | +0.66 | +219 |
| 30/30 (1:1 R:R) | 4/7 | +1.66 | +552 |
| **90/30 (3:1 R:R)** | **5/7** | **+2.54** | **+847** |
| 60/15 (4:1 R:R) | 2/7 | +0.11 | +37 |
| 3×ATR / 1.5×ATR | 4/7 | +0.41 | +137 |
| 2×ATR / 1×ATR | 4/7 | +0.91 | +303 |
| wick-only, 60/30 | 4/7 | +0.31 | +358 |

### Current best: 90p target / 30p stop with wick+drift+away

- 5/7 pairs profitable, ~67 trades/year, +2.54 pips/trade expectancy
- +847 pips total over 5 years, all 7 pairs combined
- Losers: USD_CAD (−5.2), GBP_JPY (−1.8) — both mild
- USD_CAD dislikes the filter at any target/stop tried; pair-specific issue.
- GBP_JPY improves under **30/30** (+2.3) — high volatility wants tighter targets.

### Non-obvious takeaways

- Best-case ambiguity barely helps (+0.66 vs +0.39). Path assumption isn't the bottleneck.
- 4:1 R:R (60/15) is actively bad — too many stopouts.
- Wider targets (90p) with same 30p stop = decisive winner.
- Pure `wick-only` filter fires 3.5× more trades at similar per-trade expectancy — bigger total (+358) but looser risk.
- **No single config is best for all pairs.** GBP_JPY wants tight targets; USD_CAD wants a different filter (or exclusion).

### Open questions

- Per-pair custom target/stop (or ATR-scaled with pair-specific multipliers) —
  is there a global rule that works for all, or do we accept pair-tailoring?
- Realistic **spread** and **slippage** costs — none included yet. Would eat ~1p per trade in a major.
- Time-in-market: 90/30 win trades hold much longer than losses. That skews
  the equity-curve dynamics.
- One-position-at-a-time vs overlapping: currently all events trade.
- Trend / session / hour filters within the winning combo — do they help further?
- Out-of-sample time slices (H1 vs H2 on the strategy P&L, not just edge).
