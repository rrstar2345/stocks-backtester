# Stocks Backtester

A Streamlit app for backtesting a configurable **RSI + ADX** strategy on Indian markets using historical data from the **Fyers API v3**. It works on a single instrument (index, equity, future, option) or on a **rolling option chain** where the strike is re-picked on every bar from the underlying's price.

## Features

- **Fyers data with a local cache.** Candles are stored in DuckDB. Repeat backtests only request the symbol / date / resolution ranges that are missing, so changing strategy parameters costs no API calls.
- **Symbol master.** Fyers' master contract files are downloaded once a day (Streamlit `cache_data`, persisted to disk) and used to resolve symbols, lot sizes, expiries and strikes.
- **Configurable strategy.** RSI crossing its own EMA/SMA smoothing line, filtered by ADX / DI trend. Parameters are editable in the UI.
- **Option-chain backtests.** Choose expiry (Current, Previous 1-3, Next 1-3) and strike (ATM, ITM 1-5, OTM 1-5) for CE, PE or both.
- **Realistic costs and risk.** Target and stop loss (points or %), lot-based position sizing, intraday square-off, flat brokerage per order and govt charges on sell turnover.
- **Results.** Colour-coded summary cards (gross, charges, net, win rate, profit factor, drawdown), equity curve, trade ledger with per-trade charges, CSV download.

## Setup

The project uses [uv](https://docs.astral.sh/uv/getting-started/installation/) (check with `uv --version`).

```bash
uv python install 3.13   # only if you have no Python 3.11+
uv sync                  # creates .venv and installs locked dependencies
uv run streamlit run app.py
```

The app runs on port 8548 (see `.streamlit/config.toml`). Dependencies live in `pyproject.toml` and are locked in `uv.lock`; add one with `uv add <package>`, and install dev tools (`ty`, `pytest`) with `uv sync` (the dev group is included by default).

### Fyers authentication

1. Create an API app in the Fyers API dashboard and note the App ID and Secret ID.
2. In the sidebar, enter App ID, Secret ID and Redirect URL, then **Save Credentials** (stored encrypted locally).
3. Open the login link, authorise, and paste the `auth_code` from the redirect URL.
4. Click **Generate Access Token**. The token is saved locally. Fyers tokens expire daily, so repeat steps 3-4 each day.

## Using the app

**Single instrument:** pick the exchange / segment, search a symbol (or choose underlying, expiry, strike for derivatives), set resolution and dates, and run. An override box accepts any raw Fyers symbol, for example an expired contract.

**Option chain:**

1. Set **Mode** to *Option chain*, then pick the segment (NFO / BFO) and underlying.
2. Choose **Expiry**, **Strike** and **Option side** (CE / PE / Both).
3. Pick a date range on or before the expiry (it is clipped to the expiry date) and run.

Data is fetched in this order:

1. Underlying candles for the range.
2. All strikes available for the underlying and expiry.
3. Strikes matching the rule, using the underlying's close on each bar. For example, ITM 1 with the underlying at 25,020 selects 24,950 for CE and 25,050 for PE; ATM at 24,902 selects 24,900.
4. Option candles for those strikes only, checking the local cache first.

The strategy runs on each selected option's own premium (long entries only, so it buys CE / PE). An open position stays in its contract until it exits.

### Strategy: RSI cross + ADX trend

| | Condition |
|---|---|
| Long entry | RSI crosses above its smoothing line, +DI > -DI, and ADX > threshold |
| Long exit | RSI crosses below its smoothing line |
| Short (single-instrument mode) | Mirror image |

Configurable: RSI length, smoothing type (EMA / SMA) and length, ADX length, ADX threshold. Add new strategies in `src/strategies.py` by registering a `Strategy` with a parameter list and a compute function that returns the four signal columns.

### Execution model

- Signals are read on bar close and filled at the **next bar's open**.
- Targets and stops are checked against each bar's high / low. If both are touched in one bar, the stop wins. Gaps fill at the open.
- Intraday square-off exits at the first bar closing at or after the chosen time (default 15:29).
- Brokerage is charged per order (two per round trip). Govt charges are a percentage of the sell-side value. Win rate and profit factor are net of charges.

## Caching

`market_data.duckdb` holds two tables: `candles` and `fetch_coverage` (date ranges already requested). Notes:

- Only complete trading days are marked as fetched, so today is re-requested until the session closes.
- Failed requests are retried. Empty responses (holidays, no trades) are remembered.
- Tick **Ignore local cache** to force a full re-fetch.
- The request rate is throttled to stay under Fyers' 200 calls/minute limit.

## Limitations

- **Previous expiries.** Expired contracts are not in the symbol master. Their dates are derived from the underlying's expiry cadence and tickers are built from Fyers' naming format (verified against live tickers, with a warning if it doesn't match). Whether Fyers serves expired-contract history is not guaranteed.
- Strike gaps for previous expiries are inferred from the live chain; lot size comes from today's master and can be edited.
- Option-chain mode is long-only. Slippage is not modelled, and charges are limited to the two inputs above.

## Project layout

```
app.py                 Streamlit UI
src/
  auth.py              Encrypted credentials, Fyers login, token handling
  symbols.py           Daily symbol-master download and lookups
  database.py          DuckDB candle cache, coverage tracking, Fyers history fetch
  optionchain.py       Expiry resolution, strike selection, chain data pipeline
  indicators.py        RSI, ADX / DI, EMA, SMA (Wilder conventions)
  strategies.py        Strategy registry and the RSI + ADX strategy
  backtester.py        Simulation engine, charges, performance summary
.streamlit/config.toml Server and theme settings
pyproject.toml         Dependencies and tool config (uv)
uv.lock                Locked dependency versions
```

Local files created at runtime (all git-ignored): `market_data.duckdb`, the encrypted credentials and key files, and the access token file.

## Disclaimer

For research and education only. Backtest results do not guarantee future performance and are not investment advice.
