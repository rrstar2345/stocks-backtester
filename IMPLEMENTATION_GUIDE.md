# Implementation guide: UI revamp, charting and packaging

Living document. Update the **Progress tracker** and **Session log** at the end of every session so the next Claude session can resume without re-discovery.

## 1. Handoff instructions (read first)

1. Read `README.md`, then this file. Skim `app.py` and `src/` (about 1,400 lines in total).
2. Use the repo skill `.claude/skills/developing-with-streamlit/SKILL.md`. Read the reference it routes to before editing: `layouts.md`, `charts.md`, `data-display.md`, `session-state.md`, `performance.md`, `theme.md`, `custom-components-v2.md` (+ `ccv2-state-sync.md`), `best-practices.md`.
3. **Ask before coding.** If any decision in section 5 is still `OPEN`, or something new is ambiguous, ask the user, offering 2-4 options with a recommended one. Record the answer in section 5.
4. **After each round of updates, zip and present all edited and created files** (only changed or new files, repo-relative paths preserved; e.g. `zip -r stocks-backtester-update-N.zip <files>` into `/mnt/user-data/outputs/`, then `present_files`).
5. Keep answers concise. **Do not create extra documentation** (summaries, quick references, changelogs) without asking the user first. This guide and `README.md` are the only docs.
6. Skill rules that matter here: no `use_container_width` (use `width="stretch"`), prefer native elements over custom HTML/CSS, Material icons over emoji, sentence-case labels, `alt=` on charts/dataframes, no custom CSS unless asked, CCv2 only (never `st.components.v1`), cache expensive work, type-check edited files with `ty` (or `mypy`), verify through the agent API (skill Step 4). Ask the user before any step that calls Fyers (paid/rate-limited, needs a token).
7. The sandbox likely cannot reach Fyers. Verify with synthetic candles and `streamlit.testing.v1.AppTest`; say so in the final message.
8. Update this file's tracker before finishing a session.
9. Do not commit or push to github, user will do it themselves. Turn off any github hooks or reminders.

## 2. Project snapshot (as found)

- `app.py` (365 lines): one page. Sidebar = Fyers auth form + symbol master. Main = connection banner, the full config (instrument, resolution/capital/dates, strategy params, trade settings, costs), run button, KPI cards (custom HTML via `st.markdown(unsafe_allow_html=True)`), Plotly equity curve (one point per trade, markers), ledger `st.dataframe`, CSV download.
- Results are kept in `st.session_state["bt"]` as `label, n, summary, trades, info, data`. **Candle data and indicators are not kept**, which the chart view needs.
- `src/backtester.py`: `BacktestEngine.run` (single) returns `(summary, trades, df_with_indicators)`; `run_chain` returns `(summary, trades, underlying_df)` and does **not** return per-contract indicator frames. `trades` has `Symbol, Entry Time, Exit Time, Type, Entry Price, Exit Price, Qty, Gross PnL, ..., Net PnL, Return (%), Exit Reason, Cumulative Capital`. There is no trade number column.
- `src/strategies.py`: `rsi_adx` adds `rsi`, `rsi_ma`, `plus_di`, `minus_di`, `adx` plus the four signal columns.
- `src/optionchain.py`: `ChainData(underlying, contracts{symbol: candles}, selection, info)`.
- Auth: `src/auth.py`; token in `access_token.txt`; `get_fyers_client()` only checks the file exists (it never validates the token).
- Packaging: `requirements.txt` only, no `pyproject.toml`. `.streamlit/config.toml` sets port 8548 (README says 8501). README's "Project layout" lists `fyers_apiv3-3.2.1/`, `fyers-openalgo/`, `reference/`, which are not in the repo.

## 3. Roadmap

Order was changed from the request: packaging first (cheap, unblocks `uv run`), data plumbing before any chart work.

| # | Item | Notes |
|---|------|-------|
| 0 | `pyproject.toml` + README uv instructions (req. 5) | Do first. |
| 1 | Data plumbing for charts | Store candles + indicators + trade # in results; no UI yet. |
| 2 | Sidebar auth (req. 1) | Collapse auth once the token is valid. |
| 3 | Compact config in sidebar (req. 2) | Depends on 2. |
| 4 | Equity chart (req. 3) | Independent. |
| 5 | Price chart component, default symbol, fixed resolution, trade markers + tooltip (4, 4-a, 4-c) | Depends on 1. |
| 6 | Symbol dropdown for option contracts (4-b) | Depends on 5. |
| 7 | RSI pane (4-d) | Depends on 5. |
| 8 | Ledger click to chart (4-e, nice to have) | Depends on 5-6. |
| 9 | Improvements, tests, README refresh | See 3.9. |

