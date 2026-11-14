import uuid
from datetime import date, time as dtime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from src import auth_ui, optionchain, symbols
from src.auth import get_fyers_client
from src.backtester import BacktestEngine, ExecConfig, RESOLUTIONS, bar_minutes, warmup_days
from src.database import fetch_history_fyers, fetch_candles_from_db
from src.strategies import STRATEGIES

st.set_page_config(page_title="Fyers backtesting dashboard", page_icon=":material/candlestick_chart:", layout="wide")
st.title("Fyers API v3 DuckDB backtesting engine")

GREEN, RED, AMBER, SLATE = "#16a34a", "#dc2626", "#f59e0b", "#64748b"
MODE_SINGLE, MODE_CHAIN = "Single instrument", "Option chain"
MODE_CHAIN_HELP = ("Option chain: the underlying's close picks the strike on every bar; the strategy runs on that "
                   "option's premium (long entries only, i.e. buying CE / PE). The range is clipped to the expiry date.")


def sign_color(v):
    return GREEN if v > 0 else RED if v < 0 else SLATE


def inr(v):
    return f"{'-' if v < 0 else ''}₹{abs(v):,.2f}"


def delta_for(v, text):
    """Metric delta that is gray at exactly zero (numeric 0) and green/red by sign otherwise."""
    return 0 if v == 0 else text


def pnl_style(v):
    return f"color: {sign_color(v)}; font-weight: 600"


