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
- ~~Out-of-sample time slices (H1 vs H2 on the strategy P&L, not just edge).~~ **Done — see below.**

## 2026-07-08 — 10-year OOS split on strategy P&L (the honest test)

Extended history to 10 years (2016-01-01 → 2026-07-08, ~261K M15 bars per pair).
Ran the previously-declared winning config (`wick+drift+away`, 90p target,
30p stop, entry=confirm, worst-case path) split at **2021-01-01**.

### The winner didn't generalize

| Pair | H1 exp | H1 total | H2 exp | H2 total | Verdict |
|---|---:|---:|---:|---:|:--|
| EUR_USD | −5.1 | −271 | +0.2 | +9 | ⚠ FLIP |
| GBP_USD | +7.6 | +410 | +9.3 | +380 | ✓ both+ |
| AUD_USD | −0.9 | −41 | +13.1 | +549 | ⚠ FLIP |
| USD_CAD | +2.4 | +126 | −5.8 | −254 | ⚠ FLIP |
| USD_JPY | +0.8 | +39 | +3.6 | +210 | ✓ both+ |
| EUR_JPY | −6.3 | −345 | +0.4 | +24 | ⚠ FLIP |
| GBP_JPY | −10.0 | −448 | +1.4 | +83 | ⚠ FLIP |

- **Aggregate H1: −530 pips (−1.50 / trade).** Losing.
- **Aggregate H2: +1000 pips (+2.73 / trade).** Winning.
- **5 of 7 pairs FLIPPED signs.** The 5-year winner was regime-driven, not
  a true generalization.

### Robustness sweep — 11 configs × 7 pairs × 2 halves

Ran a full sweep looking for any config that's positive in *both* halves on
aggregate (see `analysis/sweep_oos.py`). Ranked by min(H1 exp, H2 exp):

| Config | min(H1, H2) exp | Both-halves-positive pairs |
|---|---:|---:|
| all events 90/30 (no filter) | **+0.02** ✓ | 2/7 |
| wick+drift 60/30 | −0.40 | 2/7 |
| wick-only 60/30 | −0.90 | 2/7 |
| wick+drift 90/30 | −0.96 | 2/7 |
| 60/30 default (our previous winner!) | −1.19 | 1/7 |
| 90/30 (3:1) | −1.50 | 2/7 |
| 3×ATR / 1.5×ATR | −1.94 | 2/7 |

**Only "all events 90/30" is time-stable on aggregate — and only barely
(min exp +0.02).** Every other config flips signs. This is a strong signal
that our filter machinery was tuned to H2 characteristics.

### Per-pair truth: it's a two-pair strategy

Counting how many of the 10 configs each pair is positive in *both* halves:

| Pair | Configs stable both halves | Notes |
|---|---:|---|
| **GBP_USD** | **7/10** | ROBUST across most configs. 60/30 default gives +7.9/+7.9 — remarkably flat. |
| **AUD_USD** | **4/10** | Robust with `wick` / `wick+drift` / no filter; the `wick+drift+away` variants over-fit. |
| USD_JPY | 3/10 | Config-sensitive; only three combos survive. |
| EUR_JPY | 1/10 | Only marginal (+0.2/+0.0 on all-events). |
| **EUR_USD** | 0/10 | Not profitable in any config both-halves. |
| **USD_CAD** | 0/10 | Same. |
| **GBP_JPY** | 0/10 | Same. |

**GBP_USD and AUD_USD are the only two pairs that survive out-of-sample.**
The "cross-pair validated" narrative from earlier was a 5-year artifact.

### Time-stable strategy candidates

Two candidates emerge from the sweep — each has a config where BOTH halves
are positive:

1. **GBP_USD, `wick+drift+away`, 60/30 target/stop**
   - H1 (2016-2020): +7.9 pips/trade, ~54 trades → +410 pips
   - H2 (2021-2026): +7.9 pips/trade, ~41 trades → +380 pips
   - ~10 trades/year, ~790 pips over 10 years
   - Extremely flat across halves — the strongest signal we have.

2. **AUD_USD, `wick-only` filter, 60/30 target/stop**
   - H1: +2.8 pips/trade, ~104 trades → +291 pips
   - H2: +10.0 pips/trade, ~101 trades → +1010 pips
   - ~20 trades/year, ~1300 pips over 10 years
   - Positive in both halves but H2 nearly 4× the edge → possible regime tailwind.

**Combined portfolio: ~30 trades/year, +2000 pips over 10 years, robust to
the H1/H2 split.** Modest but honest.

### What didn't work

- The `wick+drift+away` filter (our earlier "cross-pair winner") only survives
  for GBP_USD out of sample. Everywhere else it's a regime artifact.
