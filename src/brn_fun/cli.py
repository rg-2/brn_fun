"""Command-line entry point.

Exposed as ``brn`` via ``[project.scripts]``. Everything is a thin shell around
functions in the other modules — keep argument parsing here, keep logic there.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import click

from .analyze import TIERS, analyze, grid_for, summarize_outcomes
from .backtest import FILTERS, backtest_touches, summarize_trades, uses_confirmation
from .config import Granularity, load_config, load_secrets
from .db import connect, count_candles, fetch_candles, latest_time, upsert_candles
from .oanda import download_range, download_recent
from .plot import plot_trades_pdf, sample_by_half
from .reaction import (
    adverse_percentiles,
    compute_reactions,
    favorable_percentiles,
    target_stats,
)
from .live.paper import PaperTrader
from .strategy import STRATEGIES, get_strategy

# Rough time-per-bar for granularity codes we might see. Used only for
# human-readable hour labels in the reaction table.
_MINUTES_PER_BAR = {
    "S5": 5 / 60, "S10": 10 / 60, "S15": 15 / 60, "S30": 30 / 60,
    "M1": 1, "M2": 2, "M4": 4, "M5": 5, "M10": 10, "M15": 15, "M30": 30,
    "H1": 60, "H2": 120, "H3": 180, "H4": 240,
}


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default="config.yaml",
    show_default=True,
    help="Path to config.yaml.",
)
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, config_path: Path, verbose: bool) -> None:
    """brn_fun — trade around big round numbers."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)


@cli.command()
@click.argument("instrument")
@click.option(
    "--granularity",
    "-g",
    type=str,
    default=None,
    help="Candle granularity (default: from config, usually M15).",
)
@click.option(
    "--from",
    "start",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]),
    default=None,
    help="Start UTC time. Omit to resume from the last stored bar (or last 500).",
)
@click.option(
    "--to",
    "end",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]),
    default=None,
    help="End UTC time (default: now).",
)
@click.option(
    "--count",
    type=int,
    default=None,
    help="If set (and --from is not), pull this many recent bars instead.",
)
@click.pass_context
def download(
    ctx: click.Context,
    instrument: str,
    granularity: str | None,
    start: datetime | None,
    end: datetime | None,
    count: int | None,
) -> None:
    """Download candles for INSTRUMENT (e.g. EUR_USD) into the local SQLite DB."""
    cfg = ctx.obj["config"]
    secrets = load_secrets()
    gran: Granularity = granularity or cfg.default_granularity  # type: ignore[assignment]

    with connect(cfg.db_path) as conn:
        # Decide the fetch mode:
        #   --count wins if given.
        #   Otherwise pick a start: --from, or resume-from-latest, or last 500.
        if count is not None:
            click.echo(f"Fetching last {count} {gran} bars for {instrument}…")
            candles = list(
                download_recent(secrets, instrument, gran, count=count, price=cfg.price)
            )
        else:
            if start is None:
                latest = latest_time(conn, instrument, gran)
                if latest is None:
                    click.echo(
                        f"No stored bars for {instrument} {gran}; "
                        "fetching last 500 as a seed."
                    )
                    candles = list(
                        download_recent(
                            secrets, instrument, gran, count=500, price=cfg.price
                        )
                    )
                else:
                    click.echo(f"Resuming {instrument} {gran} from {latest}…")
                    start_dt = _iso_to_dt(latest)
                    candles = list(
                        download_range(
                            secrets, instrument, gran, start_dt, end, price=cfg.price
                        )
                    )
            else:
                start_utc = start.replace(tzinfo=timezone.utc)
                end_utc = end.replace(tzinfo=timezone.utc) if end else None
                click.echo(
                    f"Fetching {instrument} {gran} from {start_utc.isoformat()}…"
                )
                candles = list(
                    download_range(
                        secrets, instrument, gran, start_utc, end_utc, price=cfg.price
                    )
                )

        written = upsert_candles(conn, candles)
        total = count_candles(conn, instrument, gran)
        click.echo(
            f"Wrote {written} bars (total for {instrument} {gran}: {total})."
        )


@cli.command("download-all")
@click.option("--granularity", "-g", type=str, default=None)
@click.pass_context
def download_all(ctx: click.Context, granularity: str | None) -> None:
    """Run `download` for every instrument in config.yaml."""
    cfg = ctx.obj["config"]
    for inst in cfg.instruments:
        ctx.invoke(
            download,
            instrument=inst,
            granularity=granularity,
            start=None,
            end=None,
            count=None,
        )


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show what's stored locally for each configured instrument."""
    cfg = ctx.obj["config"]
    with connect(cfg.db_path) as conn:
        gran = cfg.default_granularity
        click.echo(f"DB: {cfg.db_path}   default granularity: {gran}")
        click.echo(f"{'instrument':<10}  {'bars':>8}  latest")
        for inst in cfg.instruments:
            n = count_candles(conn, inst, gran)
            last = latest_time(conn, inst, gran) or "-"
            click.echo(f"{inst:<10}  {n:>8}  {last}")


