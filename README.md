# Lazy Popper (tt-paper)

Lazy Popper is a Typer-based command line tool that quickly finds and paper-enters SPX credit spreads using the [tastytrade Python SDK](https://github.com/tastyware/tastytrade). It automates the repetitive work of picking expirations, streaming greeks over DXLink, sizing the width, and logging detailed dry-run results (credit, P/L bounds, IVR, buying power, and fees) to a CSV you can analyze later.

## Highlights
- Streams real-time greeks over DXLink to pick a short leg near your target delta.
- Automatically walks the option chain to find matching long legs and rounds credit quotes to the proper SPX tick.
- Logs every simulated trade with UUIDs, timestamps, quotes, IV rank, buying power impact, and tastytrade fee details.
- Ships with standalone smoke tests so you can validate OAuth credentials, DXLink connectivity, and CSV logging without touching live capital.
- Distributes a `TT` console entry point plus a module entry so you can run it directly via `python -m ttpaper.cli`.

## Requirements
- Python 3.10+
- A tastytrade account with API credentials (refresh token + client secret) and DXFeed/DXLink access.
- `pip install -e .` inside a virtual environment (recommended).

## Quick start
1. Clone and enter the project:
   ```bash
   git clone git@github.com:theglove44/lazy-popper.git
   cd lazy-popper
   python -m venv .venv && source .venv/bin/activate
   pip install -e .
   ```
2. Copy `.env.example.copy` to `.env` (or export vars another way) and fill in your tastytrade credentials:
   ```dotenv
   TT_CLIENT_SECRET=...
   TT_REFRESH_TOKEN=...
   TT_ACCOUNT_NUMBER=...
   # Optional: override the CSV destination
   TT_PAPER_CSV=./paper_trades.csv
   ```
3. Run the CLI. The installed console script is `TT`, but you can also execute the module directly:
   ```bash
   TT buy --symbol SPX --delta 0.28 --width 5 --qty 1
   # or
   python -m ttpaper.cli sell --symbol SPX --delta 0.30 --width 10 --qty 2
   ```

## CLI overview
There are only two commands:
- `TT buy` builds a bull put credit spread (short put near your desired absolute delta, long put further OTM).
- `TT sell` builds a bear call credit spread (short call near your desired absolute delta, long call further OTM).

Both commands share the same options (Typer exposes them as `--option value`):

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--symbol` | `SPX` | Index symbol to trade (supports other tastytrade symbols if they behave like SPX).
| `--qty` | `1` | Number of spreads (legs are scaled accordingly).
| `--delta` | `0.30` | Target absolute delta for the short leg. Sign is derived from the command (negative for puts, positive for calls).
| `--width` | `5.0` | Distance in index points between short and long legs. The long leg is picked at this width or the closest available strike.
| `--csv-path` | `TT_PAPER_CSV` env var or `./paper_trades.csv` | Destination CSV for logged paper trades.
| `--max-candidates` | `180` | Maximum strikes evaluated when picking the short leg.
| `--min-abs-delta` | `0.10` | Stop streaming greeks once the far OTM tail reaches this absolute delta for `stop-n` consecutive strikes.
| `--batch-size` | `25` | Number of strikes subscribed per DXLink batch.
| `--stop-n` | `3` | Required consecutive tail strikes with abs(delta) <= `min-abs-delta` before scanning stops.
| `--max-scan` | `250` | Hard cap on total strikes scanned for safety.

Every run fetches a one-shot index quote, streams greeks to find the best short option, finds the corresponding long leg, quotes both legs for mid/bid/ask, rounds the credit to the correct SPX tick, and places a tastytrade `dry_run` order so you can inspect buying power and fee impact without touching real capital.

## CSV output
Every simulated trade is appended to `paper_trades.csv` (or the path you set via `--csv-path`/`TT_PAPER_CSV`). Columns match the CLI internals and downstream notebooks/tools can rely on them being present:

```
paper_id,created_at_utc,cmd,symbol,exp,qty,target_abs_delta,short_delta,width_points,spot_mid,
short_strike,long_strike,short_bid,short_ask,short_mid,long_bid,long_ask,long_mid,
credit_mid_points,limit_credit_points,max_profit_usd,max_loss_usd,ivr,
bp_change,bp_isolated_margin_req,fees_total,fees_breakdown,dry_run_warnings,dry_run_errors,
greeks_subscribed,greeks_batches,greeks_received
```

A miniature fixture lives in `_paper_trades_test.csv` so you can see the schema end-to-end.

## Smoke tests & utilities
These helper scripts are meant to be run directly with Python (after activating your venv and exporting the same env vars the CLI uses):
- `python oauth_smoke_test.py` – validates that your OAuth credentials allow logging in, fetching account balances, and refreshing sessions.
- `python chain_greeks_test.py --symbol SPX --right put` – exercises option chain retrieval plus DXLink streaming so you can confirm greeks arrive in the expected batches.
- `python paper_csv_smoke_test.py` – rewrites `_paper_trades_test.csv` and ensures the CSV logging helper always emits required columns.

All of them exit non-zero on failure and print useful diagnostics (account number, column counts, etc.) so they double as troubleshooting tools when something changes in the tastytrade API.

## Running locally without installing
If you prefer not to install the console entry point you can run the CLI module in-place:
```bash
python -m ttpaper.cli buy --symbol SPX --delta 0.25 --width 7 --qty 1 --csv-path ./my_trades.csv
```

## Development notes
- Typer + Rich provide colorized output, so run the CLI in a terminal that supports ANSI colors.
- The tastytrade SDK auto-detects whether you are in production or sandbox; you can override with `TT_IS_TEST=1` if you want to hit the test environment.
- The project ships with a `pyproject.toml` entry point (`TT = "ttpaper.cli:app"`), so after editing locally run `pip install -e .` again to refresh.
- Keep `.env` and any generated CSVs out of version control; `.gitignore` already covers them.

Happy paper trading!
