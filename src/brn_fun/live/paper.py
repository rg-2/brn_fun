"""Paper-mode live signal detector.

Polls Oanda every ``poll_seconds`` for new M1 bars, upserts into the
existing candles table, then runs incremental round-number touch
detection using the same rules as the backtester. When a touch fires,
tracks the confirmation window; after ``entry_offset + 1`` bars,
computes the limit price and expected target/stop, and simulates
whether the limit would have filled and how the trade would have
exited — logging every stage as a text-line event.

**No orders are ever placed.** Safe to run against any Oanda credential,
including live accounts, because this module only calls the read-only
candle endpoints via the existing ``download_range`` helper.

State (last processed bar, per-level last-touched, pending signals,
open hypothetical trades) is persisted to a JSON file so restarts pick
up cleanly.
"""

from __future__ import annotations

import json
import logging
import math
import signal
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from ..config import Secrets
from ..db import Candle, connect, fetch_candles, upsert_candles
from ..oanda import _parse_rfc3339, download_range
from ..strategy import StrategyConfig

log = logging.getLogger(__name__)


# ---------- Persisted state --------------------------------------------------


@dataclass
class PendingSignal:
    """A detected touch waiting for the confirmation window to elapse."""

    signal_time: str          # bar time that touched the level
    signal_idx: int           # bar index (in the loaded bars sequence)
    level: float
    direction: Literal["up", "down"]   # touch approach direction


@dataclass
class OpenTrade:
    """A hypothetical open position: limit filled, awaiting target/stop/timeout."""

    entry_time: str
    entry_idx: int
    entry_price: float
    direction: Literal["long", "short"]
    level: float
    target_price: float
    stop_price: float
    exit_by_idx: int         # bar index at which timeout would fire


@dataclass
class PaperState:
    """Everything we need to resume across process restarts."""

    strategy_name: str
    instrument: str
    granularity: str
    last_processed_time: str = ""          # RFC3339 of the last bar we handled
    last_touch: dict[str, str] = field(default_factory=dict)
                                            # level (as str) → last-touched RFC3339
    pending: list[dict] = field(default_factory=list)   # serialized PendingSignal
    open_trades: list[dict] = field(default_factory=list)  # serialized OpenTrade

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load_or_new(cls, path: Path, strategy_name: str,
                     instrument: str, granularity: str) -> "PaperState":
        if path.exists():
            data = json.loads(path.read_text())
            # Trust the file; if it's for a different strategy, fail loudly.
            if data.get("strategy_name") != strategy_name:
                raise ValueError(
                    f"State file at {path} was written for strategy "
                    f"{data.get('strategy_name')!r}, not {strategy_name!r}. "
                    "Move or delete it before running a different strategy."
                )
            return cls(**data)
        return cls(strategy_name=strategy_name, instrument=instrument,
                   granularity=granularity)


# ---------- Helpers ---------------------------------------------------------


def _round_levels_in(low: float, high: float, grid: float) -> list[float]:
    """Round-number levels in the closed [low, high] range at spacing ``grid``.

    Duplicated from analyze._round_levels_in (avoiding cross-module private
    import). Small function, worth the tiny duplication for isolation.
    """
    if grid <= 0:
        raise ValueError("grid must be > 0")
    eps = grid * 1e-9
    first = math.ceil((low - eps) / grid) * grid
    last = math.floor((high + eps) / grid) * grid
    if first > last:
        return []
    n = int(round((last - first) / grid)) + 1
    decimals = max(0, -int(math.floor(math.log10(grid))) + 4)
    return [round(first + i * grid, decimals) for i in range(n)]