@cli.command()
@click.argument("instrument")
@click.option(
    "--granularity", "-g", type=str, default=None,
    help="Candle granularity (default: from config).",
)
@click.option(
    "--tail", "tail", type=int, default=20, show_default=True,
    help="Show the last N bars (chronological, newest at bottom).",
)
@click.option(
    "--head", "head", type=int, default=None,
    help="Show the first N stored bars instead (overrides --tail).",
)
@click.option(
    "--complete-only/--all", default=False,
    help="Drop the currently-forming bar (complete=0).",
)
@click.pass_context
def show(
    ctx: click.Context,
    instrument: str,
    granularity: str | None,
    tail: int,
    head: int | None,
    complete_only: bool,
) -> None:
    """Print stored bars for INSTRUMENT as a table."""
    cfg = ctx.obj["config"]
    gran = granularity or cfg.default_granularity

    with connect(cfg.db_path) as conn:
        if head is not None:
            bars = fetch_candles(
                conn, instrument, gran,
                limit=head, order="asc", complete_only=complete_only,
            )
        else:
            # Grab last N in desc order, then flip so output reads oldest→newest.
            bars = fetch_candles(
                conn, instrument, gran,
                limit=tail, order="desc", complete_only=complete_only,
            )
            bars.reverse()

    if not bars:
        click.echo(f"No {gran} bars stored for {instrument}.")
        return

    # Header
    click.echo(
        f"{'time':<30}  {'open':>9}  {'high':>9}  {'low':>9}  "
        f"{'close':>9}  {'volume':>7}  c"
    )
    for b in bars:
        flag = "1" if b.complete else "·"  # · = forming bar, easy to spot
        click.echo(
            f"{b.time:<30}  {b.open:>9.5f}  {b.high:>9.5f}  {b.low:>9.5f}  "
            f"{b.close:>9.5f}  {b.volume:>7d}  {flag}"
        )
    click.echo(f"({len(bars)} bars)")


@cli.command()
@click.argument("instrument")
@click.option("--granularity", "-g", type=str, default=None,
              help="Candle granularity (default: from config).")
@click.option("--tier", type=click.Choice(list(TIERS.keys())), default="figure",
              show_default=True,
              help="Round-level grid: handle=0.10, half=0.05, figure=0.01.")
@click.option("--grid", type=float, default=None,
              help="Numeric grid override (e.g. 0.005). Wins over --tier.")
@click.option("--cooldown-bars", type=int, default=7200, show_default=True,
              help="Bars a level must be untouched before it counts as fresh "
                   "(7200 M1 = 5 trading days).")
@click.option("--forward-bars", type=int, default=1440, show_default=True,
              help="Bars to look forward when tagging outcome (1440 M1 = 24h).")
@click.option("--bounce-pips", type=float, default=30.0, show_default=True,
              help="Favorable move (pips) required to tag 'bounce'.")
@click.option("--break-pips", type=float, default=30.0, show_default=True,
              help="Adverse move (pips) required to tag 'break'.")
@click.option("--bounce-atr", type=float, default=None,
              help="Use ATR × this multiplier as the bounce threshold instead "
                   "of --bounce-pips. Normalizes 'meaningful move' across pairs.")
@click.option("--break-atr", type=float, default=None,
              help="Use ATR × this multiplier as the break threshold instead "
                   "of --break-pips.")
@click.option("--pip", type=float, default=0.0001, show_default=True,
              help="Pip size (0.0001 for majors, 0.01 for JPY pairs).")
@click.option("--complete-only/--all", default=True, show_default=True,
              help="Skip the currently-forming bar when reading history.")