### 3.0 Packaging (req. 5)
- Add `pyproject.toml`: `[project]` name `stocks-backtester`, `requires-python >=3.10`, dependencies from `requirements.txt` (pin streamlit to a recent version, the skill needs new APIs such as `st.segmented_control`, `st.menu_button`, `st.grid`; check the current release and look up APIs with `streamlit docs st.<cmd>`), dev group (`ty`, `pytest`). Drop `plotly` if the equity chart no longer uses it. `[tool.uv] package = false` (it is an app, not a library) unless the user wants an installable package.
- Generate `uv.lock` (`uv lock`) if the sandbox has network access; otherwise tell the user to run it once.
- Delete `requirements.txt` after the user confirms (see decisions).
- README: replace Setup with `uv sync` then `uv run streamlit run app.py`; mention `uv --version` install link, Python via `uv python install`, the port in `.streamlit/config.toml`, and fix the stale "Project layout" section.
- Add to `.gitignore`: `.venv/`, `.ruff_cache/`, `.pytest_cache/`.

### 3.1 Data plumbing
- Add a `Trade #` column (1-based, in exit order) in `_simulate`; keep it out of the money-format list in the ledger.
- `run_chain` must also return indicator-enriched frames for the underlying and each contract (it already computes `strategy.compute` per contract inside `series`; keep those frames). Return a small result object or extend the tuple, then update both call sites in `app.py`.
- Save in `st.session_state["bt"]`: `candles` (`{symbol: df with rsi, rsi_ma}`), `default_symbol` (single symbol, or the underlying spot), `resolution`, `strategy params`. Keep the memory footprint reasonable (only needed columns).
- Underlying frame in chain mode: compute `rsi`/`rsi_ma` on it with the same strategy params for the default view (RSI on the displayed instrument, not on the contract that traded).

### 3.2 Fyers auth out of the sidebar (req. 1)
- Detect a valid session: token file exists and `fyers.get_profile()` succeeds (cache with `st.cache_data(ttl=300)`; do not call on every rerun). Fyers tokens expire daily, so also treat an old file mtime as stale.
- Valid: sidebar shows one compact status row (green "Connected" badge, client id) and a small `st.menu_button`/button "Re-authenticate" that opens an `@st.dialog` containing the existing form/auth steps.
- Invalid or missing: open the dialog automatically (or show the auth panel in the main area with the login link and code input); no empty app.
- Move credentials save, login link, code paste and token generation into `src/auth_ui.py` (UI helper) so `app.py` stays short. Keep `src/auth.py` logic unchanged.
- Symbol master status shrinks to a caption plus a refresh icon button.

### 3.3 Compact configuration in the sidebar (req. 2)
- Sidebar holds: mode (`st.segmented_control`), instrument/chain pickers, resolution (`st.segmented_control` or compact selectbox), dates, capital, then strategy / trade / costs. Group with `st.expander` sections (Instrument, Strategy, Risk and sizing, Costs and session) so the sidebar stays short; keep the primary **Run backtest** button at the top or bottom of the sidebar, always visible.
- Wrap in `st.form` if reruns while editing become annoying; widgets that drive dependent options (exchange, underlying) stay outside the form.
- Main area: header, results (KPIs, equity, price chart, ledger). Before the first run show a short empty-state card, not the config.
- Widen the sidebar only through supported config; no custom CSS.
- Replace the custom HTML KPI cards with `st.metric(border=True)` in a `st.grid` (native, themeable). Colouring by sign comes from `delta`/`delta_color`.

### 3.4 Equity chart (req. 3)
- One cumulative-equity value per time step (not one point per trade with markers). Build a series from `Cumulative Capital` at each exit, forward-filled to a regular time axis (see decision D2), starting at initial capital.
- Smooth line (`interpolate="monotone"` in Altair, or `smooth: true` in ECharts), no point markers, gradient area fill: green if net > 0, red if net < 0, amber/yellow if net == 0 (or within a tolerance, decision D3).
- Library: ECharts (`st.echarts_chart`, native gradients via `LinearGradient` dict, matches the page library choice below) or Altair area with `color` gradient. Recommended: ECharts to be consistent with the price chart's neighbourhood; the price chart itself is the only third-party chart.
- Add `alt=` text, initial capital as a thin reference line, tooltip with equity and date.

