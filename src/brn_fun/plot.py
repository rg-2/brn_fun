"""Multi-panel PDF plots of backtested trades.

For each trade, we render:
  - Candlestick bars for ``context_before`` bars up to entry and through
    the hold window.
  - A dashed horizontal line at the round-number ``level``.
  - Dotted horizontal lines at ``target_price`` (green) and ``stop_price``
    (red).
  - Entry marker: black triangle (up for long, down for short) at the
    entry bar's close.
  - Exit marker: circle at the exit bar, colored by exit reason
    (green=target, red=stop, gray=timeout).
  - Title with entry time, direction, PnL in pips, and exit reason.

Plots are compact — designed for a 4×3 or 5×4 grid so a page can hold
12–20 trades side-by-side. Layout intentionally strips axis ticks; use
the CLI's ``--head`` output for the numeric details.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle

from .backtest import Trade
from .db import Candle


def _draw_candles(ax, bars: Sequence[Candle]) -> None:
    """Bare-bones candle rendering: black wick line + colored body rect."""
    for i, b in enumerate(bars):
        # Wick from low to high.
        ax.plot([i, i], [b.low, b.high], color="black", lw=0.4, zorder=1)
        # Body from open to close.
        body_bottom = min(b.open, b.close)
        body_height = max(abs(b.close - b.open), 1e-9)
        color = "#2ca02c" if b.close >= b.open else "#d62728"
        ax.add_patch(Rectangle(
            (i - 0.35, body_bottom), 0.7, body_height,
            facecolor=color, edgecolor="black", lw=0.2, zorder=2,
        ))


def plot_trade(
    ax,
    bars: Sequence[Candle],
    trade: Trade,
    *,
    pip: float = 0.0001,
    context_before: int = 40,
    context_after_extra: int = 8,
) -> None:
    """Render one trade panel on ``ax``.

    ``context_before`` bars are shown before entry; the hold window plus
    ``context_after_extra`` extra bars are shown after entry.
    """
    entry_idx = trade.entry_idx
    exit_idx = trade.exit_idx
    start = max(0, entry_idx - context_before)
    end = min(len(bars), exit_idx + context_after_extra + 1)
    window = bars[start:end]

    _draw_candles(ax, window)

    # Level (blue dashed), target (green dotted), stop (red dotted).
    ax.axhline(trade.level, color="#1f77b4", ls="--", lw=0.8, zorder=0)
    ax.axhline(trade.target_price, color="#2ca02c", ls=":", lw=0.7, zorder=0)
    ax.axhline(trade.stop_price,   color="#d62728", ls=":", lw=0.7, zorder=0)

    # Entry marker at close of entry bar.
    entry_rel = entry_idx - start
    marker = "^" if trade.direction == "long" else "v"
    ax.plot(entry_rel, trade.entry_price,
            marker=marker, mfc="black", mec="black", ms=7, zorder=5)

    # Exit marker at exit bar.
    exit_rel = exit_idx - start
    exit_color = {
        "target": "#2ca02c",
        "stop":   "#d62728",
        "timeout": "#7f7f7f",
    }[trade.exit_reason]
    ax.plot(exit_rel, trade.exit_price,
            marker="o", mfc=exit_color, mec="black", ms=7, zorder=5)

    # Tight title: date, direction, pnl, exit reason.
    dt = trade.entry_time[:16].replace("T", " ")
    pnl_pips = trade.pnl_price / pip
    ax.set_title(
        f"{dt}  {trade.direction} {pnl_pips:+.0f}p ({trade.exit_reason})",
        fontsize=7, pad=2,
    )
    ax.set_xticks([])
    ax.set_yticks([])
    # Slim margin so candles fill the panel.
    ax.set_xlim(-0.5, len(window) - 0.5)


def _sample_evenly(items: list, n: int) -> list:
    """Return up to n items evenly spaced through ``items`` (chronological)."""
    if len(items) <= n:
        return items[:]
    if n <= 1:
        return items[:1]
    step = (len(items) - 1) / (n - 1)
    return [items[int(round(i * step))] for i in range(n)]


def plot_trades_pdf(
    bars: Sequence[Candle],
    trades_by_half: dict[str, list[Trade]],
    out_path: Path,
    *,
    pip: float = 0.0001,
    cols: int = 4,
    rows: int = 3,
    context_before: int = 40,
    title_prefix: str = "",
) -> int:
    """Write a PDF with each half's trades on its own group of pages.

    ``trades_by_half`` maps a label (e.g. "H1 (2016-2020)") to a list of
    sampled trades. Each half's pages start with a page-header string.
    Returns the total number of trades plotted.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    per_page = cols * rows
    plotted = 0

    with PdfPages(out_path) as pdf:
        for half_label, trades in trades_by_half.items():
            if not trades:
                # Empty page indicating no trades this half.
                fig = plt.figure(figsize=(cols * 3, rows * 2.2))
                fig.suptitle(f"{title_prefix} — {half_label} — no trades",
                             fontsize=12, y=0.5)
                pdf.savefig(fig)
                plt.close(fig)
                continue

            n_pages = (len(trades) + per_page - 1) // per_page
            for p in range(n_pages):
                fig, axes = plt.subplots(rows, cols, figsize=(cols * 3, rows * 2.2))
                # Keep axes indexable even with rows=1 or cols=1.
                if rows == 1 and cols == 1:
                    axes = [axes]
                else:
                    axes = axes.flatten()

                for j in range(per_page):
                    idx = p * per_page + j
                    if idx >= len(trades):
                        axes[j].axis("off")
                        continue
                    plot_trade(
                        axes[j], bars, trades[idx],
                        pip=pip, context_before=context_before,
                    )
                    plotted += 1

                header = f"{title_prefix}  {half_label}  page {p+1}/{n_pages}"
                fig.suptitle(header, fontsize=10, y=0.995)
                fig.tight_layout(rect=(0, 0, 1, 0.97))
                pdf.savefig(fig)
                plt.close(fig)

    return plotted


def sample_by_half(
    trades: list[Trade], split_time: str, n_per_half: int,
) -> dict[str, list[Trade]]:
    """Split trades at ``split_time`` and evenly-sample up to n from each half."""
    h1 = [t for t in trades if t.entry_time < split_time]
    h2 = [t for t in trades if t.entry_time >= split_time]
    return {
        f"H1 (< {split_time[:10]})": _sample_evenly(h1, n_per_half),
        f"H2 (≥ {split_time[:10]})": _sample_evenly(h2, n_per_half),
    }