@click.option("--export", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write per-touch rows to CSV.")
@click.option("--head", type=int, default=15, show_default=True,
              help="How many recent touches to print in the table.")
@click.option("--atr-period", type=int, default=210, show_default=True,
              help="Bars used for ATR (210 M1 = 3.5h, matches classic 14-M15).")
@click.option("--approach-bars", type=int, default=300, show_default=True,
              help="Bars for approach-change/approach-range (300 M1 = 5h).")
@click.option("--sma-period", type=int, default=28800, show_default=True,
              help="Bars for the trend SMA (28800 M1 = 20 trading days).")
@click.option("--slope-lookback", type=int, default=7200, show_default=True,
              help="Bars back for SMA slope (7200 M1 = 5 trading days).")
@click.option("--trend-flat-pips", type=float, default=50.0, show_default=True,
              help="|SMA slope| below this many pips is 'flat', else up/down.")
@click.pass_context
def touches(
    ctx: click.Context,
    instrument: str,
    granularity: str | None,
    tier: str,
    grid: float | None,
    cooldown_bars: int,
    forward_bars: int,
    bounce_pips: float,
    break_pips: float,
    bounce_atr: float | None,
    break_atr: float | None,
    pip: float,
    complete_only: bool,
    export: Path | None,
    head: int,
    atr_period: int,
    approach_bars: int,
    sma_period: int,
    slope_lookback: int,
    trend_flat_pips: float,
) -> None:
    """Find round-number 'first-touch-in-a-while' events and tag outcomes."""
    cfg = ctx.obj["config"]
    gran = granularity or cfg.default_granularity
    grid_val = float(grid) if grid is not None else grid_for(tier)

    with connect(cfg.db_path) as conn:
        bars = fetch_candles(
            conn, instrument, gran, limit=None, order="asc",
            complete_only=complete_only,
        )

    if not bars:
        click.echo(f"No {gran} bars stored for {instrument} — run `brn download` first.")
        return

    events = list(analyze(
        bars,
        grid=grid_val, cooldown_bars=cooldown_bars, forward_bars=forward_bars,
        bounce_pips=bounce_pips, break_pips=break_pips,
        bounce_atr=bounce_atr, break_atr=break_atr, pip=pip,
        atr_period=atr_period, approach_bars=approach_bars,
        sma_period=sma_period, slope_lookback=slope_lookback,
        trend_flat_pips=trend_flat_pips,
    ))

    summary = summarize_outcomes(iter(events))
    # `summarize_outcomes` consumed a fresh iterator; events list is intact.

    span_from = bars[0].time
    span_to = bars[-1].time
    # Report the threshold mode so the reader knows which definition of
    # bounce/break is being applied.
    bnc_desc = f"{bounce_atr:g}×ATR" if bounce_atr is not None else f"{bounce_pips:g}p"
    brk_desc = f"{break_atr:g}×ATR" if break_atr is not None else f"{break_pips:g}p"
    click.echo(
        f"{instrument} {gran}   grid={grid_val:g}   cooldown={cooldown_bars} bars   "
        f"forward={forward_bars} bars   thresh: bnc={bnc_desc} brk={brk_desc}"
    )
    click.echo(f"span: {span_from} → {span_to}   ({len(bars):,} bars)")
    click.echo(
        f"touches: {summary['n']:>4}   "
        f"bounce={summary['bounce']}   break={summary['break']}   "
        f"both={summary['both']}   chop={summary['chop']}"
    )
    if summary["n"]:
        click.echo(
            f"avg favorable: {summary['favorable_avg'] / pip:5.1f} pips   "
            f"avg adverse:   {summary['adverse_avg'] / pip:5.1f} pips"
        )

    if events:
        click.echo("")
        # Compact shape codes so the table stays readable:
        #   D doji, H hammer, S shooting_star, + bullish, - bearish, . neutral
        # Trend/alignment codes:
        #   trend: U up, D down, ~ flat
        #   align: W with, A against, · flat
        click.echo(
            f"{'time':<30}  {'level':>7}  {'dir':<4}  "
            f"{'atr':>5}  {'appr':>6}  wb  ts  cs  cf  tr  al  "
            f"{'fav':>6}  {'adv':>6}  {'outcome':<7}"
        )
        for t, c, cf, o in events[-head:]:
            wb = "W" if c.wick_only else "b"
            ts = _shape_code(c.touch_shape)
            cs = _shape_code(cf.shape) if cf.present else " "
            # Confirmation flag: E engulfing + close-away, e engulfing only,
            # a close-away only, · nothing. If confirm bar isn't present, blank.
            if not cf.present:
                cflag = " "
            elif cf.engulfing and cf.close_away:
                cflag = "E"
            elif cf.engulfing:
                cflag = "e"
            elif cf.close_away:
                cflag = "a"
            else:
                cflag = "·"
            trend_code = {"up": "U", "down": "D", "flat": "~"}[c.trend]
            align_code = {"with": "W", "against": "A", "flat": "·"}[c.trend_alignment]
            click.echo(
                f"{t.time:<30}  {t.level:>7.4f}  {t.direction:<4}  "
                f"{c.atr / pip:>5.1f}  {c.approach_change / pip:>+6.1f}  {wb}   "
                f"{ts}   {cs}   {cflag}   {trend_code}   {align_code}   "
                f"{o.favorable / pip:>6.1f}  {o.adverse / pip:>6.1f}  {o.tag:<7}"
            )
        click.echo(f"(showing last {min(head, len(events))} of {len(events)})")
        click.echo(
            "legend: wb=Wick/body   ts=touch shape   cs=confirm shape   "
            "cf=E(ngulfing+away)/e(ngulf)/a(way)/·(none)   "
            "tr=U up / D down / ~ flat   al=W with / A against / · flat   "
            "D doji H hammer S shooting_star + bull - bear . neutral"
        )

    if export is not None:
        _export_touches(export, events, pip=pip)
        click.echo(f"\nwrote {len(events)} rows to {export}")


_SHAPE_CODES = {
    "doji": "D", "hammer": "H", "shooting_star": "S",
    "bullish": "+", "bearish": "-", "neutral": ".",
}


def _shape_code(shape: str) -> str:
    return _SHAPE_CODES.get(shape, "?")


def _export_touches(
    path: Path,
    events: list,  # list[tuple[Touch, Context, Confirmation, Outcome_]]
    *,
    pip: float,
) -> None:
    """Write per-touch results as CSV. Pip-denominated columns for readability."""
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "time", "bar_idx", "level", "direction", "cooldown_bars",
            "atr_pips", "hour_utc", "dow",
            "approach_change_pips", "approach_range_pips", "wick_only",
            "touch_shape", "touch_rejection",
            "confirm_present", "confirm_shape", "confirm_engulfing", "confirm_close_away",
            "sma_20d", "sma_slope_pips", "trend", "trend_alignment",
            "favorable_pips", "adverse_pips", "close_after", "close_dist_pips",
            "window_bars", "outcome",
        ])
        for t, c, cf, o in events:
            w.writerow([
                t.time, t.idx, f"{t.level:.5f}", t.direction, t.cooldown_bars,
                f"{c.atr / pip:.1f}", c.hour_utc, c.dow,
                f"{c.approach_change / pip:+.1f}", f"{c.approach_range / pip:.1f}",
                int(c.wick_only),
                c.touch_shape, int(c.touch_rejection),
                int(cf.present), cf.shape, int(cf.engulfing), int(cf.close_away),
                f"{c.sma_20d:.5f}", f"{c.sma_slope / pip:+.1f}",
                c.trend, c.trend_alignment,
                f"{o.favorable / pip:.1f}", f"{o.adverse / pip:.1f}",
                f"{o.close_after:.5f}", f"{o.close_dist / pip:.1f}",
                o.window_bars, o.tag,
            ])


@cli.command()
@click.argument("instrument")
@click.option("--granularity", "-g", type=str, default=None)
@click.option("--tier", type=click.Choice(list(TIERS.keys())), default="figure",
              show_default=True)
@click.option("--grid", type=float, default=None,
              help="Numeric grid override; wins over --tier.")
@click.option("--pip", type=float, default=0.0001, show_default=True,
              help="Pip size (0.0001 majors, 0.01 JPY pairs).")
@click.option("--cooldown-bars", type=int, default=7200, show_default=True,
              help="Bars a level must be untouched (7200 M1 = 5 trading days).")
@click.option("--forward-bars", type=int, default=1440, show_default=True,
              help="Analyze-window bars (1440 M1 = 24h).")