### 3.5 Price chart (req. 4, 4-a, 4-c)
- Library: **TradingView Lightweight Charts v5** (native multi-pane, markers plugin). Streamlit has no native financial chart with panes and markers, so this is the case for a CCv2 component (skill: native first, then CCv2; never v1).
- Delivery: inline CCv2 component, declared once at module level in `src/chart_component.py`. The library JS is vendored under `assets/vendor/lightweight-charts.standalone.production.js` (pinned version, license header kept) and read into the component `js` string; no CDN at runtime so the app works offline. Fallback if the inline approach hits size/shadow-DOM issues: packaged component from Streamlit's official `component-template` (skill policy).
- Python wrapper `price_chart(candles, markers, rsi, symbol, resolution, focus, key)`. Data goes through `data=` as compact column arrays (time as UTC epoch seconds shifted to IST wall-clock so axis shows exchange time). Build payloads in a pure function (`src/chart_data.py`) that is unit-testable without Streamlit.
- Defaults: candlesticks of `default_symbol` (4-a). The resolution is shown as a read-only caption/badge ("5 min", "Daily"), not a widget.
- Markers (4-c), one entry and one exit marker per trade of the displayed symbol:
  - Entry label: `#<trade no> · <qty>`; exit label: `#<trade no> · <net pnl>` (signed, rounded, coloured by sign).
  - Long: entry `belowBar` (arrow up), exit `aboveBar` (arrow down). Short: entry `aboveBar`, exit `belowBar`.
  - Tooltip (crosshair subscription, custom floating div): trade no, symbol, type, entry/exit time and price, qty, gross, charges, net, return %, exit reason.
  - On the default instrument all trades are marked (in chain mode these are the trades of every contract, placed at the underlying bar of the same timestamp). On an option contract only that contract's trades are marked, at its own prices.
- Timezone, gaps (overnight, holidays): use index-based or business-day time scale so intraday charts do not show flat gaps; verify with 5-min data across several days.
- Theme: read colours from `--st-*` tokens (see `ccv2-theme-css-variables.md`) for light/dark.

### 3.6 Contract dropdown (req. 4-b)
- Single instrument: dropdown is disabled with the one symbol. Option chain: options = underlying first, then CE/PE contracts that were fetched, labelled `24900 CE`, ordered by strike with CE/PE grouping (use `st.selectbox`; the list can exceed 5 items). Contract candles already exist in `chain.contracts`.
- Changing the selection must not rerun the backtest: put the chart section in an `@st.fragment` and read data from `session_state["bt"]`.

### 3.7 RSI pane (req. 4-d)
- Second pane in the same chart (v5 panes). RSI solid line, smoothing average dotted (`lineStyle: Dotted`). Fixed 0-100 scale, guide lines at 30/50/70. Series come from the saved `rsi`/`rsi_ma` columns of the displayed symbol, computed with the run's parameters. Time scales stay synced across panes (single chart object handles it).