- ATR-scaled thresholds didn't help — they didn't hurt either, but they don't
  fix the fundamental regime dependency.
- Tighter R:R (4:1 via 60/15) is actively bad in H1.
- The 5-year "wick+drift+away is our winner" conclusion was substantially wrong
  when the H1 window was included.

### Open questions

- Realistic spread / slippage costs — GBP_USD ~1p, AUD_USD ~1.5p per round-trip.
  Would eat 1-2 pips of expectancy. GBP_USD's +7.9 is comfortable; AUD_USD's
  H1 +2.8 is marginal after costs.
- Is a rolling-year edge chart different from H1/H2? Might reveal narrower
  windows where even our survivors flip.
- Why did the confirmation filter over-fit? Guessing: `confirm_close_away`
  captured a market microstructure feature (post-touch mean-reversion in
  H2's algo-driven regime) that wasn't as strong pre-2021.
- Can we exploit the fact that AUD_USD prefers simpler filters — is there a
  meta-signal (volatility regime, pair category) that tells us which filter
  to use?

## 2026-07-08 — Reaction study + quick-trade discovery

Motivated by the observation from the H1/H2 plots that "almost every touch
has *some* reaction, even when the level breaks eventually." Built a
reaction analyzer that measures cumulative max fav/adv per bar after entry.

### The level really does react

Cross-pair summary at 2h window (8 M15 bars):

| Pair    | Median fav | Median adv | Hit@10p | Hit@15p | AdvP75@10p |
|---------|-----------:|-----------:|--------:|--------:|-----------:|
| GBP_JPY |     23.8p |     23.2p |     77% |     66% |      19.1p |
| USD_JPY |     16.6p |     16.8p |     64% |     54% |      15.6p |
| EUR_JPY |     16.4p |     16.1p |     68% |     54% |      15.3p |
| GBP_USD |     16.3p |     17.9p |     69% |     53% |      14.8p |
| USD_CAD |     13.4p |     11.9p |     60% |     43% |      11.3p |
| EUR_USD |     10.8p |     12.3p |     53% |     40% |       9.3p |
| AUD_USD |      8.6p |      8.6p |     43% |     31% |       7.8p |

Every pair sees ≥40% probability of the level holding for at least 10 pips
within 2h, and adverse-before-hit stays tight (P75 usually just above the
target size). This confirms the user's hypothesis.

### But path ambiguity at M15 kills tight-threshold backtests

Naïve interpretation: "40% hit rate at 10p → tight 10/15 target/stop must
print money." Actual worst-case backtest at 10/15/8 aggregate: **−1.10p/trade
in H1, −0.85p/trade in H2** — losing.

Testing worst-case vs best-case path_ambiguity assumption:

| Config    | worst H1 | worst H2 | best H1 | best H2 |
|-----------|--------:|--------:|--------:|--------:|
| 10/15/8   |  −1.10  |  −0.85  |  +0.31  |  +0.73  |
| 15/20/8   |  −0.65  |  −0.62  |  +0.53  |  +0.56  |
| 15/20/16  |  −0.61  |  −0.57  |  +0.61  |  +0.61  |
| 20/25/16  |  −0.67  |  −0.45  |  +0.44  |  +0.42  |

Gap is 1.1–1.6 pips per trade. **The true expectancy of a 10p-target
strategy on M15 data cannot be determined without finer granularity.**
Midpoint estimate is roughly breakeven. To honestly evaluate 10-15p
target trades, we need M1 candles during the trade window.

### But we found new OOS-stable pair-specific configs

Even under worst-case path (pessimistic), some pair-config combos survive:

**AUD_USD — 7/8 quick configs OOS-stable.** Best: **30/15/8** (2:1 R:R, 2h max)
- H1: +1.29 pips/trade × 197 trades = +254 pips
- H2: +2.07 pips/trade × 214 trades = +443 pips
- **Total: +698 pips over 10y, ~40 trades/year**

**USD_CAD — 6/8 quick configs OOS-stable.** Best: **25/25/16** (1:1 R:R, 4h max)
- H1: +0.67 × 282 = +189 pips
- H2: +1.95 × 250 = +487 pips
- **Total: +676 pips over 10y, ~50 trades/year**

**GBP_USD — only 1/8 quick configs works.** Its edge lives in the slow 60/30
trades: +790 pips over 10y at ~10 trades/year.

**Other pairs (EUR_USD, JPY pairs) — 0/8 quick configs work** under worst-case
path ambiguity. Their true expectancy is unknown until we get M1 data.

### Emerging portfolio (pair-specific strategies)

| Pair    | Config                            | Style        | Est. 10y P&L |
|---------|-----------------------------------|--------------|-------------:|
| GBP_USD | 60/30 wick+drift+away, 24h max    | Slow bounce  |      +790 p |
| AUD_USD | 30/15/8 all, 2h max               | Quick 2:1    |      +698 p |
| USD_CAD | 25/25/16 all, 4h max              | Balanced 1:1 |      +676 p |
| **Total** |                                   |              |  **+2,164 p** |

~100 trades/year across 3 pairs. All configs OOS-stable at worst-case path
ambiguity. Modest but real.

### Open questions

- ~~**M1 data during trade windows** would resolve path ambiguity for the
  4 pairs currently classified as "unknown".~~ **Done — see M1 section below.**
- The reaction study assumed entry at confirm-bar close. Does entering on
  the touch bar's close (no confirmation wait) change things? Might reveal
  more setups on the "losing" pairs. → **Answered: entry timing is
  *pair-specific* and hugely material at M1.**
- Regime effect: AUD_USD H2 edge is 60% larger than H1. Real signal
  strengthening, or 2020+ tailwind we haven't identified?
- No spread/slippage costs yet. AUD_USD +1.29 H1 becomes marginal after
  1.5p spread; USD_CAD +0.67 H1 is similar.

## 2026-07-08 — Full M1 switch

Backfilled 10 years of M1 candles for all 7 pairs (~26M rows, ~5.5 GB SQLite).
Rescaled every bar-count CLI default by 15× so time semantics match M15:
cooldown 480 → 7200 (5 trading days), forward 96 → 1440 (24h), etc. Set
`default_granularity: M1` in config.

### Path ambiguity essentially eliminated ✓

Worst vs best backtest expectancy gap collapsed from **1.1-1.6 p/trade at M15
to 0.00-0.36 p/trade at M1**. The biggest methodological uncertainty in our
prior work is now resolved.

| Config       | M15 gap | M1 gap |
|--------------|--------:|-------:|
| 60/30/24h    | ~0.0p  | 0.00p |
| 15/20/2h     | +1.19p | +0.27p |
| 30/15/2h     | +1.29p | +0.28p |

### The entry-timing discovery

Our M15 backtester's `entry=confirm` waited 1 M15 bar = **15 min**. At M1
the same knob waits 1 min — barely any confirmation. Adding an
``--entry-offset`` parameter (extra bars to wait after the base entry
bar) revealed that **optimal wait is pair-specific**:

Best pair-config-offset combos, aggregate under worst-case path, OOS-stable
(both H1 and H2 positive):

| Pair    | Config        | Offset   | H1 exp | H2 exp | 10y total |
|---------|---------------|---------|-------:|-------:|----------:|
| **AUD_USD** | 60/30/24h | 15 min | +3.85 | +6.23 | **+2,013 pips** |
| **USD_CAD** | 15/20/4h  |  1 min | +2.06 | +1.07 |    +828 pips |
| **EUR_JPY** | 30/15/2h  | 60 min | +1.37 | +0.76 |    +725 pips *(new)* |
| **GBP_USD** | 30/15/2h  | 60 min | +1.36 | +0.64 |    +686 pips *(new setup)* |

**Combined 4-pair portfolio: ~+4,250 pips over 10 years, ~200 trades/year.**
Nearly 2× the earlier M15 portfolio (~+2,164 pips over 10y).

### What we learned

- **M15's fixed 15-min confirmation forced everyone into the same wait**;
  M1 with tunable offset unlocks pair-specific setups.
- **GBP_USD and EUR_JPY** need a full 60-min wait to filter M1 noise —
  they were "unfit" before because 15 min wasn't enough.
- **AUD_USD** likes 15-min waits — its M15 result was already near-optimal.
- **USD_CAD** wants no wait at all — enter at the touch and go.
- **EUR_USD, USD_JPY, GBP_JPY** still don't produce OOS-stable configs
  even with tuning. Whatever the pattern is, it either doesn't exist for
  them, or requires features we haven't captured.

### Non-path-ambiguity findings

- Analysis wall-clock: `brn touches` on M1 is ~45s per pair (vs 2s at M15).
  Backtest ~45s per pair. Cross-pair sweeps ~10 min. Slower but tractable
  for research.
- Feature semantics shift with granularity — `wick_only`, `touch_shape`,
  ATR, etc. compute on M1 bars now. Not directly comparable to their M15
  meanings. This is a real semantic re-anchor.
- Storage cost: ~200 bytes/row in SQLite → 5.5 GB. Bigger than the
  80 bytes/row estimate I gave the user.

### Open questions

- Entry offset is now a knob per pair — need to formalize this into the
  strategy config, or expose it in a per-pair table users can adjust.
- Do these pair-specific offsets have any physical interpretation
  (typical microstructure reaction time for that pair)? Or is it
  data-fit at 4 pairs × 3 offsets?
- ~~Rolling-year check on the M1 portfolio would tighten confidence~~
  **Done — see rolling section below.**
- **Spread/slippage costs still not modelled.** AUD_USD +3.85 H1 easily
  survives 1.5p spread. USD_CAD +2.06 H1 does too. But borderline pairs
  (EUR_JPY +1.37, GBP_USD +1.36) get eroded.

## 2026-07-08 — Rolling-year and monthly performance check

The H1/H2 split is only two data points. Broke the M1 portfolio down by
calendar year and month to see how it would have actually performed over
time. See `analysis/rolling_check.py` and `data/plots/portfolio_equity.pdf`.

### Yearly P&L

| Year | AUD_USD | USD_CAD | EUR_JPY | GBP_USD | Portfolio | Note |
|------|--------:|--------:|--------:|--------:|----------:|------|
| 2016 | +353 | +207 | +209 | +453 | **+1,222** | best year |
| 2017 | +155 | +241 |   +1 |   −8 |    +389 | |
| 2018 |  +55 | +154 | +159 |  −72 |    +296 | |
| **2019** |  −24 |  +25 | +106 | −153 |    **−47** | **losing year** |
| 2020 | +185 |  −59 |  −63 | +271 |    +334 | |
| 2021 | +284 |  +17 |  +42 |  +28 |    +371 | |
| 2022 | +446 |  +68 | +239 | +268 | **+1,021** | 2nd best |
| 2023 | +202 |  +76 | +142 |  +52 |    +472 | |
| 2024 | +130 | +125 | **−186** |  +69 |    +138 | EUR_JPY bad |
| 2025 | +226 | **−107** | +124 | **−179** |     +65 | thin |
| 2026 (H1) |   +2 |  +82 |  −48 |  −44 |     −8 | losing YTD |
| **Total** | **+2,013** | **+828** | **+725** | **+686** | **+4,253** | |

### Monthly summary

- **127 total months, 82 winning (64.6%), 45 losing (35.4%).**
- Worst month: 2022-06 (−174 pips). Best: 2022-09 (+483 pips).
- Best 5 months alone = +1,599 pips (38% of total from 4% of months).
  Fat right tail — the strategy has upside skew but sit-through periods.

### Concerning patterns

1. **Two exceptional years carry ~half the total.** 2016 (+1,222) + 2022
   (+1,021) = +2,243 = 53% of the 10y total. Remove those and the strategy
   makes ~+2,010 over 8 years, i.e. ~+250 pips/year.

2. **Recent decay is real.** 2024 (+138) → 2025 (+65) → 2026 YTD (−8).
   Per-trade expectancy has been degrading: from ~+1.85 average → +0.32
   (2025) → ~−0.09 (2026 partial). Could be regime, could be crowding
   as this pattern gets exploited, could be noise.

3. **Losing streaks exist within each pair.** GBP_USD has losing years
   2017, 2018, 2019, 2025. AUD_USD had a small loss in 2019. USD_CAD had
   losing years 2020 and 2025. EUR_JPY had −186 in 2024. Anyone trading
   a single pair would need serious tolerance.

4. **Portfolio drawdown periods:** 2018-Q4 through mid-2019 (~4-6 months
   negative), 2020-Q1 (COVID crash), late 2019 similarly rough. Peak
   drawdown from the equity curve looks like ~500 pips (visible in the
   PDF, roughly 2018 → 2020).

### What survives this test

- **10-year total is positive**, both halves are positive under the H1/H2
  split. The signal is real.
- **Yearly win rate: 9 winning years, 2 losing years (2019 and 2026 partial)**.
- **Monthly win rate 65%** with fat right tail on the winners.
- **Diversification helps** — no year had all 4 pairs deeply negative;
  the losers always had at least one strong offsetter.

### But we should worry about

- **Regime decay in 2024-2026.** Even if the OOS split at 2021 shows both
  halves positive, the recent trend is a warning. Might be worth
  re-splitting at 2024 to check whether the strategy still works on
  "very recent" data.
- ~~**Concentration in 2016 and 2022 wins.** If those were regime-specific
  (say, particular volatility environments), we should identify what
  made those years work.~~ **Answered — see next section.**
- **Per-trade expectancy is small (+1.85 avg).** After realistic spread
  costs (~1-1.5p per trade), the edge is much thinner. AUD_USD +5.10
  per trade is comfortable but the others (USD_CAD +1.59, EUR_JPY +1.02,
  GBP_USD +1.03) get badly eroded.

## 2026-07-08 — What made the winner years special

Yearly base-rate profile across all 7 pairs (see `analysis/year_profile.py`)
showed 2016 and 2022 share a clear signature.

### Year-by-year market character

| Metric              | 2016  | 2017 | 2018 | 2019 | 2020 | 2021 | 2022  | 2023 | 2024 | 2025 |
|---------------------|------:|-----:|-----:|-----:|-----:|-----:|------:|-----:|-----:|-----:|
| Touches (all 7)     |  625  | 377  | 363  | 331  | 415  | 291  |  570  | 408  | 454  | 398  |
| Median ATR (pips)   |  5.4  | 3.4  | 3.3  | 3.3  | 3.4  | 2.3  |  4.6  | 3.8  | 4.3  | 3.6  |
| Approach range      | 36.7  | 23.2 | 20.9 | 22.2 | 23.1 | 15.4 | 29.9  | 24.0 | 28.1 | 22.9 |
| Hit @10p / 2h       | 74%   |  67% |  65% |  60% |  69% |  54% |  74%  |  66% |  67% |  69% |
| Median fav @2h      | 22p   | 14p  | 15p  | 13p  | 19p  | 11p  |  22p  | 15p  | 18p  | 16p  |

Feature distributions (`wick_only` ~45-57%, `touch_rejection` ~10-16%,
`close_away` ~47-57%, `trend` ~85-98% flat) are **stable** across all
years — none of the candlestick / trend features differentiate winners
from losers. What varies is **market volatility**.

- 2016 and 2022 have the highest median ATR (5.4p, 4.6p) and the widest
  approach ranges (37p, 30p).
- Their hit rate at a 10p target within 2h is **74%** — 20 percentage points
  above the 54% of 2021.
- Their median max fav within 2h is **22p** — nearly double the 11-13p of
  weak years 2019, 2021, 2026.

**The strategy's edge is a volatility-regime effect**, not a
candlestick-pattern effect. High-vol years produce ~2× the reactions.

### Per-pair ATR-quartile analysis

For each portfolio pair, grouped its trades by the ATR-at-touch quartile:

| Pair      | Q1 (low vol) | Q2       | Q3       | Q4 (high vol) |
|-----------|-------------:|---------:|---------:|--------------:|
| **AUD_USD** | +2.60 exp  | +3.07    | +4.86    | **+9.92**     |
| USD_CAD   | **+2.61**   | +2.20    | +1.82    | −0.28         |
| EUR_JPY   | −0.37       | +0.18    | **+2.50** | +1.78        |
| GBP_USD   | +0.60       | +0.46    | −0.51    | **+3.58**     |

Preferences are **pair-specific and config-specific**:

- **AUD_USD** (60p target / 30p stop / 24h): monotonically loves high
  vol — Q4 is 4× the Q1 edge. Big targets need big moves.
- **USD_CAD** (15p target / 20p stop / 4h): monotonically hates high
  vol — Q4 actually loses. Tight thresholds get chopped by noise.
- **EUR_JPY, GBP_USD**: mixed preferences with local dips.

### ATR-filtered portfolio (in-sample fit — noted below)

Applying the favorable bucket per pair:

| Metric              | Unfiltered   | ATR-filtered |
|---------------------|-------------:|-------------:|
| Trades              | 2,293        | 1,344 (−41%) |
| Per-trade expectancy| +1.85 pips  | **+2.50 pips (+35%)** |
| Total pips (10y)    | +4,253       | +3,366 (−21%) |
| H1 expectancy       | +2.79        | +3.10        |
| H2 expectancy       | +2.02        | +2.00        |

Both halves stay positive, per-trade edge lifts by 35%, but total P&L
shrinks 21% (fewer trades). Straight-up **more selective, more edge, less volume** trade-off.

### Caveat: data leakage

The favorable ATR bucket per pair was chosen using the full 10y history
we're testing on — this is in-sample fit. A real deployment would need
a **rolling ATR percentile** computed from data available only before
each trade decision. The finding that per-pair vol preferences exist
is real; the specific per-trade edges are optimistic.

### Practical implications

1. **AUD_USD is a "high-vol strategy"** and should probably always be
   active when volatility is elevated. Consider sizing it up.
2. **USD_CAD is a "low-vol strategy"** — pause in high-vol regimes.
3. **A rolling ATR-percentile filter per pair** would formalize this and
   avoid the leakage above.
4. **Global market volatility indicator** (e.g. sum of pair ATRs, VIX
   proxy) might predict which config-set to run — dynamic switching.
