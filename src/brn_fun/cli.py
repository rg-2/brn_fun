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
from .config import Granularity, load_config, load_secrets
from .db import connect, count_candles, fetch_candles, latest_time, upsert_candles
from .oanda import download_range, download_recent


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
@click.option("--cooldown-bars", type=int, default=480, show_default=True,
              help="Bars a level must be untouched before it counts as fresh.")
@click.option("--forward-bars", type=int, default=96, show_default=True,
              help="Bars to look forward when tagging outcome.")
@click.option("--bounce-pips", type=float, default=30.0, show_default=True,
              help="Favorable move (pips) required to tag 'bounce'.")
@click.option("--break-pips", type=float, default=30.0, show_default=True,
              help="Adverse move (pips) required to tag 'break'.")
@click.option("--pip", type=float, default=0.0001, show_default=True,
              help="Pip size (0.0001 for majors, 0.01 for JPY pairs).")
@click.option("--complete-only/--all", default=True, show_default=True,
              help="Skip the currently-forming bar when reading history.")
@click.option("--export", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write per-touch rows to CSV.")
@click.option("--head", type=int, default=15, show_default=True,
              help="How many recent touches to print in the table.")
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
    pip: float,
    complete_only: bool,
    export: Path | None,
    head: int,
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
        bounce_pips=bounce_pips, break_pips=break_pips, pip=pip,
    ))

    summary = summarize_outcomes(iter(events))
    # `summarize_outcomes` consumed a fresh iterator; events list is intact.

    span_from = bars[0].time
    span_to = bars[-1].time
    click.echo(
        f"{instrument} {gran}   grid={grid_val:g}   cooldown={cooldown_bars} bars   "
        f"forward={forward_bars} bars"
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
        click.echo(
            f"{'time':<30}  {'level':>7}  {'dir':<4}  "
            f"{'fav':>6}  {'adv':>6}  {'outcome':<7}"
        )
        for t, o in events[-head:]:
            click.echo(
                f"{t.time:<30}  {t.level:>7.4f}  {t.direction:<4}  "
                f"{o.favorable / pip:>6.1f}  {o.adverse / pip:>6.1f}  {o.tag:<7}"
            )
        click.echo(f"(showing last {min(head, len(events))} of {len(events)})")

    if export is not None:
        _export_touches(export, events, pip=pip)
        click.echo(f"\nwrote {len(events)} rows to {export}")


def _export_touches(
    path: Path,
    events: list,  # list[tuple[Touch, Outcome_]]
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
            "favorable_pips", "adverse_pips", "close_after", "close_dist_pips",
            "window_bars", "outcome",
        ])
        for t, o in events:
            w.writerow([
                t.time, t.idx, f"{t.level:.5f}", t.direction, t.cooldown_bars,
                f"{o.favorable / pip:.1f}", f"{o.adverse / pip:.1f}",
                f"{o.close_after:.5f}", f"{o.close_dist / pip:.1f}",
                o.window_bars, o.tag,
            ])


def _iso_to_dt(s: str) -> datetime:
    """Parse a stored candle time back into a UTC datetime for range queries."""
    from .oanda import _parse_rfc3339  # local import avoids a public re-export

    return _parse_rfc3339(s)


if __name__ == "__main__":
    cli()