def _cooldown_bars_between(a: str, b: str) -> int:
    """Number of M1 bars between two RFC3339 timestamps (a earlier than b)."""
    da = _parse_rfc3339(a)
    db = _parse_rfc3339(b)
    return max(0, int((db - da).total_seconds() // 60))


# ---------- Log formatting --------------------------------------------------


def _log_event(fh, event: str, **fields) -> None:
    """Append one human-readable event line to the log file."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    line = f"{ts}  {event:<12}  {parts}"
    print(line, flush=True)
    if fh is not None:
        fh.write(line + "\n")
        fh.flush()


# ---------- Paper trader ----------------------------------------------------


class PaperTrader:
    """Polls Oanda, detects touches, simulates limit fills + exits."""

    def __init__(
        self,
        strategy: StrategyConfig,
        secrets: Secrets,
        db_path: Path,
        state_path: Path,
        log_path: Path | None,
        poll_seconds: int = 60,
        # How far back to load bars for the working window in memory. Needs
        # to comfortably exceed cooldown + entry_offset + max_bars so that
        # per-level cooldowns and open trades can be resolved without
        # database re-queries mid-loop.
        working_window_bars: int | None = None,
    ) -> None:
        self.strategy = strategy
        self.secrets = secrets
        self.db_path = db_path
        self.state_path = state_path
        self.log_path = log_path
        self.poll_seconds = poll_seconds
        self.working_window_bars = working_window_bars or (
            strategy.cooldown_bars + strategy.max_bars + strategy.limit_fill_window + 60
        )

        self.state = PaperState.load_or_new(
            state_path, strategy.name, strategy.instrument, strategy.granularity,
        )
        self._stop = False
        self._log_fh = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = log_path.open("a", buffering=1)

    # ------------------------------------------------------------------ shutdown

    def request_stop(self, *_args) -> None:
        if self._stop:
            return  # Multiple signals in quick succession — log once.
        _log_event(self._log_fh, "stop", note="SIGINT/SIGTERM received")
        self._stop = True

    def close(self) -> None:
        if self._log_fh is not None:
            self._log_fh.close()

    # ------------------------------------------------------------------ data

    def _fetch_new_bars(self) -> int:
        """Ask Oanda for bars since the last one we have. Return count written."""
        with connect(self.db_path) as conn:
            # Find the latest bar we already stored for this instrument.
            row = conn.execute(
                "SELECT MAX(time) FROM candles WHERE instrument = ? AND granularity = ?",
                (self.strategy.instrument, self.strategy.granularity),
            ).fetchone()
            latest = row[0] if row and row[0] else None
        if latest is None:
            # Cold start: pull enough to cover the working window.
            end = datetime.now(timezone.utc)
            start = end - timedelta(minutes=self.working_window_bars)
        else:
            start = _parse_rfc3339(latest) + timedelta(microseconds=1)
            end = datetime.now(timezone.utc)
        if start >= end:
            return 0

        new_candles = list(download_range(
            self.secrets, self.strategy.instrument, self.strategy.granularity,
            start, end,
        ))
        # Filter to complete bars only — never act on the currently-forming bar.
        complete = [c for c in new_candles if c.complete]
        if complete:
            with connect(self.db_path) as conn:
                upsert_candles(conn, complete)
        return len(complete)

    def _load_working_bars(self) -> list[Candle]:
        """Load the most recent ``working_window_bars`` complete bars from SQLite."""
        with connect(self.db_path) as conn:
            bars = fetch_candles(
                conn, self.strategy.instrument, self.strategy.granularity,
                limit=self.working_window_bars, order="desc",
                complete_only=True,
            )
        bars.reverse()  # chronological
        return bars

    # ------------------------------------------------------------------ logic

    def _pip(self) -> float:
        return self.strategy.pip

    def _detect_touches(self, bars: list[Candle], start_from_time: str) -> list[PendingSignal]:
        """Scan bars strictly after ``start_from_time`` for new round-number touches."""
        signals: list[PendingSignal] = []
        grid = self.strategy.grid
        cooldown_bars = self.strategy.cooldown_bars

        # Rebuild the "last touched" map from persisted state.
        last_touch_time = dict(self.state.last_touch)  # level (str) → time

        for i, bar in enumerate(bars):
            if start_from_time and bar.time <= start_from_time:
                continue
            levels = _round_levels_in(bar.low, bar.high, grid)
            if not levels:
                continue
            # Need a prior close to infer direction; skip the very first bar
            # if there isn't one.
            if i == 0:
                continue
            prev_close = bars[i - 1].close

            for lvl in levels:
                key = f"{lvl:.5f}"
                prev_time = last_touch_time.get(key)
                if prev_time is not None:
                    bars_since = _cooldown_bars_between(prev_time, bar.time)
                    if bars_since < cooldown_bars:
                        # Suppress but still refresh the last-touched time.
                        last_touch_time[key] = bar.time
                        continue
                direction: Literal["up", "down"] = (
                    "up" if prev_close < lvl else "down"
                )
                signals.append(PendingSignal(
                    signal_time=bar.time, signal_idx=i,
                    level=lvl, direction=direction,
                ))
                last_touch_time[key] = bar.time

        # Persist updated last-touch map (only if we actually processed bars).
        self.state.last_touch = last_touch_time
        return signals

    def _promote_pending(self, bars: list[Candle]) -> list[OpenTrade]:
        """Turn pending signals whose confirmation window has elapsed into open trades."""
        newly_open: list[OpenTrade] = []
        still_pending: list[dict] = []
        # Wait 1 bar (confirm) + entry_offset extra bars.
        needed_offset = 1 + self.strategy.entry_offset

        # Fast index lookup from bar time → position in bars.
        idx_of_time = {b.time: i for i, b in enumerate(bars)}

        for raw in self.state.pending:
            sig = PendingSignal(**raw)
            entry_i = idx_of_time.get(sig.signal_time)
            if entry_i is None:
                # Signal is older than our working window — drop it.
                _log_event(
                    self._log_fh, "DROP-PENDING",
                    level=f"{sig.level:.5f}",
                    signal_time=sig.signal_time,
                    reason="signal older than working window",
                )
                continue
            target_idx = entry_i + needed_offset
            if target_idx >= len(bars):
                # Confirmation window hasn't closed yet — keep waiting.
                still_pending.append(raw)
                continue

            # Place the limit right now (in paper).
            signal_bar = bars[target_idx]
            signal_price = signal_bar.close
            trade_dir: Literal["long", "short"] = (
                "short" if sig.direction == "up" else "long"
            )
            limit_offset = self.strategy.limit_offset_pips * self._pip()
            if trade_dir == "long":
                limit_price = signal_price - limit_offset
            else:
                limit_price = signal_price + limit_offset

            _log_event(
                self._log_fh, "LIMIT-SIM",
                dir=trade_dir, level=f"{sig.level:.5f}",
                signal_time=signal_bar.time,
                signal_price=f"{signal_price:.5f}",
                limit_price=f"{limit_price:.5f}",
                fill_window=self.strategy.limit_fill_window,
            )

            # Try to fill within the fill window using bars we already have.
            fill_end_idx = min(target_idx + self.strategy.limit_fill_window,
                                len(bars) - 1)
            filled = self._try_fill_limit(bars, target_idx, fill_end_idx,
                                            trade_dir, limit_price, sig.level)
            if filled is None and fill_end_idx == len(bars) - 1:
                # Ran out of bars — carry as pending-fill on next poll.
                still_pending.append(raw)  # keep same pending signal; we'll retry
                continue
            if filled is None:
                _log_event(
                    self._log_fh, "CANCEL-SIM",
                    dir=trade_dir, level=f"{sig.level:.5f}",
                    reason=f"limit not filled within {self.strategy.limit_fill_window} bars",
                )
                continue
            newly_open.append(filled)

        self.state.pending = still_pending
        return newly_open

    def _try_fill_limit(
        self, bars: list[Candle], start_idx: int, end_idx: int,
        direction: Literal["long", "short"], limit_price: float, level: float,
    ) -> OpenTrade | None:
        """Search bars (start_idx+1, end_idx] for the first limit fill."""
        for j in range(start_idx + 1, end_idx + 1):
            bar = bars[j]
            filled = (
                bar.low <= limit_price if direction == "long"
                else bar.high >= limit_price
            )
            if not filled:
                continue
            target_dist = self.strategy.target_pips * self._pip()
            stop_dist = self.strategy.stop_pips * self._pip()
            if direction == "long":
                target_price = limit_price + target_dist
                stop_price = limit_price - stop_dist
            else:
                target_price = limit_price - target_dist
                stop_price = limit_price + stop_dist
            trade = OpenTrade(
                entry_time=bar.time, entry_idx=j,
                entry_price=limit_price, direction=direction, level=level,
                target_price=target_price, stop_price=stop_price,
                exit_by_idx=j + self.strategy.max_bars,
            )
            _log_event(
                self._log_fh, "FILL-SIM",
                dir=direction, level=f"{level:.5f}",
                fill_time=bar.time, entry=f"{limit_price:.5f}",
                target=f"{target_price:.5f}", stop=f"{stop_price:.5f}",
            )
            return trade
        return None

    def _resolve_open_trades(self, bars: list[Candle]) -> None:
        """Walk each open trade forward through the newly seen bars."""
        idx_of_time = {b.time: i for i, b in enumerate(bars)}
        still_open: list[dict] = []
        pip = self._pip()

        for raw in self.state.open_trades:
            trade = OpenTrade(**raw)
            entry_i = idx_of_time.get(trade.entry_time)
            if entry_i is None:
                _log_event(
                    self._log_fh, "DROP-OPEN",
                    entry_time=trade.entry_time,
                    reason="entry bar older than working window",
                )
                continue

            last_scan_i = max(entry_i, self._last_scanned_index_for(trade, bars))
            exit_by = trade.exit_by_idx
            exited = False

            for j in range(last_scan_i + 1, min(exit_by + 1, len(bars))):
                bar = bars[j]
                if trade.direction == "long":
                    hit_target = bar.high >= trade.target_price
                    hit_stop = bar.low <= trade.stop_price
                else:
                    hit_target = bar.low <= trade.target_price
                    hit_stop = bar.high >= trade.stop_price

                if hit_target and hit_stop:
                    # Worst-case path assumption, matching backtester.
                    exit_price = trade.stop_price
                    reason = "stop"
                elif hit_target:
                    exit_price = trade.target_price
                    reason = "target"
                elif hit_stop:
                    exit_price = trade.stop_price
                    reason = "stop"
                else:
                    continue

                gross = (
                    exit_price - trade.entry_price if trade.direction == "long"
                    else trade.entry_price - exit_price
                )
                pnl = gross - self.strategy.spread_pips * pip
                _log_event(
                    self._log_fh, "EXIT-SIM",
                    dir=trade.direction, level=f"{trade.level:.5f}",
                    exit_time=bar.time, exit_price=f"{exit_price:.5f}",
                    reason=reason, pnl_pips=f"{pnl/pip:+.1f}",
                    hold_bars=j - entry_i,
                )
                exited = True
                break

            if exited:
                continue
            if bars[-1] and idx_of_time.get(bars[-1].time, 0) >= exit_by:
                # Timeout: close at last-scanned bar's close.
                last_bar = bars[min(exit_by, len(bars) - 1)]
                gross = (
                    last_bar.close - trade.entry_price if trade.direction == "long"
                    else trade.entry_price - last_bar.close
                )
                pnl = gross - self.strategy.spread_pips * pip
                _log_event(
                    self._log_fh, "TIMEOUT-SIM",
                    dir=trade.direction, level=f"{trade.level:.5f}",
                    exit_time=last_bar.time, exit_price=f"{last_bar.close:.5f}",
                    pnl_pips=f"{pnl/pip:+.1f}",
                    hold_bars=self.strategy.max_bars,
                )
                continue

            # Still open — save it back.
            still_open.append(raw)

        self.state.open_trades = still_open

    def _last_scanned_index_for(self, trade: OpenTrade, bars: list[Candle]) -> int:
        """Best-effort index into ``bars`` we last scanned. For simplicity we
        just rescan from entry each time — safe for correctness (we exit on
        first hit) at a small perf cost while open trades are open."""
        idx_of_time = {b.time: i for i, b in enumerate(bars)}
        return idx_of_time.get(trade.entry_time, 0)

    # ------------------------------------------------------------------ loop

    def poll_once(self) -> None:
        new_count = self._fetch_new_bars()
        bars = self._load_working_bars()
        if not bars:
            _log_event(self._log_fh, "poll", new_bars=new_count, working_bars=0)
            return

        # Detect new touches.
        signals = self._detect_touches(bars, self.state.last_processed_time)
        for s in signals:
            _log_event(
                self._log_fh, "SIGNAL",
                level=f"{s.level:.5f}", direction=s.direction,
                signal_time=s.signal_time,
            )
            self.state.pending.append(asdict(s))

        # Promote pending → open (fills the limit as of newly-arrived bars).
        newly_open = self._promote_pending(bars)
        for t in newly_open:
            self.state.open_trades.append(asdict(t))

        # Advance open trades forward.
        self._resolve_open_trades(bars)

        self.state.last_processed_time = bars[-1].time
        self.state.save(self.state_path)
        _log_event(
            self._log_fh, "poll",
            new_bars=new_count, working_bars=len(bars),
            signals=len(signals), pending=len(self.state.pending),
            open=len(self.state.open_trades),
            latest_bar=bars[-1].time,
        )

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        _log_event(
            self._log_fh, "START",
            strategy=self.strategy.name, instrument=self.strategy.instrument,
            granularity=self.strategy.granularity,
            poll_seconds=self.poll_seconds,
        )
        try:
            while not self._stop:
                try:
                    self.poll_once()
                except Exception as e:  # noqa: BLE001
                    _log_event(self._log_fh, "ERROR", err=repr(e))
                # Interruptible sleep.
                for _ in range(self.poll_seconds):
                    if self._stop:
                        break
                    time.sleep(1)
        finally:
            _log_event(self._log_fh, "STOP",
                        latest_bar=self.state.last_processed_time,
                        open_trades=len(self.state.open_trades),
                        pending=len(self.state.pending))
            self.close()