@click.option("--filter", "filter_name",
              type=click.Choice(list(FILTERS.keys())),
              default="wick+drift+away", show_default=True,
              help="Named entry filter — see backtest.FILTERS.")
@click.option("--entry", type=click.Choice(["touch", "confirm"]), default=None,
              help="Bar to enter at. Defaults to 'confirm' when the filter "
                   "uses confirmation features (avoids peeking).")
@click.option("--entry-offset", type=int, default=0, show_default=True,
              help="Extra bars to wait after the base entry bar. Useful on M1 "
                   "where 1 bar = 1 min; --entry-offset 14 mimics M15's "
                   "15-min confirmation wait.")
@click.option("--target-pips", type=float, default=60.0, show_default=True)
@click.option("--stop-pips",   type=float, default=30.0, show_default=True)
@click.option("--target-atr", type=float, default=None,
              help="If set, target = this multiplier × per-touch ATR "
                   "(overrides --target-pips).")
@click.option("--stop-atr", type=float, default=None,
              help="If set, stop = this multiplier × per-touch ATR "
                   "(overrides --stop-pips).")
@click.option("--max-bars", type=int, default=1440, show_default=True,
              help="Timeout: bars to hold before closing at market "
                   "(1440 M1 = 24h).")
@click.option("--path-ambiguity", type=click.Choice(["worst", "best"]),
              default="worst", show_default=True,
              help="If a bar contains both target and stop, which fires first.")
@click.option("--spread-pips", type=float, default=1.0, show_default=True,
              help="Round-trip spread cost in pips, deducted from every trade. "
                   "Rough per-pair estimates: 1.0 majors, 1.2 GBP crosses, "
                   "1.5 JPY crosses. NEVER report P&L without a spread cost.")
@click.option("--limit-offset-pips", type=float, default=2.0, show_default=True,
              help="Limit order at this many pips FAVORABLE to signal "
                   "(below for longs, above for shorts). Default 2p should "
                   "at minimum cover the spread. Pass 0 only for research "
                   "comparison to market entry.")
@click.option("--limit-fill-window", type=int, default=60, show_default=True,
              help="Bars past signal to wait for the limit to fill "
                   "(default 60 M1 = 1 hour).")
@click.option("--complete-only/--all", default=True, show_default=True,
              help="Skip the currently-forming bar when reading history.")
@click.option("--head", type=int, default=10, show_default=True,
              help="Trades to print in the tail table.")
@click.option("--export", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write per-trade rows to CSV.")
@click.pass_context
def backtest(
    ctx: click.Context,
    instrument: str,
    granularity: str | None,
    tier: str,
    grid: float | None,
    pip: float,
    cooldown_bars: int,
    forward_bars: int,
    filter_name: str,
    entry: str | None,
    entry_offset: int,
    target_pips: float,
    stop_pips: float,
    target_atr: float | None,
    stop_atr: float | None,
    max_bars: int,
    path_ambiguity: str,
    spread_pips: float,
    limit_offset_pips: float,
    limit_fill_window: int,
    complete_only: bool,
    head: int,
    export: Path | None,
) -> None:
    """Backtest a target/stop strategy driven by round-number touch events."""
    cfg = ctx.obj["config"]
    gran = granularity or cfg.default_granularity
    grid_val = float(grid) if grid is not None else grid_for(tier)

    # Pick a safe default entry mode based on whether the filter peeks.
    if entry is None:
        entry = "confirm" if uses_confirmation(filter_name) else "touch"

    with connect(cfg.db_path) as conn:
        bars = fetch_candles(
            conn, instrument, gran, limit=None, order="asc",
            complete_only=complete_only,
        )
    if not bars:
        click.echo(f"No {gran} bars stored for {instrument} — run `brn download` first.")
        return

    events = list(analyze(
        bars,
        grid=grid_val, cooldown_bars=cooldown_bars, forward_bars=forward_bars,
        pip=pip,
    ))
    trades = backtest_touches(
        bars, events,
        pip=pip,
        filter_name=filter_name,
        entry=entry,  # type: ignore[arg-type]
        entry_offset=entry_offset,
        target_pips=target_pips, stop_pips=stop_pips,
        target_atr=target_atr, stop_atr=stop_atr,
        max_bars=max_bars,
        path_ambiguity=path_ambiguity,  # type: ignore[arg-type]
        spread_pips=spread_pips,
        limit_offset_pips=limit_offset_pips,
        limit_fill_window=limit_fill_window,
    )
    stats = summarize_trades(trades, pip=pip)

    tgt_desc = f"{target_atr:g}×ATR" if target_atr is not None else f"{target_pips:g}p"
    stp_desc = f"{stop_atr:g}×ATR"   if stop_atr   is not None else f"{stop_pips:g}p"

    click.echo(
        f"{instrument} {gran}   filter={filter_name}   entry={entry}+{entry_offset}   "
        f"target={tgt_desc}   stop={stp_desc}   max_bars={max_bars}"
    )
    if bars:
        click.echo(f"span: {bars[0].time} → {bars[-1].time}   ({len(bars):,} bars)")

    if stats["n"] == 0:
        click.echo("No trades — the filter matched nothing.")
        return

    click.echo(
        f"trades: {stats['n']}   win rate: {stats['win_rate']:.1f}%   "
        f"expectancy: {stats['expectancy_pips']:+.1f} pips/trade   "
        f"total: {stats['total_pips']:+.1f} pips"
    )
    click.echo(
        f"avg win: {stats['avg_win_pips']:+.1f} pips   "
        f"avg loss: {stats['avg_loss_pips']:+.1f} pips   "
        f"max drawdown: {stats['max_drawdown_pips']:.1f} pips"
    )
    click.echo(
        f"exits: target={stats['target']}  stop={stats['stop']}  "
        f"timeout={stats['timeout']}   avg hold: {stats['avg_hold_bars']:.1f} bars"
    )

    if head > 0:
        click.echo("")
        click.echo(
            f"{'entry_time':<30}  dir    lvl     entry     exit   "
            f"{'pnl':>6}  reason   hold"
        )
        for t in trades[-head:]:
            click.echo(
                f"{t.entry_time:<30}  {t.direction:<5}  {t.level:>6.4f}  "
                f"{t.entry_price:>7.5f}  {t.exit_price:>7.5f}  "
                f"{t.pnl_price / pip:>+6.1f}  {t.exit_reason:<7}  {t.hold_bars:>4d}"
            )
        click.echo(f"(showing last {min(head, len(trades))} of {len(trades)})")

    if export is not None:
        _export_trades(export, trades, pip=pip)
        click.echo(f"\nwrote {len(trades)} trades to {export}")


def _export_trades(path: Path, trades: list, *, pip: float) -> None:
    """CSV export of trades. Prices as-is, PnL in pips."""
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "entry_time", "entry_idx", "direction", "level",
            "entry_price", "target_price", "stop_price",
            "exit_time", "exit_idx", "exit_price", "exit_reason",
            "pnl_pips", "hold_bars",
        ])
        for t in trades:
            w.writerow([
                t.entry_time, t.entry_idx, t.direction, f"{t.level:.5f}",
                f"{t.entry_price:.5f}", f"{t.target_price:.5f}", f"{t.stop_price:.5f}",
                t.exit_time, t.exit_idx, f"{t.exit_price:.5f}", t.exit_reason,
                f"{t.pnl_price / pip:+.1f}", t.hold_bars,
            ])


