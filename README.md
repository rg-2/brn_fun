# brn_fun

Trade around big round numbers.

See [`project_plan.md`](./project_plan.md) for the full pitch. This repo is the
Python implementation: an Oanda-backed data pipeline, a round-number bounce
strategy, and a backtester/analyzer to study its behavior.

## Status

Early scaffold. Right now you can:

- Download M15 (or other) candles from Oanda into a local SQLite DB.
- Resume incrementally on re-run.
- Inspect what's stored.

The strategy, backtester, and visualization tools are still to come.

## Quickstart

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
# 1. Install / sync the environment
uv sync

# 2. Configure secrets
cp .env.example .env
$EDITOR .env      # fill in OANDA_API_KEY, OANDA_ACCOUNT_ID

# 3. (Optional) tune config.yaml — instruments, granularity, DB path

# 4. Pull a batch of recent bars
uv run brn download EUR_USD --count 500

# 5. See what's in the DB
uv run brn status

# 6. Top up every configured instrument from where you left off
uv run brn download-all
```

## Layout

```
brn_fun/
├── config.yaml           # instruments, granularity, DB path
├── .env                  # Oanda credentials (gitignored)
├── data/brn_fun.sqlite   # local candles (gitignored)
└── src/brn_fun/
    ├── config.py         # YAML + env loader
    ├── db.py             # SQLite schema + upsert helpers
    ├── oanda.py          # candle downloader with pagination
    └── cli.py            # `brn` command
```

## Development

```bash
uv run pytest       # tests
uv run ruff check   # lint
```
