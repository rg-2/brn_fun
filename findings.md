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

- Are these filters stable across time (e.g. does 2021–2023 look like 2024–2026)? See task #18 (parameter sweep) and time-slicing.
- Does the edge survive with realistic entry, stop, and target rules (task: backtester skeleton)?
- What if we tune bounce/break thresholds per pair (e.g. use ATR-scaled thresholds instead of fixed 30 pips)?
- Add candlestick pattern detection for **bar N+2** (a second confirmation).