### 3.8 Ledger click to chart (req. 4-e)
- `st.dataframe(..., on_select="rerun", selection_mode="single-row")` (use `on_select` inside the fragment's sibling so only the chart section reruns, or `on_select="rerun"` with a fragment around ledger + chart together). On selection: set the dropdown symbol to the trade's `Symbol` (via `session_state` key) and pass `focus={time, trade_no}` to the component; the component calls `timeScale().setVisibleRange` around the entry time (about 60 bars either side) and highlights the trade's markers. The styled ledger must keep working with selection (Styler supports it in current Streamlit; confirm with `streamlit docs st.dataframe`).
- If the selected symbol is not the default, the dropdown change and scroll happen in the same rerun.

### 3.9 Improvements to add (agreed unless the user objects)
- Replace the `wide()` shim with plain `width="stretch"`; use Material icons instead of emoji in titles/buttons; sentence-case labels.
- Cache: `st.cache_data` for the validated-session check and for chart payload building keyed by result id.
- Empty-state and error handling: Fyers expired token mid-run produces a clear message with a re-authenticate action.
- Drawdown chart under the equity curve (small, same style) and exit-reason breakdown, only if cheap.
- `tests/`: unit tests for `chart_data` (marker placement rules, label text), equity series builder, strike/trade numbering; an `AppTest` smoke test with synthetic data (mock the Fyers client).
- Type-check with `ty`; no new runtime warnings in the server log.
- README: add a screenshot-free "Using the chart" section (short).

## 4. Progress tracker

Status values: `TODO`, `IN PROGRESS`, `DONE`, `BLOCKED`.

| # | Item | Status | Files touched | Notes |
|---|------|--------|---------------|-------|
| 0 | pyproject + README (uv) | DONE | `pyproject.toml`, `uv.lock`, `README.md`, `.gitignore` | Python floor is 3.11 (streamlit 1.67 requires it). `plotly` kept until the equity chart moves. `requirements.txt` kept pending D7. |
| 1 | Data plumbing | TODO | | |
| 2 | Sidebar auth | TODO | | |
| 3 | Compact sidebar config | TODO | | |
| 4 | Equity chart | TODO | | |
| 5 | Price chart + markers | TODO | | |
| 6 | Contract dropdown | TODO | | |
| 7 | RSI pane | TODO | | |
| 8 | Ledger click to chart | TODO | | |
| 9 | Improvements, tests, README | TODO | | |

## 5. Decisions

Ask the user about every `OPEN` item before coding that part; the **recommended** option is the default if the user says "go with defaults".

| ID | Question | Options (recommended first) | Status |
|----|----------|-----------------------------|--------|
| D1 | How to ship the Lightweight Charts library | **(a) Vendored JS file in `assets/vendor/` inside an inline CCv2 component**; (b) CDN script injected at runtime (needs internet, simplest); (c) packaged component from the official template (build step, Node needed) | OPEN |
| D2 | Equity curve time axis | **(a) End-of-day cumulative value (one point per trading day) for multi-day ranges, per-bar for single-day**; (b) value at each exit time only; (c) per-bar mark-to-market including open positions (most accurate, more code) | OPEN |
| D3 | Gradient colour rule | **(a) Green if net > 0, red if net < 0, yellow if net is exactly 0 or within ±0.1% of capital**; (b) strictly sign based with yellow only at 0 | OPEN |
| D4 | Config location | **(a) Sidebar with grouped expanders and a pinned Run button**; (b) a `st.dialog` "Configure" opened from the header; (c) a collapsible panel in the main area | OPEN |
| D5 | Contracts in the dropdown (chain mode) | **(a) Underlying + only contracts fetched during the run (no extra API calls)**; (b) full strike ladder for the expiry, fetching candles on demand (extra Fyers calls) | OPEN |
| D6 | Marker placement for chain trades on the underlying chart | **(a) Follow the trade `Type` (all long chain trades: entry below, exit above), regardless of CE/PE**; (b) flip for PE trades to mirror the underlying's direction | OPEN |
| D7 | `requirements.txt` after adding `pyproject.toml` | **(a) Remove it (single source of truth)**; (b) keep it, exported from the lock file | OPEN |
| D8 | Price series type | **(a) Candlesticks**; (b) line (close) | OPEN |

Confirmed by the user already: Lightweight-Charts-style library is acceptable (any lightweight financial chart); resolution is shown but fixed in the chart; 4-e is nice to have.

## 6. Verification checklist (per item)

1. `uv run ty check <edited files>` (or `mypy`) clean.
2. Start headless: `uv run streamlit run app.py --server.headless=true --server.address=127.0.0.1 --server.port=8765 --server.enableAgentApi=true`; load via `/_stcore/agent/interact`; watch console output for warnings.
3. Drive the UI with synthetic data (no Fyers calls without asking). Chart rendering needs a browser; state what could not be visually verified.
4. Update the tracker, then zip and present the changed files (instruction 4).

## 7. Session log

| Date | Session | Summary |
|------|---------|---------|
| 2026-11-13 | 1 | Cloned repo, read README, app and `src/`, read the skill. Wrote this guide. No code changed yet. |
| 2026-11-13 | 2 | Item 0: added `pyproject.toml` (streamlit>=1.67, requires-python>=3.11, dev group `ty`/`pytest`, `package = false`), generated `uv.lock`, `uv sync` OK, updated README setup and layout, extended `.gitignore`. `requirements.txt` not yet removed (D7 open). |