def pick_instrument():
    """Returns (fyers_symbol, lot_size)."""
    if master is None:
        custom = st.text_input("Fyers symbol (symbol master unavailable)", value="NSE:NIFTY50-INDEX")
        return custom.strip(), 1

    exchange = st.selectbox("Exchange / segment", symbols.EXCHANGES, index=2)
    if exchange in symbols.DERIVATIVE_EXCHANGES:
        kind = st.segmented_control("Instrument", ["OPT", "FUT"], default="OPT", required=True, key="kind",
                                    format_func=lambda k: "Option" if k == "OPT" else "Future") or "OPT"
        pool = symbols.contracts(master, exchange, kind)
        names = sorted(pool["underlying"].dropna().unique())
        if not names:
            st.warning("No contracts found for this selection.")
            return "", 1
        c1, c2 = st.columns(2)
        underlying = c1.selectbox("Underlying", names, index=names.index("NIFTY") if "NIFTY" in names else 0)
        pool = pool[pool["underlying"] == underlying]
        expiries = sorted(pool["expiry"].dropna().unique())
        expiry = c2.selectbox("Expiry", expiries, format_func=lambda d: pd.Timestamp(d).strftime("%d-%b-%Y"))
        pool = pool[pool["expiry"] == expiry]
        if kind == "OPT":
            opt_type = st.segmented_control("Option type", ["CE", "PE"], default="CE", required=True,
                                            key="opt_type") or "CE"
            strikes = sorted(pool.loc[pool["instrumenttype"] == opt_type, "strike"].unique())
            strike = st.selectbox("Strike", strikes, index=len(strikes) // 2, format_func=lambda s: f"{s:g}")
            pool = pool[(pool["instrumenttype"] == opt_type) & (pool["strike"] == strike)]
        row = pool.iloc[0]
    else:
        query = st.text_input("Search symbol / company", value="NIFTY50" if exchange == "NSE_INDEX" else "")
        found = symbols.search_symbols(master, exchange, query)
        if found.empty:
            st.warning("No matching symbols.")
            return "", 1
        label = dict(zip(found["brsymbol"], found["symbol"] + "  —  " + found["name"].fillna("")))
        chosen = st.selectbox("Symbol", list(label), format_func=label.get)
        row = found[found["brsymbol"] == chosen].iloc[0]
    st.caption(f"Resolved Fyers symbol: **{row['brsymbol']}** · lot size {int(row['lotsize'])}")
    return row["brsymbol"], int(row["lotsize"])


def pick_chain():
    """Option-chain inputs. Returns a dict or None."""
    if master is None:
        st.error("The symbol master is required for option-chain mode (download failed).")
        return None
    exchange = st.segmented_control("Derivatives segment", ["NFO", "BFO"], default="NFO", required=True,
                                    key="chain_exchange") or "NFO"
    names = sorted(symbols.contracts(master, exchange, "OPT")["underlying"].dropna().unique())
    if not names:
        st.warning("No option contracts found.")
        return None
    default = "NIFTY" if exchange == "NFO" else "SENSEX"
    underlying = st.selectbox("Underlying (index / equity)", names, index=names.index(default) if default in names else 0)

    c1, c2 = st.columns(2)
    expiry_choice = c1.selectbox("Expiry", optionchain.EXPIRY_CHOICES)
    strike_choice = c2.selectbox("Strike", optionchain.STRIKE_CHOICES)
    side = st.segmented_control("Option side", ["Both", "CE", "PE"], default="Both", required=True,
                                key="chain_side") or "Both"

    spot = optionchain.spot_symbol(master, exchange, underlying)
    override = st.text_input("Override underlying (spot) Fyers symbol (optional)")
    spot = override.strip() or spot
    if not spot:
        st.error("Could not resolve the spot symbol for this underlying; enter it above.")
        return None

    today = pd.Timestamp(symbols.today_ist())
    live = optionchain.live_expiries(master, exchange, underlying, today)
    try:
        exp = optionchain.resolve_expiry(live, optionchain.parse_expiry_choice(expiry_choice))
    except ValueError as e:
        st.error(str(e))
        return None
    lot = int(optionchain.option_rows(master, exchange, underlying)["lotsize"].iloc[0])
    st.caption(f"Spot: **{spot}** · Expiry: **{exp.date:%a %d-%b-%Y}** "
               f"({'listed' if exp.listed else 'expired, derived from the expiry cadence'}) · lot size {lot}")
    if not exp.listed:
        ratio = optionchain.check_ticker_format(master, exchange, underlying, live)
        if ratio < 0.99:
            st.warning(f"Expired-contract ticker rule only reproduces {ratio:.0%} of listed tickers for "
                       f"{underlying}; data for previous expiries may not be found.")
    return dict(exchange=exchange, underlying=underlying, spot=spot, expiry_choice=expiry_choice,
                strike_choice=strike_choice, sides=["CE", "PE"] if side == "Both" else [side],
                lot=lot, expiry=exp.date)


def rule(kind, value):
    return None if kind == "Off" or not value else (kind.lower(), float(value))


# --- Sidebar: connection status, run button, configuration ---
status, who = auth_ui.session_status()
fyers = get_fyers_client() if status in auth_ui.CONNECTED else None
connected = fyers is not None

with st.sidebar:
    with st.container(border=True):
        auth_ui.render_status(status, who)
        # Symbol master: downloaded once a day, cached.
        master_error = None
        try:
            master = symbols.load_master(symbols.today_ist())
            master_note = f"{len(master):,} symbols · {symbols.today_ist()}"
        except Exception as e:
            master, master_note, master_error = None, ":red[Symbol download failed]", str(e)
        with st.container(horizontal=True, vertical_alignment="center"):
            st.caption(master_note, help=master_error)
            st.button("Refresh", key="refresh_symbols", icon=":material/refresh:", type="tertiary",
                      help="Download the symbol master again", on_click=symbols.load_master.clear)
    run_slot = st.container()  # filled with the Run button once the configuration below has been read

auth_ui.open_dialog_if_requested(status)

if connected:
    with st.sidebar:
        with st.expander("Instrument and data", expanded=True, icon=":material/candlestick_chart:"):
            mode = st.segmented_control("Mode", [MODE_SINGLE, MODE_CHAIN], default=MODE_SINGLE, required=True,
                                        key="mode", help=MODE_CHAIN_HELP) or MODE_SINGLE
            chain_cfg = None
            if mode == MODE_CHAIN:
                chain_cfg = pick_chain()
                target_symbol, lot_default = "", (chain_cfg["lot"] if chain_cfg else 1)
            else:
                target_symbol, lot_default = pick_instrument()
                override = st.text_input("Override with a Fyers symbol (optional)",
                                         help="For example an expired contract.")
                if override.strip():
                    target_symbol = override.strip()

            resolution = st.selectbox("Resolution", RESOLUTIONS, index=RESOLUTIONS.index("5"), key="resolution")
            initial_capital = st.number_input("Initial capital (₹)", value=100000.0, step=10000.0, key="initial_capital")
            d1, d2 = st.columns(2)
            start_date = d1.date_input("Start date", value=date.today() - timedelta(days=30), key="start_date")
            end_date = d2.date_input("End date", value=date.today(), key="end_date")
            refresh = st.checkbox("Ignore local cache", value=False, key="refresh",
                                  help="Re-fetch everything from Fyers. By default only symbol / date / resolution "
                                       "ranges missing from the local DuckDB cache are requested.")

        with st.expander("Strategy", icon=":material/insights:"):
            strategy = STRATEGIES[st.selectbox("Strategy", list(STRATEGIES), key="strategy")]
            params = {}
            param_cols = st.columns(2)
            for i, p in enumerate(strategy.params):
                col, key = param_cols[i % 2], f"{strategy.name}.{p.key}"
                if p.kind == "int":
                    params[p.key] = col.number_input(p.label, int(p.min), int(p.max), int(p.default), 1, key=key)
                elif p.kind == "float":
                    params[p.key] = col.number_input(p.label, float(p.min), float(p.max), float(p.default), 1.0, key=key)
                else:
                    params[p.key] = col.selectbox(p.label, p.options, index=p.options.index(p.default), key=key)

        with st.expander("Risk and sizing", icon=":material/shield:"):
            if mode == MODE_CHAIN:
                direction = "long"
                st.caption("Direction: long only (buying CE / PE).")
            else:
                direction = {"Long": "long", "Short": "short", "Both": "both"}[
                    st.segmented_control("Direction", ["Long", "Short", "Both"], default="Long", required=True,
                                         key="direction") or "Long"]
            tgt_kind = st.segmented_control("Target", ["Off", "Points", "Percent"], default="Points", required=True,
                                            key="tgt_kind") or "Points"
            sl_kind = st.segmented_control("Stop loss", ["Off", "Points", "Percent"], default="Points", required=True,
                                           key="sl_kind") or "Points"
            r1, r2 = st.columns(2)
            tgt_val = r1.number_input("Target value", min_value=0.0, value=30.0, step=1.0, disabled=tgt_kind == "Off",
                                      key="tgt_val")
            sl_val = r2.number_input("Stop loss value", min_value=0.0, value=25.0, step=1.0, disabled=sl_kind == "Off",
                                     key="sl_val")
            sizing = {"From capital": "capital", "Fixed qty": "fixed"}[
                st.segmented_control("Position sizing", ["From capital", "Fixed qty"], default="From capital",
                                     required=True, key="sizing") or "From capital"]
            # No key: the default follows the selected instrument's lot size.
            lot_size = st.number_input("Lot size", min_value=1, value=lot_default, step=1) if sizing == "capital" else 1
            fixed_qty = st.number_input("Quantity", min_value=1, value=max(lot_default, 1), step=1) if sizing == "fixed" else 1

        with st.expander("Costs and session", icon=":material/payments:"):
            intraday_res = bar_minutes(resolution) is not None
            square_off = st.checkbox("Intraday: square off daily", value=True, disabled=not intraday_res,
                                     key="square_off")
            w1, w2 = st.columns(2)
            win_start = w1.time_input("Entries from", value=dtime(9, 15), disabled=not (intraday_res and square_off),
                                      key="win_start")
            sq_at = w2.time_input("Square off at", value=dtime(15, 29), disabled=not (intraday_res and square_off),
                                  key="sq_at")
            b1, b2 = st.columns(2)
            brokerage = b1.number_input("Brokerage per order (₹)", min_value=0.0, value=20.0, step=1.0, key="brokerage")
            govt_pct = b2.number_input("Govt charges on sell (%)", min_value=0.0, value=0.05, step=0.01, format="%.3f",
                                       key="govt_pct")

        run_disabled = chain_cfg is None if mode == MODE_CHAIN else not target_symbol
        run_clicked = run_slot.button("Run backtest", key="run_backtest", type="primary", width="stretch",
                                      icon=":material/play_arrow:", disabled=run_disabled,
                                      help="Fetch any missing candles from Fyers, then run the simulation.")

    if run_clicked:
        cfg = ExecConfig(
            direction=direction,
            target=rule(tgt_kind, tgt_val), stop=rule(sl_kind, sl_val),
            sizing=sizing,
            lot_size=int(lot_size), fixed_qty=int(fixed_qty),
            bar_minutes=bar_minutes(resolution), square_off=square_off,
            window_start=win_start, square_off_at=sq_at, brokerage=brokerage, govt_pct=govt_pct,
        )
        engine = BacktestEngine(initial_capital)
        st.session_state.pop("bt", None)

        if mode == MODE_CHAIN:
            if chain_cfg:
                prog = st.progress(0.0, text="Starting...")
                try:
                    chain = optionchain.build_chain(
                        fyers, master, chain_cfg["exchange"], chain_cfg["underlying"], chain_cfg["spot"],
                        chain_cfg["expiry_choice"], chain_cfg["strike_choice"], chain_cfg["sides"],
                        resolution, str(start_date), str(end_date),
                        warmup_days=warmup_days(strategy, params, bar_minutes(resolution)),
                        progress=lambda f, t: prog.progress(min(max(f, 0.0), 1.0), text=t),
                        use_cache=not refresh)
                    res = engine.run_chain(chain, strategy, params, cfg)
                    st.session_state["bt"] = dict(
                        id=uuid.uuid4().hex, resolution=resolution, strategy=strategy.name, params=params,
                        label=f"{chain_cfg['underlying']} · {chain_cfg['expiry_choice']} expiry · {chain_cfg['strike_choice']}",
                        n=len(chain.underlying), summary=res.summary, trades=res.trades, info=chain.info,
                        data=chain.info["Data source"], candles=res.candles, default_symbol=res.default_symbol)
                except Exception as e:
                    st.error(str(e))
                prog.empty()
        else:
            df_hist, stats = pd.DataFrame(), {}
            with st.spinner("Fetching data and executing simulation..."):
                try:
                    df_hist = fetch_history_fyers(fyers, target_symbol, resolution=resolution,
                                                  start_date=str(start_date), end_date=str(end_date),
                                                  use_cache=not refresh, stats=stats)
                except Exception as e:
                    st.warning(f"{e} — trying locally cached data.")
                if df_hist.empty:
                    df_hist = fetch_candles_from_db(target_symbol, resolution, str(start_date), str(end_date))
                if df_hist.empty:
                    st.error("No historical data found for the selected range/symbol.")
                else:
                    res = engine.run(df_hist, strategy, params, cfg, symbol=target_symbol)
                    st.session_state["bt"] = dict(
                        id=uuid.uuid4().hex, resolution=resolution, strategy=strategy.name, params=params,
                        label=target_symbol, n=len(df_hist), summary=res.summary, trades=res.trades,
                        info=None, data=stats, candles=res.candles, default_symbol=res.default_symbol)

# --- Main area: empty states, then results ---
bt = st.session_state.get("bt")
if not bt and not connected:
    st.warning("Connect to Fyers to configure and run a backtest.", title="Not connected", icon=":material/lock:")
    st.button("Connect to Fyers", key="auth_open_main", type="primary", icon=":material/key:",
              on_click=auth_ui.request_dialog)
elif not bt:
    st.info("Choose the instrument, strategy and trade settings in the sidebar, then select **Run backtest**.",
            title="Ready to backtest", icon=":material/tune:")

if bt:
    summary, trades_df = bt["summary"], bt["trades"]
    d = bt.get("data") or {}
    st.success(f"{bt['label']}: {bt['n']} underlying candles. Data: {d.get('api_calls', 0)} Fyers API call(s) · "
               f"{d.get('from_cache', 0)} symbol(s) served fully from the local cache · "
               f"{d.get('fetched', 0)} fetched / topped up.")

    st.subheader("Performance summary")
    capital = summary["Initial Capital"]
    net, gross, charges = summary["Net Profit / Loss"], summary["Gross Profit / Loss"], summary["Total Charges"]
    pf, wr = summary["Profit Factor"], summary["Win Rate (%)"]
    avg = net / summary["Total Trades"] if summary["Total Trades"] else 0.0
    with st.grid(5):
        st.metric("Initial capital", inr(capital), border=True, height="stretch", help="Starting balance")
        st.metric("Gross P/L", inr(gross), delta_for(gross, f"{gross / capital * 100:+.2f}%"),
                  delta_description="before charges", border=True, height="stretch")
        st.metric("Charges", inr(charges), border=True, height="stretch",
                  help=f"Brokerage {inr(summary['Brokerage'])} · Govt charges {inr(summary['Govt Charges'])}")
        st.metric("Net P/L", inr(net), delta_for(net, f"{net / capital * 100:+.2f}%"),
                  delta_description="on capital", border=True, height="stretch")
        st.metric("Final capital", inr(summary["Final Capital"]), delta_for(net, inr(net)),
                  delta_description="after charges", border=True, height="stretch")
    with st.grid(5):
        st.metric("Total trades", summary["Total Trades"], border=True, height="stretch", help="Round trips")
        st.metric("Win rate", f"{wr:.2f}%", border=True, height="stretch", help="Net of charges")
        st.metric("Profit factor", "∞" if pf == float("inf") else f"{pf:.2f}", border=True, height="stretch",
                  help="Net wins / net losses")
        st.metric("Max drawdown", f"{summary['Max Drawdown (%)']:.2f}%", border=True, height="stretch",
                  help="Peak to trough")
        st.metric("Avg net / trade", inr(avg), border=True, height="stretch", help="Per round trip")
    if summary["Skipped (insufficient capital)"]:
        st.warning(f"{summary['Skipped (insufficient capital)']} signal(s) skipped: capital below one lot.")

    if bt["info"]:
        info = bt["info"]
        with st.expander("Option chain details (strikes and contracts fetched)", icon=":material/link:"):
            st.write(f"**Spot:** {info['Spot symbol']} · **Expiry:** {info['Expiry']} · **Strike rule:** {info['Strike rule']}")
            for side, ks in info["Strikes fetched"].items():
                st.write(f"**{side} strikes selected by the LTP movement ({len(ks)}):** " + ", ".join(f"{k:g}" for k in ks))
            st.dataframe(pd.DataFrame({"Contract": list(info["Contracts"]), "Candles": list(info["Contracts"].values())}),
                         hide_index=True, alt="Contracts fetched and their candle counts")
            for issue in info["Issues"]:
                st.warning(issue)

    st.subheader("Equity curve")
    if not trades_df.empty:
        fig = px.line(trades_df, x="Exit Time", y="Cumulative Capital", title="Strategy equity curve (net of charges)",
                      markers=True, hover_data=["Symbol", "Net PnL", "Exit Reason"])
        fig.update_traces(line_color=sign_color(net), marker_color=sign_color(net))
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No completed trades generated during this period with current parameters.")

    st.subheader("Executed trades")
    if not trades_df.empty:
        money = ["Entry Price", "Exit Price", "Gross PnL", "Brokerage", "Govt Charges", "Total Charges", "Net PnL",
                 "Cumulative Capital"]
        styler = trades_df.style.format({**{c: "{:,.2f}" for c in money}, "Return (%)": "{:+.2f}%"})
        paint = getattr(styler, "map", None) or styler.applymap
        styler = paint(pnl_style, subset=["Gross PnL", "Net PnL", "Return (%)"])
        styler = (getattr(styler, "map", None) or styler.applymap)(lambda v: f"color: {AMBER}", subset=["Brokerage", "Govt Charges", "Total Charges"])
        st.dataframe(styler, alt="Ledger of executed trades")
    else:
        st.dataframe(trades_df, alt="Ledger of executed trades (empty)")
    st.download_button(
        label="Download trades report as CSV",
        data=trades_df.to_csv(index=False).encode("utf-8"),
        file_name=f"backtest_trades_{bt['label'].replace(':', '_').replace(' ', '_')}.csv",
        mime="text/csv",
        icon=":material/download:",
    )
