"""How does price react to a round-number touch, bar by bar after entry?

The touches analyzer describes *whether* a bounce/break happens over a full
24-hour window. The reaction analyzer describes *when and how* the price
moves in the first few hours — the data you need to design tight, quick
trade management (fast targets, snug stops, hard time exits).

For each candidate event, entering at the close of the confirmation bar
(``touch.idx + 1``) with the same direction convention as the backtester
(up-touch → short, down-touch → long), we build two cumulative curves over
the forward window:

- ``max_fav[k]`` — the max favorable price move seen in the first k bars.
- ``max_adv[k]`` — the max adverse price move seen in the first k bars.

From these curves per event, per-pair aggregates answer:

1. What are typical fav/adv magnitudes at 30 min, 1 h, 2 h, 4 h, 8 h?
2. For target T pips, what fraction of events reach T within N bars?
3. For events that reach T, what's the distribution of max adverse taken
   *before* T is reached — i.e. what stop size covers X% of the eventual winners?

The core insight for the user's "many of these can be winners" hypothesis:
if P90 of "adverse before +15p" is 12p, a 15p stop will let 90% of the
events that eventually reach +15p do so without stopping out prematurely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .analyze import Confirmation, Context, Touch
from .db import Candle


@dataclass(frozen=True, slots=True)
class Reaction:
    """One event's forward-window profile."""

    entry_idx: int
    entry_time: str
    entry_price: float
    direction: str            # "long" or "short"
    level: float
    fav_profile: tuple[float, ...]  # cumulative max favorable per bar offset (1..N)
    adv_profile: tuple[float, ...]  # cumulative max adverse per bar offset (1..N)


def reaction_profile(
    bars: Sequence[Candle],
    entry_idx: int,
    direction: str,
    entry_price: float,
    forward_bars: int,
) -> tuple[list[float], list[float]]:
    """Return (fav_profile, adv_profile) in price units.

    Each list has one entry per bar past entry (1..forward_bars). Truncates
    if the entry is too close to the end of the data.
    """
    fav_profile: list[float] = []
    adv_profile: list[float] = []
    running_max_fav = 0.0
    running_max_adv = 0.0
    for offset in range(1, forward_bars + 1):
        j = entry_idx + offset
        if j >= len(bars):
            break
        bar = bars[j]
        if direction == "long":
            fav = max(0.0, bar.high - entry_price)
            adv = max(0.0, entry_price - bar.low)
        else:  # short
            fav = max(0.0, entry_price - bar.low)
            adv = max(0.0, bar.high - entry_price)
        running_max_fav = max(running_max_fav, fav)
        running_max_adv = max(running_max_adv, adv)
        fav_profile.append(running_max_fav)
        adv_profile.append(running_max_adv)
    return fav_profile, adv_profile


def compute_reactions(
    bars: Sequence[Candle],
    events: Iterable[tuple[Touch, Context, Confirmation, object]],
    *,
    forward_bars: int = 32,
    entry: str = "confirm",
) -> list[Reaction]:
    """Compute a Reaction per event using the same entry rules as the backtester."""
    out: list[Reaction] = []
    for touch, _context, confirm, _outcome in events:
        entry_idx = touch.idx if entry == "touch" else touch.idx + 1
        # If we need the confirm bar but it doesn't exist, skip — matches
        # backtester's behavior of not entering.
        if entry == "confirm" and not confirm.present:
            continue
        if entry_idx >= len(bars):
            continue
        entry_price = bars[entry_idx].close
        direction = "short" if touch.direction == "up" else "long"
        fav, adv = reaction_profile(bars, entry_idx, direction, entry_price, forward_bars)
        if not fav:
            continue
        out.append(Reaction(
            entry_idx=entry_idx,
            entry_time=bars[entry_idx].time,
            entry_price=entry_price,
            direction=direction,
            level=touch.level,
            fav_profile=tuple(fav),
            adv_profile=tuple(adv),
        ))
    return out


def _percentiles(values: list[float], ps: list[int]) -> list[float]:
    """Return the requested percentile values from a list of floats."""
    if not values:
        return [0.0 for _ in ps]
    xs = sorted(values)
    out: list[float] = []
    for p in ps:
        # Nearest-rank; fine for a few-hundred-sample analysis.
        k = max(0, min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1)))))
        out.append(xs[k])
    return out


def favorable_percentiles(
    reactions: list[Reaction],
    windows: list[int],
    ps: list[int],
) -> dict[int, list[float]]:
    """Return {window_bars: [P{p} of max_fav at that window]} in price units."""
    out: dict[int, list[float]] = {}
    for w in windows:
        vals = [r.fav_profile[w - 1] for r in reactions if len(r.fav_profile) >= w]
        out[w] = _percentiles(vals, ps)
    return out


def adverse_percentiles(
    reactions: list[Reaction],
    windows: list[int],
    ps: list[int],
) -> dict[int, list[float]]:
    """Return {window_bars: [P{p} of max_adv at that window]} in price units."""
    out: dict[int, list[float]] = {}
    for w in windows:
        vals = [r.adv_profile[w - 1] for r in reactions if len(r.adv_profile) >= w]
        out[w] = _percentiles(vals, ps)
    return out


@dataclass(frozen=True, slots=True)
class TargetStats:
    """Hit-rate and adverse-before-hit stats for one (target, window) cell."""

    hit_rate: float            # fraction of events reaching target within window
    n_hits: int                # count of events that hit
    adv_before_hit_p50: float  # median max adverse before target fires (only for hitters)
    adv_before_hit_p75: float
    adv_before_hit_p90: float


def target_stats(
    reactions: list[Reaction],
    target: float,        # price units (e.g. 15 * pip)
    window_bars: int,
) -> TargetStats:
    """For target T over the first N bars: hit rate and adverse-before-hit stats."""
    hits = 0
    adv_before: list[float] = []
    n_total = 0
    for r in reactions:
        # Only count events that had at least ``window_bars`` of forward data.
        if len(r.fav_profile) < window_bars:
            continue
        n_total += 1
        hit_bar: int | None = None
        for k in range(window_bars):
            if r.fav_profile[k] >= target:
                hit_bar = k
                break
        if hit_bar is not None:
            hits += 1
            # Max adverse observed in bars 1..hit_bar+1 (inclusive).
            adv_before.append(r.adv_profile[hit_bar])

    hit_rate = hits / n_total if n_total else 0.0
    p50, p75, p90 = _percentiles(adv_before, [50, 75, 90]) if adv_before else (0.0, 0.0, 0.0)
    return TargetStats(
        hit_rate=hit_rate,
        n_hits=hits,
        adv_before_hit_p50=p50,
        adv_before_hit_p75=p75,
        adv_before_hit_p90=p90,
    )


def suggested_stop_for_target(
    reactions: list[Reaction],
    target: float,
    window_bars: int,
    survive_frac: float = 0.85,
) -> float:
    """A stop that lets ``survive_frac`` of the eventual winners survive.

    Returns 0 if no events hit the target.
    """
    if not (0.0 < survive_frac < 1.0):
        raise ValueError("survive_frac must be in (0, 1)")
    adv_before: list[float] = []
    for r in reactions:
        if len(r.fav_profile) < window_bars:
            continue
        for k in range(window_bars):
            if r.fav_profile[k] >= target:
                adv_before.append(r.adv_profile[k])
                break
    if not adv_before:
        return 0.0
    p = int(round(survive_frac * 100))
    return _percentiles(adv_before, [p])[0]