@cli.command()
@click.argument("instrument")
@click.option("--granularity", "-g", type=str, default=None)
@click.option("--tier", type=click.Choice(list(TIERS.keys())), default="figure",
              show_default=True)
@click.option("--grid", type=float, default=None)
@click.option("--pip", type=float, default=0.0001, show_default=True)
@click.option("--cooldown-bars", type=int, default=7200, show_default=True)
@click.option("--forward-bars", type=int, default=1440, show_default=True)
@click.option("--filter", "filter_name",
              type=click.Choice(list(FILTERS.keys())),
              default="wick+drift+away", show_default=True)
@click.option("--entry", type=click.Choice(["touch", "confirm"]), default=None)
@click.option("--target-pips", type=float, default=60.0, show_default=True)
@click.option("--stop-pips",   type=float, default=30.0, show_default=True)
@click.option("--target-atr", type=float, default=None)
@click.option("--stop-atr", type=float, default=None)
@click.option("--max-bars", type=int, default=1440, show_default=True)
@click.option("--path-ambiguity", type=click.Choice(["worst", "best"]),
              default="worst", show_default=True)
@click.option("--complete-only/--all", default=True, show_default=True)
@click.option("--split", type=str, default="2021-01-01", show_default=True,
              help="RFC-3339 date splitting trades into H1 / H2 sections.")
@click.option("--n-h1", type=int, default=50, show_default=True,
              help="Sample size from the pre-split half (evenly spaced).")
@click.option("--n-h2", type=int, default=50, show_default=True,
              help="Sample size from the post-split half (evenly spaced).")
@click.option("--cols", type=int, default=4, show_default=True,
              help="Grid columns per PDF page.")
@click.option("--rows", type=int, default=3, show_default=True,
              help="Grid rows per PDF page.")
@click.option("--context-before", type=int, default=600, show_default=True,
              help="Bars of pre-entry context shown per panel (600 M1 = 10h).")
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Output PDF path. Default: data/plots/<INSTRUMENT>.pdf")
@click.pass_context
def plot(
    ctx: click.Context,
    instrument: str,
    granularity: str | None,
    tier: str,
    grid: float | None,
    pip: float,
    cooldown_bars: int,
    forward_bars: int,
    filter_name: str,
    entry: str | None,
    target_pips: float,
    stop_pips: float,
    target_atr: float | None,
    stop_atr: float | None,
    max_bars: int,
    path_ambiguity: str,
    complete_only: bool,
    split: str,
    n_h1: int,
    n_h2: int,
    cols: int,
    rows: int,
    context_before: int,
    out: Path | None,
) -> None:
    """Render a multi-panel PDF of backtest trades, split H1 vs H2."""
    cfg = ctx.obj["config"]
    gran = granularity or cfg.default_granularity
    grid_val = float(grid) if grid is not None else grid_for(tier)
    if entry is None:
        entry = "confirm" if uses_confirmation(filter_name) else "touch"
    if out is None:
        out = Path("data/plots") / f"{instrument}.pdf"

    with connect(cfg.db_path) as conn:
        bars = fetch_candles(
            conn, instrument, gran, limit=None, order="asc",
            complete_only=complete_only,
        )
    if not bars:
        click.echo(f"No {gran} bars stored for {instrument}.")
        return

    events = list(analyze(
        bars, grid=grid_val, cooldown_bars=cooldown_bars,
        forward_bars=forward_bars, pip=pip,
    ))
    trades = backtest_touches(
        bars, events,
        pip=pip, filter_name=filter_name, entry=entry,  # type: ignore[arg-type]
        target_pips=target_pips, stop_pips=stop_pips,
        target_atr=target_atr, stop_atr=stop_atr,
        max_bars=max_bars, path_ambiguity=path_ambiguity,  # type: ignore[arg-type]
    )
    if not trades:
        click.echo("No trades produced — try a looser filter.")
        return

    halves = sample_by_half(trades, split, max(n_h1, n_h2))
    # sample_by_half samples the same N from each half; enforce per-half caps.
    keys = list(halves.keys())
    halves[keys[0]] = halves[keys[0]][:n_h1]
    halves[keys[1]] = halves[keys[1]][:n_h2]

    title = (
        f"{instrument} {gran}  filter={filter_name}  entry={entry}  "
        f"target={target_pips:g}p  stop={stop_pips:g}p"
    )
    click.echo(f"{len(trades)} total trades  →  "
               f"H1 sampled {len(halves[keys[0]])}, "
               f"H2 sampled {len(halves[keys[1]])}")
    click.echo(f"writing {out}")
    plotted = plot_trades_pdf(
        bars, halves, out,
        pip=pip, cols=cols, rows=rows,
        context_before=context_before, title_prefix=title,
    )
    click.echo(f"plotted {plotted} panels")


