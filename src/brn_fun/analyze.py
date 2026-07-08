"""Round-number touch detection and outcome tagging.

Given a chronological list of bars, find every "first touch in a while" of a
round-number price level and characterize what happened in the forward window.

Definitions (see also the CLI defaults):

- **Round level**: a price at spacing ``grid`` (e.g. 0.01 = every 100 pips for
  EUR/USD). Different ``grid`` values give different tiers of "roundness"
  (0.10 = handle, 0.05 = half, 0.01 = figure).
- **Touch**: the bar's ``[low, high]`` range includes the level.
- **First in a while**: the level was not touched by any bar in the previous
  ``cooldown_bars`` bars.
- **Direction**: ``"up"`` if the prior bar closed below the level (price was
  rising toward it), ``"down"`` if it closed above.
- **Favorable excursion (bounce size)**: the maximum distance price moved
  *away* from the level in the reversal direction, over the forward window.
- **Adverse excursion (break size)**: the maximum distance price moved
  *through* the level, continuing past it, over the forward window.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Iterator, Literal, Sequence

from .db import Candle

Direction = Literal["up", "down"]
Outcome = Literal["bounce", "break", "both", "chop"]


@dataclass(frozen=True, slots=True)
class Touch:
    """A single 'first-touch-in-a-while' event."""

    idx: int                    # index into the bars sequence
    time: str                   # bar time (RFC3339)
    level: float                # the round-number level touched
    direction: Direction        # inferred approach direction
    cooldown_bars: int | None   # bars since previous touch of this level (None = first ever)


@dataclass(frozen=True, slots=True)
class Outcome_:
    """Post-touch characterization over a forward window."""

    favorable: float            # max reversal away from level (positive)
    adverse: float              # max continuation past level (positive)
    close_after: float          # close of the last bar in the window
    close_dist: float           # signed: close_after - level
    window_bars: int            # actual bars in the forward window (may be < forward_bars near tail)
    tag: Outcome


def _round_levels_in(low: float, high: float, grid: float) -> list[float]:
    """Return every round-grid level in the closed range [low, high]."""
    if grid <= 0:
        raise ValueError("grid must be > 0")
    # Small epsilon guards against float noise on boundaries (e.g. bar low sitting
    # exactly on a level would otherwise sometimes be missed).
    eps = grid * 1e-9
    first = math.ceil((low - eps) / grid) * grid
    last = math.floor((high + eps) / grid) * grid
    if first > last:
        return []
    # Count of steps + 1 for inclusive endpoints. Rounding both ends keeps the
    # returned levels clean (e.g. 1.10, 1.11 instead of 1.1000000001).
    n = int(round((last - first) / grid)) + 1
    # Determine decimals from grid so 1.05 doesn't come back as 1.0500000000001.
    decimals = max(0, -int(math.floor(math.log10(grid))) + 4)
    return [round(first + i * grid, decimals) for i in range(n)]


def find_first_touches(
    bars: Sequence[Candle],
    *,
    grid: float = 0.01,
    cooldown_bars: int = 480,
) -> Iterator[Touch]:
    """Yield the first touch of each round-grid level after ``cooldown_bars``.

    A bar can produce multiple touches if its range straddles more than one
    level. Direction is inferred from the *previous* bar's close.
    """
    last_touch_idx: dict[float, int] = {}

    for i, bar in enumerate(bars):
        levels = _round_levels_in(bar.low, bar.high, grid)
        if not levels:
            # Still record no touches — the last_touch_idx map only grows.
            continue

        for level in levels:
            prev_i = last_touch_idx.get(level)
            is_first_in_a_while = prev_i is None or (i - prev_i) >= cooldown_bars

            if is_first_in_a_while and i > 0:
                # Approach direction: was the previous close on the "low side"
                # (price rising toward the level) or "high side" (falling)?
                prev_close = bars[i - 1].close
                direction: Direction = "up" if prev_close < level else "down"
                yield Touch(
                    idx=i,
                    time=bar.time,
                    level=level,
                    direction=direction,
                    cooldown_bars=(i - prev_i) if prev_i is not None else None,
                )

            # Always update — even a re-touch inside the cooldown resets the
            # "last time we saw this level" clock. That prevents multiple bars
            # near the same level from each firing on the next cooldown.
            last_touch_idx[level] = i


def characterize_touch(
    bars: Sequence[Candle],
    touch: Touch,
    *,
    forward_bars: int = 96,
    bounce_pips: float = 30.0,
    break_pips: float = 30.0,
    pip: float = 0.0001,
) -> Outcome_:
    """Look forward from a touch and tag the outcome.

    Favorable / adverse are measured from the level itself, not from the
    touching bar's close — the level is the anchor of the whole exercise.
    """
    start = touch.idx + 1
    end = min(start + forward_bars, len(bars))
    window = bars[start:end]

    if not window:
        # Touch is right at the tail of data; no forward info available.
        return Outcome_(
            favorable=0.0, adverse=0.0,
            close_after=bars[touch.idx].close, close_dist=bars[touch.idx].close - touch.level,
            window_bars=0, tag="chop",
        )

    max_high = max(b.high for b in window)
    min_low = min(b.low for b in window)
    level = touch.level

    if touch.direction == "up":
        # Approached from below → favorable move is DOWN (pullback),
        # adverse move is UP (break through).
        favorable = max(0.0, level - min_low)
        adverse = max(0.0, max_high - level)
    else:  # "down"
        # Approached from above → favorable move is UP, adverse is DOWN.
        favorable = max(0.0, max_high - level)
        adverse = max(0.0, level - min_low)

    close_after = window[-1].close
    close_dist = close_after - level

    bounce_thresh = bounce_pips * pip
    break_thresh = break_pips * pip

    fav_hit = favorable >= bounce_thresh
    adv_hit = adverse >= break_thresh
    if fav_hit and adv_hit:
        tag: Outcome = "both"
    elif fav_hit:
        tag = "bounce"
    elif adv_hit:
        tag = "break"
    else:
        tag = "chop"

    return Outcome_(
        favorable=favorable,
        adverse=adverse,
        close_after=close_after,
        close_dist=close_dist,
        window_bars=len(window),
        tag=tag,
    )


def analyze(
    bars: Sequence[Candle],
    *,
    grid: float = 0.01,
    cooldown_bars: int = 480,
    forward_bars: int = 96,
    bounce_pips: float = 30.0,
    break_pips: float = 30.0,
    pip: float = 0.0001,
) -> Iterator[tuple[Touch, Outcome_]]:
    """Convenience: pair each touch with its outcome in one iterator."""
    for touch in find_first_touches(bars, grid=grid, cooldown_bars=cooldown_bars):
        outcome = characterize_touch(
            bars, touch,
            forward_bars=forward_bars,
            bounce_pips=bounce_pips,
            break_pips=break_pips,
            pip=pip,
        )
        yield touch, outcome


# --- Tier helper for the CLI --------------------------------------------------

TIERS: dict[str, float] = {
    "handle": 0.10,   # 1.00, 1.10, 1.20 — the biggest round numbers
    "half":   0.05,   # 1.00, 1.05, 1.10, 1.15, 1.20
    "figure": 0.01,   # 1.00, 1.01, 1.02, ...
}


def grid_for(tier_or_number: str | float) -> float:
    """Resolve a --tier name or a numeric --grid value into a grid float."""
    if isinstance(tier_or_number, str) and tier_or_number in TIERS:
        return TIERS[tier_or_number]
    return float(tier_or_number)


def summarize_outcomes(
    events: Iterable[tuple[Touch, Outcome_]],
) -> dict[str, int | float]:
    """Roll up an iterable of (touch, outcome) into counts + averages."""
    n = 0
    tags = {"bounce": 0, "break": 0, "both": 0, "chop": 0}
    fav_sum = 0.0
    adv_sum = 0.0
    for _t, o in events:
        n += 1
        tags[o.tag] += 1
        fav_sum += o.favorable
        adv_sum += o.adverse
    return {
        "n": n,
        **tags,
        "favorable_avg": (fav_sum / n) if n else 0.0,
        "adverse_avg": (adv_sum / n) if n else 0.0,
    }
