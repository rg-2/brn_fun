"""Named-strategy registry.

A ``StrategyConfig`` captures every knob needed to reproduce a specific
trading strategy: instrument, granularity, signal detection parameters,
entry rules, order type (market vs limit), exit rules, and costs. Adding
a new strategy means adding a new entry to :data:`STRATEGIES` — no code
outside this module needs to change.

The CLI command ``brn strategy NAME`` runs one end-to-end and prints
the summary; ``brn strategy list`` shows all available strategies.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Complete specification of a trading strategy.

    Every parameter that ``brn backtest`` accepts has a home here, plus a
    ``description`` and ``reference_pnl_10y`` for human documentation.
    Adding stop-management or filter knobs later is just a matter of
    adding fields with a safe default.
    """

    name: str                      # short slug used to invoke, e.g. "audusd"
    instrument: str                # Oanda instrument (e.g. "AUD_USD")
    granularity: str = "M1"        # candles to analyze at

    # Signal detection ------------------------------------------------------
    grid: float = 0.01             # round-number grid (0.01 = every 100 pips)
    pip: float = 0.0001            # pip size (0.0001 majors, 0.01 JPY)
    cooldown_bars: int = 7200      # level "cool" period before another touch
    forward_bars: int = 1440       # analyze() outcome-window (only used for tagging)

    # Entry -----------------------------------------------------------------
    filter_name: str = "all"       # backtest.FILTERS key
    entry: str = "confirm"         # "touch" or "confirm"
    entry_offset: int = 0          # extra bars to wait past base entry bar

    # Order type -----------------------------------------------------------
    limit_offset_pips: float = 2.0        # 0 = market at close
    limit_fill_window: int = 60           # bars to wait for a limit to fill

    # Exit ------------------------------------------------------------------
    target_pips: float = 60.0
    stop_pips: float = 30.0
    target_atr: float | None = None       # if set, overrides target_pips
    stop_atr: float | None = None         # if set, overrides stop_pips
    max_bars: int = 1440                  # hard timeout

    # Costs ----------------------------------------------------------------
    spread_pips: float = 1.0              # round-trip

    # Optional stop management ---------------------------------------------
    breakeven_trigger_pips: float = 0.0
    trail_trigger_pips: float = 0.0
    trail_distance_pips: float = 0.0

    # Optional event filters -----------------------------------------------
    max_sma_slope_pips: float | None = None
    reject_hours_utc: tuple[int, ...] = field(default_factory=tuple)

    # Backtest infra --------------------------------------------------------
    path_ambiguity: str = "worst"

    # Documentation --------------------------------------------------------
    description: str = ""
    reference_pnl_10y: str = ""


# ----------------------------------------------------------------------
# Registered strategies
# ----------------------------------------------------------------------


STRATEGIES: dict[str, StrategyConfig] = {
    "audusd": StrategyConfig(
        name="audusd",
        instrument="AUD_USD",
        granularity="M1",

        grid=0.01,
        pip=0.0001,
        cooldown_bars=7200,     # 5 trading days at M1
        forward_bars=1440,      # 24h — only used for outcome tagging

        filter_name="all",
        entry="confirm",
        entry_offset=14,        # 1 base + 14 = 15-min confirmation wait

        limit_offset_pips=2.0,  # buy 2p below signal / sell 2p above
        limit_fill_window=60,   # 1 hour to fill

        target_pips=60.0,
        stop_pips=30.0,
        max_bars=1440,          # 24h max hold

        spread_pips=1.0,        # round-trip cost

        description=(
            "AUD_USD round-number bounce strategy.\n"
            "  Signal: touches of the 0.01 (100-pip) grid — first touch\n"
            "          of each round level after 5 trading days untouched.\n"
            "  Entry:  2-pip favorable LIMIT (buy dips / sell rallies), placed\n"
            "          15 minutes after the touch bar closes; canceled if it\n"
            "          hasn't filled within 60 minutes.\n"
            "  Exit:   +60 pips target, -30 pips stop, hard timeout at 24h.\n"
            "  Cost:   1 pip round-trip spread deducted from every trade.\n"
        ),
        reference_pnl_10y=(
            "+2,203 pips over 10 years (2016-2026). "
            "~30 trades/year, 48% win rate, +6.72 pips per trade, "
            "max drawdown 225 pips."
        ),
    ),
}


def get_strategy(name: str) -> StrategyConfig:
    """Look up a strategy by its slug, raising a clear error if missing."""
    if name not in STRATEGIES:
        known = ", ".join(sorted(STRATEGIES))
        raise KeyError(f"Unknown strategy {name!r}. Known: {known}")
    return STRATEGIES[name]