@cli.command()
@click.argument("instrument")
@click.option("--granularity", "-g", type=str, default=None)
@click.option("--tier", type=click.Choice(list(TIERS.keys())), default="figure",
              show_default=True)
@click.option("--grid", type=float, default=None)
@click.option("--pip", type=float, default=0.0001, show_default=True)
@click.option("--cooldown-bars", type=int, default=7200, show_default=True)
@click.option("--forward-bars", type=int, default=480, show_default=True,
              help="Bars past entry to record reaction over (480 M1 = 8h).")
@click.option("--filter", "filter_name",
              type=click.Choice(list(FILTERS.keys())),
              default="all", show_default=True,
              help="Only include events passing this filter. 'all' = raw base rates.")
@click.option("--entry", type=click.Choice(["touch", "confirm"]), default=None)
@click.option("--windows", default="30,60,120,240,480", show_default=True,
              help="Comma-separated bar counts for time-bucket stats "
                   "(default: 30min,1h,2h,4h,8h at M1).")
@click.option("--targets", default="10,15,20,25,30", show_default=True,
              help="Comma-separated target sizes (pips) for hit-rate table.")
@click.option("--split", type=str, default=None,
              help="If set, run separately on events before/after this date.")
@click.option("--complete-only/--all", "complete_only",
              default=True, show_default=True)
@click.pass_context
def reaction(
    ctx: click.Context,
    instrument: str,
    granularity: str | None,
    tier: str,
    grid: float | None,
    pip: float,
    cooldown_bars: int,
    forward_bars: int,
    filter_name: str,
    entry: str | None,
    windows: str,
    targets: str,
    split: str | None,
    complete_only: bool,
) -> None:
    """Per-pair reaction study: how fast/deep the level bounce develops."""
    cfg = ctx.obj["config"]
    gran = granularity or cfg.default_granularity
    grid_val = float(grid) if grid is not None else grid_for(tier)
    if entry is None:
        entry = "confirm" if uses_confirmation(filter_name) else "touch"

    window_bars = [int(w) for w in windows.split(",") if w]
    target_pips = [float(t) for t in targets.split(",") if t]

    with connect(cfg.db_path) as conn:
        bars = fetch_candles(
            conn, instrument, gran, limit=None, order="asc",
            complete_only=complete_only,
        )
    if not bars:
        click.echo(f"No {gran} bars stored for {instrument}.")
        return

    events = list(analyze(
        bars, grid=grid_val, cooldown_bars=cooldown_bars,
        forward_bars=forward_bars, pip=pip,
    ))
    # Apply named filter — reuse the backtester's filter registry.
    filter_fn = FILTERS[filter_name]
    events = [e for e in events if filter_fn(e[0], e[1], e[2], pip)]

    def report(subset_events, label: str) -> None:
        reactions = compute_reactions(
            bars, subset_events, forward_bars=forward_bars, entry=entry,
        )
        if not reactions:
            click.echo(f"\n{label}: no reactions to analyze.")
            return
        click.echo(f"\n{label}   n={len(reactions)} events")
        # 1. Favorable / adverse percentiles at each time bucket.
        fav_p = favorable_percentiles(reactions, window_bars, [25, 50, 75, 90])
        adv_p = adverse_percentiles(reactions, window_bars, [25, 50, 75, 90])
        click.echo("\n  Max FAVORABLE (pips) by time window:")
        click.echo(f"  {'bars':>4} {'hours':>6}   {'P25':>6} {'P50':>6} {'P75':>6} {'P90':>6}")
        for w in window_bars:
            hrs = w * _MINUTES_PER_BAR.get(gran, 1) / 60
            vals = [v / pip for v in fav_p[w]]
            click.echo(f"  {w:>4} {hrs:>5.1f}h    {vals[0]:>6.1f} {vals[1]:>6.1f} {vals[2]:>6.1f} {vals[3]:>6.1f}")
        click.echo("\n  Max ADVERSE (pips) by time window:")
        click.echo(f"  {'bars':>4} {'hours':>6}   {'P25':>6} {'P50':>6} {'P75':>6} {'P90':>6}")
        for w in window_bars:
            hrs = w * _MINUTES_PER_BAR.get(gran, 1) / 60
            vals = [v / pip for v in adv_p[w]]
            click.echo(f"  {w:>4} {hrs:>5.1f}h    {vals[0]:>6.1f} {vals[1]:>6.1f} {vals[2]:>6.1f} {vals[3]:>6.1f}")

        # 2. Hit-rate + adverse-before-hit for each target across each window.
        click.echo("\n  Target HIT RATE within N bars   |   Adverse before hit (pips)")
        header_windows = "  ".join(f"{w:>3}b" for w in window_bars)
        click.echo(f"  {'target':>6}   {header_windows}   |   {'adv P50':>7} {'adv P75':>7} {'adv P90':>7}")
        for t in target_pips:
            hit_cells = []
            for w in window_bars:
                s = target_stats(reactions, t * pip, w)
                hit_cells.append(f"{s.hit_rate * 100:>3.0f}%")
            # Use the widest window for adverse-before-hit stats.
            widest = max(window_bars)
            s_wide = target_stats(reactions, t * pip, widest)
            click.echo(
                f"  {int(t):>4}p    "
                + "  ".join(hit_cells)
                + "   |   "
                + f"{s_wide.adv_before_hit_p50 / pip:>6.1f}p "
                + f"{s_wide.adv_before_hit_p75 / pip:>6.1f}p "
                + f"{s_wide.adv_before_hit_p90 / pip:>6.1f}p"
            )

    click.echo(
        f"{instrument} {gran}  filter={filter_name}  entry={entry}  "
        f"forward={forward_bars} bars"
    )
    if split is None:
        report(events, label="all events")
    else:
        h1 = [e for e in events if e[0].time < split]
        h2 = [e for e in events if e[0].time >= split]
        report(h1, label=f"H1 (< {split})")
        report(h2, label=f"H2 (>= {split})")


