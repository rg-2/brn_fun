# brn_fun

Trade around big round numbers.

An Oanda-backed data pipeline, a round-number bounce strategy, and a
backtester/analyzer to study its behavior. The tour of what's been built and
found is in [`findings.md`](./findings.md); the settled AUD_USD strategy is
documented at [`docs/audusd/STRATEGY.md`](./docs/audusd/STRATEGY.md).

## Quickstart

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
# 1. Install / sync the environment
uv sync

# 2. Configure Oanda secrets
cp .env.example .env
$EDITOR .env      # fill in OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_ENV

# 3. (Optional) tune config.yaml — instruments, default granularity, DB path

# 4. Pull data. For the AUD_USD strategy you need 10y of M1 bars:
uv run brn download AUD_USD --granularity M1 --from 2016-01-01

# 5. Run the strategy
uv run brn strategy run audusd
```

## Command index

Every top-level command supports `--help`. Highlights:

### Data

| Command | Purpose |
|---|---|
| `brn download INSTRUMENT [--from YYYY-MM-DD] [--granularity M1]` | Fetch bars from Oanda into SQLite. Resume-from-latest by default. |
| `brn download-all` | Run `download` for every instrument in `config.yaml`. |
| `brn status` | Show what's stored locally, per instrument. |
| `brn show INSTRUMENT [--tail N \| --head N]` | Print stored bars as a table. |

### Analysis

| Command | Purpose |
|---|---|
| `brn touches INSTRUMENT [--tier handle\|half\|figure]` | Find round-number "first-touch-in-a-while" events, tag each with bounce / break / chop and rich Context features. |
| `brn reaction INSTRUMENT` | Per-pair reaction study: how fast/deep bounces develop; hit-rate tables at various targets. |
| `brn plot INSTRUMENT` | Multi-panel PDF of backtest trades split H1/H2. |

### Backtesting

| Command | Purpose |
|---|---|
| `brn backtest INSTRUMENT [--target-pips 60] [--stop-pips 30] [--filter NAME]` | One-off backtest with full control over every knob (spread, limit offset, stop management, trend filter, etc.). |
| `brn strategy list` | List registered named strategies. |
| `brn strategy info NAME` | Show a strategy's full parameter set. |
| `brn strategy run NAME [--start YYYY-MM-DD] [--end ...] [--export CSV]` | Run a named strategy end-to-end with the settled parameters. |

## Anatomy of a strategy run

When you run `brn strategy run audusd`, this is what happens:

### 1. Startup (~instant)

- Reads `config.yaml` for the DB path.
- Looks up `STRATEGIES["audusd"]` — the frozen `StrategyConfig` dataclass in
  [`src/brn_fun/strategy.py`](./src/brn_fun/strategy.py).
- Prints the strategy name and the reference 10-year performance so you know
  what to compare the current run against.

### 2. Load bars (~1 s)

- Opens `data/brn_fun.sqlite`.
- Fetches every M1 bar for AUD_USD in chronological order — about **3.6 million
  bars** covering 2016 → today.
- Skips the currently-forming (incomplete) bar at the end.

### 3. Detect signals (~40 s — the slow step)

Calls `analyze()`, which walks every bar looking for round-number touches:

- For each bar, computes which round-number levels sit inside its `[low, high]`
  range at the 0.01 grid (every 100 pips).
- Tracks *when* each level was last touched.
- Emits a `Touch` event if the bar's range contains a level **and** that level
  hasn't been touched in the previous 7,200 bars (5 trading days).
- Direction inferred from the previous bar's close:
  - Prior close *below* the level → "up-touch" → expected rejection **down** → **SHORT** bet
  - Prior close *above* → "down-touch" → expected bounce **up** → **LONG** bet
- Also computes rich `Context` features (ATR, hour, SMA slope, approach range,
  etc.) and a `Confirmation` snapshot of the next bar.

Result on AUD_USD: **~395 raw touch events** over 10 years. Optionally filtered
by `--start` / `--end` dates.

### 4. Backtest each event (~5 s)

`backtest_touches` runs this loop for every signal:

1. **Compute the signal bar**: `touch.idx + 1 + 14` — i.e. **15 M1 bars past
   the touch** (the 15-minute confirmation wait).
2. **Signal price** = close of that bar.
3. **Set direction**: up-touch → SHORT, down-touch → LONG.
4. **Place a limit order at 2 pips FAVORABLE to signal price**:
   - LONG: `signal_price − 2p` (buy the dip)
   - SHORT: `signal_price + 2p` (sell the rally)
5. **Watch the next 60 bars** (1 hour) for a fill:
   - LONG fills when a bar's `low <= limit_price`
   - SHORT fills when a bar's `high >= limit_price`
   - **Not filled in 60 bars → cancel and skip.** This is why 395 signals turn
     into 328 trades — 67 limits never triggered.
6. **Set target and stop** relative to the fill price:
   - LONG: `target = fill + 60p`, `stop = fill − 30p`
   - SHORT: `target = fill − 60p`, `stop = fill + 30p`
7. **Walk forward up to 1,440 more bars** (24 h) looking for an exit:
   - Check every bar's `[low, high]` for target or stop hit.
   - **Same-bar ambiguity**: if a single bar contains BOTH target and stop, we
     assume the stop hit first (`path_ambiguity=worst`) — the conservative call.
   - Neither hits in 1,440 bars → close at the last bar's close (`timeout`).
8. **Compute P&L**: `exit_price − entry_price` (signed by direction), then
   **subtract 1 pip for spread cost**.
9. Record everything as a `Trade` object.

### 5. Summarize + print (~instant)

- Aggregates win rate, expectancy, average win / loss, max drawdown, exit reason
  counts, average hold time.
- Prints the summary, then the last N trades in a table.
- If `--export path.csv` was passed: writes every trade with entry / target /
  stop / exit prices and reason.

## What does NOT happen

- **No live orders placed.** This is 100% simulated backtest against stored
  historical bars.
- **No new data fetched.** Only uses bars already in SQLite. If your data is
  stale, `brn download` first.
- **No optimization / tuning at run-time.** The parameters are frozen at
  `STRATEGIES[name]`. If you want to explore a variant, use `brn backtest` with
  explicit flags.
- **No plots.** For the visual report, see
  [`docs/audusd/STRATEGY.md`](./docs/audusd/STRATEGY.md) or regenerate with
  `uv run python analysis/audusd_report.py`.

## Layout

```
brn_fun/
├── README.md             # this file
├── findings.md           # running log of research findings across sessions
├── project_plan.md       # original project pitch
├── config.yaml           # instruments, granularity, DB path
├── .env                  # Oanda credentials (gitignored)
├── data/                 # local SQLite + intermediate PDFs (gitignored)
│   └── brn_fun.sqlite
├── docs/                 # long-form, GitHub-rendered documentation
│   └── audusd/           # AUD_USD strategy report + plots + trade examples
├── src/brn_fun/
│   ├── config.py         # YAML + env loader
│   ├── db.py             # SQLite schema + upsert / fetch helpers
│   ├── oanda.py          # candle downloader with pagination
│   ├── analyze.py        # touch detection + Context / Confirmation / Outcome
│   ├── reaction.py       # per-event forward reaction profile
│   ├── backtest.py       # simulate_trade + backtest_touches + summary
│   ├── plot.py           # multi-panel PDF trade plots
│   ├── strategy.py       # StrategyConfig + registered strategies
│   ├── levels.py         # prev-day / prev-week H/L signal detection
│   └── cli.py            # the `brn` command group
├── analysis/             # research scripts (not shipped as part of the CLI)
│   ├── sweep_oos.py      # M15 out-of-sample robustness sweep
│   ├── sweep_m1.py       # M1 equivalent
│   ├── rolling_check.py  # portfolio equity curves, monthly / yearly P&L
│   ├── rolling_atr_filter.py     # walk-forward vol-regime filter test
│   ├── year_profile.py           # per-year regime characterization
│   ├── hour_of_day.py            # hour-of-entry P&L split
│   ├── spread_limit_sweep.py     # cost + limit-offset sweep
│   ├── audusd_report.py          # generates docs/audusd/
│   └── audusd_stops_trend.py     # BE / trailing / trend-filter sweep
└── tests/                # pytest suite (41 tests as of this writing)
```

## Development

```bash
uv run pytest       # tests
uv run ruff check   # lint
```
