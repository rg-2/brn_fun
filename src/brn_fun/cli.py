"""Command-line entry point.

Exposed as ``brn`` via ``[project.scripts]``. Everything is a thin shell around
functions in the other modules — keep argument parsing here, keep logic there.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import click

from .config import Granularity, load_config, load_secrets
from .db import connect, count_candles, latest_time, upsert_candles
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


def _iso_to_dt(s: str) -> datetime:
    """Parse a stored candle time back into a UTC datetime for range queries."""
    from .oanda import _parse_rfc3339  # local import avoids a public re-export

    return _parse_rfc3339(s)


if __name__ == "__main__":
    cli()