@cli.group("strategy", invoke_without_command=True)
@click.pass_context
def strategy_group(ctx: click.Context) -> None:
    """Named strategies — reproducible end-to-end runs.

    Run without a subcommand to see the list. See ``brn strategy list``
    for the same list and ``brn strategy run NAME`` to execute one.
    """
    if ctx.invoked_subcommand is None:
        _print_strategy_list()


@strategy_group.command("list")
def strategy_list_cmd() -> None:
    """List all named strategies."""
    _print_strategy_list()


def _print_strategy_list() -> None:
    click.echo("Named strategies:")
    for name, cfg in sorted(STRATEGIES.items()):
        first = cfg.description.strip().splitlines()[0] if cfg.description else ""
        click.echo(f"  {name:<12}  {cfg.instrument:<9}  {first}")
    click.echo()
    click.echo("Run with:   brn strategy run NAME")
    click.echo("Details:    brn strategy info NAME")


@strategy_group.command("info")
@click.argument("name", type=click.Choice(list(STRATEGIES)))
def strategy_info_cmd(name: str) -> None:
    """Print the full parameter set for one strategy."""
    _print_strategy_info(get_strategy(name))


def _print_strategy_info(cfg) -> None:
    click.echo(f"Strategy: {cfg.name}")
    click.echo(f"Instrument: {cfg.instrument} ({cfg.granularity})")
    click.echo()
    click.echo(cfg.description.rstrip())
    click.echo()
    click.echo("Parameters:")
    click.echo(f"  Signal grid:          {cfg.grid} (every {int(cfg.grid/cfg.pip)} pips)")
    click.echo(f"  Cooldown:             {cfg.cooldown_bars} bars")
    click.echo(f"  Filter:               {cfg.filter_name}")
    click.echo(f"  Entry:                {cfg.entry} + {cfg.entry_offset} bar(s) wait")
    if cfg.limit_offset_pips > 0:
        click.echo(f"  Order:                limit at {cfg.limit_offset_pips:g}p favorable, "
                   f"fill window {cfg.limit_fill_window} bars")
    else:
        click.echo("  Order:                market at close")
    click.echo(f"  Target:               {cfg.target_pips:g} pips")
    click.echo(f"  Stop:                 {cfg.stop_pips:g} pips")
    click.echo(f"  Max hold:             {cfg.max_bars} bars")
    click.echo(f"  Spread cost:          {cfg.spread_pips:g} pips round-trip")
    if cfg.breakeven_trigger_pips > 0:
        click.echo(f"  Breakeven trigger:    +{cfg.breakeven_trigger_pips:g}p")
    if cfg.trail_trigger_pips > 0:
        click.echo(f"  Trailing stop:        +{cfg.trail_trigger_pips:g}p trigger, "
                   f"{cfg.trail_distance_pips:g}p distance")
    if cfg.max_sma_slope_pips is not None:
        click.echo(f"  Skip if |SMA slope| > {cfg.max_sma_slope_pips:g}p")
    if cfg.reject_hours_utc:
        click.echo(f"  Reject entry hours (UTC): {sorted(cfg.reject_hours_utc)}")
    if cfg.reference_pnl_10y:
        click.echo()
        click.echo("Reference 10y performance:")
        click.echo(f"  {cfg.reference_pnl_10y}")


@strategy_group.command("run")
@click.argument("name", type=click.Choice(list(STRATEGIES)))
@click.option("--start", type=str, default=None,
              help="Restrict to signals on/after this ISO date (YYYY-MM-DD).")
@click.option("--end", type=str, default=None,
              help="Restrict to signals strictly before this ISO date.")
@click.option("--head", type=int, default=10, show_default=True,
              help="Trades to print in the tail table.")
