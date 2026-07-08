"""Oanda candle downloader.

Wraps the ``InstrumentsCandles`` endpoint. Two ways to call it:

1. ``download_recent(instrument, granularity, count=500)`` — pull the most
   recent N bars. Handy for smoke tests and topping up.
2. ``download_range(instrument, granularity, start, end)`` — pull everything
   between two RFC3339 timestamps, paginating around Oanda's 5000-bar cap.

Both return an iterator of :class:`~brn_fun.db.Candle` — the caller decides
whether to persist, inspect, or discard.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Iterator

from oandapyV20 import API
from oandapyV20.endpoints.instruments import InstrumentsCandles
from oandapyV20.exceptions import V20Error

from .config import Granularity, Secrets
from .db import Candle

log = logging.getLogger(__name__)

# Oanda caps a single response at 5000 candles.
MAX_CANDLES_PER_REQUEST = 5000


def _client(secrets: Secrets) -> API:
    """Build an oandapyV20 client pointed at the right environment."""
    environment = "live" if secrets.env == "live" else "practice"
    return API(access_token=secrets.api_key, environment=environment)


def _parse_candle(
    instrument: str, granularity: str, price_key: str, raw: dict
) -> Candle:
    """Translate one candle dict from Oanda into our Candle record."""
    # Oanda returns e.g. {"time": "...", "volume": 42, "complete": True,
    # "mid": {"o": "...", "h": "...", "l": "...", "c": "..."}}
    # price_key is "mid", "bid", or "ask" — pick from what the endpoint returned.
    prices = raw[price_key]
    return Candle(
        instrument=instrument,
        granularity=granularity,
        time=raw["time"],
        open=float(prices["o"]),
        high=float(prices["h"]),
        low=float(prices["l"]),
        close=float(prices["c"]),
        volume=int(raw["volume"]),
        complete=bool(raw["complete"]),
    )


def _price_component(price: str) -> tuple[str, str]:
    """Map our single-letter code to (api_param, response_key).

    Oanda's ``price`` query param takes letters ("M"/"B"/"A"), but the
    response keys are "mid"/"bid"/"ask".
    """
    return {
        "M": ("M", "mid"),
        "B": ("B", "bid"),
        "A": ("A", "ask"),
    }[price]


def _request_with_retry(client: API, req: InstrumentsCandles) -> dict:
    """Send a request; back off once on rate-limit / transient errors."""
    for attempt in range(3):
        try:
            return client.request(req)
        except V20Error as e:
            # 429 = rate-limited, 5xx = transient. Retry with backoff.
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                delay = 2 ** attempt
                log.warning("Oanda %s, sleeping %ds before retry", e.code, delay)
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def download_recent(
    secrets: Secrets,
    instrument: str,
    granularity: Granularity,
    count: int = 500,
    price: str = "M",
) -> Iterator[Candle]:
    """Yield the most recent ``count`` candles for ``instrument``.

    ``count`` is capped at 5000 by Oanda — larger values would need pagination,
    which is what :func:`download_range` is for.
    """
    if count > MAX_CANDLES_PER_REQUEST:
        raise ValueError(
            f"count={count} exceeds Oanda's {MAX_CANDLES_PER_REQUEST} cap; "
            "use download_range() for larger pulls"
        )
    api_price, resp_key = _price_component(price)
    req = InstrumentsCandles(
        instrument=instrument,
        params={"granularity": granularity, "count": count, "price": api_price},
    )
    data = _request_with_retry(_client(secrets), req)
    for raw in data.get("candles", []):
        yield _parse_candle(instrument, granularity, resp_key, raw)


def download_range(
    secrets: Secrets,
    instrument: str,
    granularity: Granularity,
    start: datetime,
    end: datetime | None = None,
    price: str = "M",
) -> Iterator[Candle]:
    """Yield every candle in [start, end), paginating past the 5000-bar cap.

    Oanda's ``from``/``to`` are inclusive/exclusive respectively, and if the
    window would produce >5000 bars it returns 5000 starting at ``from``. We
    advance ``from`` to just after the last bar we received and keep going
    until the response is short (meaning we've caught up).
    """
    if end is None:
        end = datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    api_price, resp_key = _price_component(price)
    client = _client(secrets)
    cursor = start

    while cursor < end:
        req = InstrumentsCandles(
            instrument=instrument,
            params={
                "granularity": granularity,
                "from": _rfc3339(cursor),
                "to": _rfc3339(end),
                "count": MAX_CANDLES_PER_REQUEST,
                "price": api_price,
            },
        )
        data = _request_with_retry(client, req)
        raws = data.get("candles", [])
        if not raws:
            return

        last_time_str: str | None = None
        for raw in raws:
            candle = _parse_candle(instrument, granularity, resp_key, raw)
            yield candle
            last_time_str = candle.time

        # If Oanda didn't hit the cap, we've drained the window.
        if len(raws) < MAX_CANDLES_PER_REQUEST or last_time_str is None:
            return

        # Advance one microsecond past the last bar we got so the next request
        # doesn't re-fetch it. Oanda's `from` is inclusive.
        cursor = _parse_rfc3339(last_time_str)
        cursor = cursor.replace(microsecond=cursor.microsecond + 1)


def _rfc3339(dt: datetime) -> str:
    """Serialize to the ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` form Oanda expects."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_rfc3339(s: str) -> datetime:
    """Parse an Oanda timestamp back into a UTC-aware datetime."""
    # Oanda returns e.g. "2024-01-02T15:30:00.000000000Z" (nanosecond precision).
    # Python's fromisoformat handles up to microseconds, so trim excess digits.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, tail = s.split(".", 1)
        frac, tz = tail[:-6], tail[-6:]  # +00:00
        frac = (frac + "000000")[:6]
        s = f"{head}.{frac}{tz}"
    return datetime.fromisoformat(s)