@click.option("--export", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write per-trade rows to CSV.")
@click.pass_context
def strategy_run_cmd(
    ctx: click.Context,
    name: str,
    start: str | None,
    end: str | None,
    head: int,
    export: Path | None,
) -> None:
    """Run a named strategy end-to-end and print the summary."""
    cfg = get_strategy(name)
    app_cfg = ctx.obj["config"]

    click.echo(f"Strategy: {cfg.name}   Instrument: {cfg.instrument} ({cfg.granularity})")
    if cfg.reference_pnl_10y:
        click.echo(f"Reference: {cfg.reference_pnl_10y}")
    click.echo()

    with connect(app_cfg.db_path) as conn:
        bars = fetch_candles(
            conn, cfg.instrument, cfg.granularity,
            limit=None, order="asc", complete_only=True,
        )
    if not bars:
        click.echo(
            f"No {cfg.granularity} bars stored for {cfg.instrument} — "
            f"run `brn download {cfg.instrument} --granularity {cfg.granularity}` first."
        )
        return

    click.echo(f"Bars: {len(bars):,} ({bars[0].time[:10]} → {bars[-1].time[:10]})")
    click.echo("Analyzing signals…")

    events = list(analyze(
        bars, grid=cfg.grid, cooldown_bars=cfg.cooldown_bars,
        forward_bars=cfg.forward_bars, pip=cfg.pip,
    ))
    click.echo(f"  {len(events)} raw signals")

    if start is not None:
        events = [e for e in events if e[0].time >= start]
    if end is not None:
        events = [e for e in events if e[0].time < end]
    if start or end:
        click.echo(f"  {len(events)} after date filter ({start or 'start'} → {end or 'end'})")

    click.echo("Backtesting…")
    trades = backtest_touches(
        bars, events,
        pip=cfg.pip,
        filter_name=cfg.filter_name,
        entry=cfg.entry,  # type: ignore[arg-type]
        entry_offset=cfg.entry_offset,
        target_pips=cfg.target_pips, stop_pips=cfg.stop_pips,
        target_atr=cfg.target_atr, stop_atr=cfg.stop_atr,
        max_bars=cfg.max_bars,
        path_ambiguity=cfg.path_ambiguity,  # type: ignore[arg-type]
        spread_pips=cfg.spread_pips,
        limit_offset_pips=cfg.limit_offset_pips,
        limit_fill_window=cfg.limit_fill_window,
        breakeven_trigger_pips=cfg.breakeven_trigger_pips,
        trail_trigger_pips=cfg.trail_trigger_pips,
        trail_distance_pips=cfg.trail_distance_pips,
        max_sma_slope_pips=cfg.max_sma_slope_pips,
    )

    if cfg.reject_hours_utc:
        before = len(trades)
        trades = [t for t in trades
                  if int(t.entry_time[11:13]) not in cfg.reject_hours_utc]
        click.echo(f"  hour filter dropped {before - len(trades)} trades")

    stats = summarize_trades(trades, pip=cfg.pip)
    click.echo()

    if stats["n"] == 0:
        click.echo("No trades to report.")
        return

    click.echo(
        f"trades: {stats['n']}   win rate: {stats['win_rate']:.1f}%   "
        f"expectancy: {stats['expectancy_pips']:+.2f} pips/trade   "
        f"total: {stats['total_pips']:+.1f} pips"
    )
    click.echo(
        f"avg win: {stats['avg_win_pips']:+.1f} pips   "
        f"avg loss: {stats['avg_loss_pips']:+.1f} pips   "
        f"max drawdown: {stats['max_drawdown_pips']:.1f} pips"
    )
    click.echo(
        f"exits: target={stats['target']}  stop={stats['stop']}  "
        f"timeout={stats['timeout']}   avg hold: {stats['avg_hold_bars']:.1f} bars"
    )

    if head > 0 and trades:
        click.echo("")
        click.echo(
            f"{'entry_time':<30}  dir    lvl     entry     exit   "
            f"{'pnl':>6}  reason   hold"
        )
        for t in trades[-head:]:
            click.echo(
                f"{t.entry_time:<30}  {t.direction:<5}  {t.level:>6.4f}  "
                f"{t.entry_price:>7.5f}  {t.exit_price:>7.5f}  "
                f"{t.pnl_price / cfg.pip:>+6.1f}  {t.exit_reason:<7}  {t.hold_bars:>4d}"
            )
        click.echo(f"(showing last {min(head, len(trades))} of {len(trades)})")

    if export is not None:
        _export_trades(export, trades, pip=cfg.pip)
        click.echo(f"\nwrote {len(trades)} trades to {export}")


@cli.group("live")
@click.pass_context
def live_group(ctx: click.Context) -> None:
    """Live trading (paper mode only for now — no orders are placed)."""


@live_group.command("watch")
@click.argument("strategy_name", type=click.Choice(list(STRATEGIES)))
@click.option("--interval", type=int, default=60, show_default=True,
              help="Seconds between Oanda polls.")
@click.option("--log-file", type=click.Path(dir_okay=False, path_type=Path),
              default=Path("data/paper.log"), show_default=True)
@click.option("--state-file", type=click.Path(dir_okay=False, path_type=Path),
              default=Path("data/paper_state.json"), show_default=True)
@click.pass_context
def live_watch_cmd(
    ctx: click.Context,
    strategy_name: str,
    interval: int,
    log_file: Path,
    state_file: Path,
) -> None:
    """Detect signals live and simulate limit-fill + target-or-stop in PAPER MODE.

    No orders are ever placed. Reads bars from Oanda, writes bars to the
    same local SQLite the backtester uses, logs every stage as text-line
    events. Ctrl+C shuts down cleanly and saves state so a restart picks
    up where it left off.
    """
    cfg = get_strategy(strategy_name)
    app_cfg = ctx.obj["config"]
    secrets = load_secrets()

    trader = PaperTrader(
        strategy=cfg,
        secrets=secrets,
        db_path=Path(app_cfg.db_path),
        state_path=state_file,
        log_path=log_file,
        poll_seconds=interval,
    )
    click.echo(
        f"Paper-mode live watcher for '{cfg.name}' ({cfg.instrument} "
        f"{cfg.granularity}). Polling every {interval}s."
    )
    click.echo(f"Log:   {log_file}")
    click.echo(f"State: {state_file}")
    click.echo("Ctrl+C to stop. No orders will be placed.\n")
    trader.run()


def _iso_to_dt(s: str) -> datetime:
    """Parse a stored candle time back into a UTC datetime for range queries."""
    from .oanda import _parse_rfc3339  # local import avoids a public re-export

    return _parse_rfc3339(s)


if __name__ == "__main__":
    cli()
