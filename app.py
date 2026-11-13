import uuid
from datetime import date, time as dtime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from src import optionchain, symbols
from src.auth import save_credentials, load_credentials, get_auth_url, generate_access_token, get_fyers_client
from src.backtester import BacktestEngine, ExecConfig, RESOLUTIONS, bar_minutes, warmup_days
from src.database import fetch_history_fyers, fetch_candles_from_db
from src.strategies import STRATEGIES

st.set_page_config(page_title="Fyers Algorithmic Backtesting Dashboard", layout="wide")
st.title("📈 Fyers API v3 DuckDB Backtesting Engine")

# --- Authentication Sidebar ---
st.sidebar.header("🔐 Fyers API Authentication")
saved_cred = load_credentials()

with st.sidebar.form("auth_form"):
    app_id = st.text_input("App ID (Client ID)", value=saved_cred["client_id"] if saved_cred else "")
    secret_id = st.text_input("Secret ID", type="password", value=saved_cred["secret_id"] if saved_cred else "")
    redirect_uri = st.text_input("Redirect URL", value=saved_cred["redirect_uri"] if saved_cred else "https://trade.fyers.in/api-login/redirect-uri/index.html")
    save_btn = st.form_submit_button("Save Credentials")

    if save_btn and app_id and secret_id:
        save_credentials(app_id, secret_id, redirect_uri)
        st.success("Credentials securely encrypted and saved locally!")

if app_id and redirect_uri:
    st.sidebar.subheader("Step 1: Get Authorization Code")
    auth_url = get_auth_url(app_id, redirect_uri)
    st.sidebar.markdown(f"[Click here to login & authorize]({auth_url})", unsafe_allow_html=True)

    auth_code_input = st.sidebar.text_input("Paste Auth Code from Redirect URL")
    if st.sidebar.button("Generate Access Token"):
        try:
            generate_access_token(app_id, secret_id, redirect_uri, auth_code_input)
            st.sidebar.success("Access Token generated and saved successfully!")
        except Exception as e:
            st.sidebar.error(f"Error: {e}")

# --- Symbol master (downloaded once a day, cached) ---
st.sidebar.divider()
st.sidebar.subheader("Symbol master")
try:
    master = symbols.load_master(symbols.today_ist())
    st.sidebar.caption(f"{len(master):,} symbols loaded for {symbols.today_ist()}")
except Exception as e:
    master = None
    st.sidebar.error(f"Symbol download failed: {e}")
if st.sidebar.button("Refresh symbols"):
    symbols.load_master.clear()
    st.rerun()


st.markdown("""
<style>
.kpi{border-radius:14px;padding:14px 16px;border:1px solid rgba(128,128,128,.18);border-left:6px solid var(--c);
     background:linear-gradient(135deg,var(--bg),transparent);margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.kpi .l{font-size:.72rem;letter-spacing:.07em;text-transform:uppercase;opacity:.75;font-weight:600}
.kpi .v{font-size:1.55rem;font-weight:700;color:var(--c);line-height:1.3}
.kpi .s{font-size:.78rem;opacity:.7;min-height:1.1em}
</style>""", unsafe_allow_html=True)

BLUE, GREEN, RED, AMBER, PURPLE, TEAL, SLATE = "#3b82f6", "#16a34a", "#dc2626", "#f59e0b", "#8b5cf6", "#0d9488", "#64748b"


def wide(fn, *args, **kwargs):
    """Full-width widget on both old and new Streamlit (use_container_width is being removed)."""
    try:
        return fn(*args, width="stretch", **kwargs)
    except TypeError:
        return fn(*args, use_container_width=True, **kwargs)


def kpi(label, value, sub="", color=BLUE):
    return (f'<div class="kpi" style="--c:{color};--bg:{color}22"><div class="l">{label}</div>'
            f'<div class="v">{value}</div><div class="s">{sub}</div></div>')


def sign_color(v):
    return GREEN if v > 0 else RED if v < 0 else SLATE


def inr(v):
    return f"{'-' if v < 0 else ''}₹{abs(v):,.2f}"


def pnl_style(v):
    return f"color: {sign_color(v)}; font-weight: 600"


def pick_instrument():
    """Returns (fyers_symbol, lot_size)."""
    if master is None:
        custom = st.text_input("Fyers symbol (symbol master unavailable)", value="NSE:NIFTY50-INDEX")
        return custom.strip(), 1

    exchange = st.selectbox("Exchange / segment", symbols.EXCHANGES, index=2)
    if exchange in symbols.DERIVATIVE_EXCHANGES:
        c1, c2, c3 = st.columns(3)
        kind = c1.radio("Instrument", ["OPT", "FUT"], format_func=lambda k: "Option" if k == "OPT" else "Future", horizontal=True)
        pool = symbols.contracts(master, exchange, kind)
        names = sorted(pool["underlying"].dropna().unique())
        if not names:
            st.warning("No contracts found for this selection.")
            return "", 1
        underlying = c2.selectbox("Underlying", names, index=names.index("NIFTY") if "NIFTY" in names else 0)
        pool = pool[pool["underlying"] == underlying]
        expiries = sorted(pool["expiry"].dropna().unique())
        expiry = c3.selectbox("Expiry", expiries, format_func=lambda d: pd.Timestamp(d).strftime("%d-%b-%Y"))
        pool = pool[pool["expiry"] == expiry]
        if kind == "OPT":
            c4, c5 = st.columns(2)
            opt_type = c4.radio("Option type", ["CE", "PE"], horizontal=True)
            strikes = sorted(pool.loc[pool["instrumenttype"] == opt_type, "strike"].unique())
            strike = c5.selectbox("Strike", strikes, index=len(strikes) // 2, format_func=lambda s: f"{s:g}")
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
    c1, c2 = st.columns(2)
    exchange = c1.selectbox("Derivatives segment", ["NFO", "BFO"])
    names = sorted(symbols.contracts(master, exchange, "OPT")["underlying"].dropna().unique())
    if not names:
        st.warning("No option contracts found.")
        return None
    default = "NIFTY" if exchange == "NFO" else "SENSEX"
    underlying = c2.selectbox("Underlying (index / equity)", names, index=names.index(default) if default in names else 0)

    c3, c4, c5 = st.columns(3)
    expiry_choice = c3.selectbox("Expiry", optionchain.EXPIRY_CHOICES)
    strike_choice = c4.selectbox("Strike", optionchain.STRIKE_CHOICES)
    side = c5.selectbox("Option side", ["Both", "CE", "PE"])

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


# --- Main Dashboard Configuration ---
fyers = get_fyers_client()
if not fyers:
    st.warning("⚠️ Please authenticate via the sidebar to connect to Fyers API.")
else:
    st.success("Connected to Fyers API Active Session.")

    st.subheader("Instrument & data")
    mode = st.radio("Mode", ["Single instrument", "Option chain"], horizontal=True)
    chain_cfg = None
    if mode == "Option chain":
        chain_cfg = pick_chain()
        target_symbol, lot_default = "", (chain_cfg["lot"] if chain_cfg else 1)
        st.caption("Option chain: the underlying's close picks the strike on every bar; the strategy runs on "
                   "that option's premium (long entries only, i.e. buying CE / PE). The range is clipped to the expiry date.")
    else:
        target_symbol, lot_default = pick_instrument()
        override = st.text_input("Override with a Fyers symbol (optional, e.g. an expired contract)")
        if override.strip():
            target_symbol = override.strip()

    c1, c2, c3, c4 = st.columns(4)
    resolution = c1.selectbox("Resolution", RESOLUTIONS, index=RESOLUTIONS.index("5"))
    initial_capital = c2.number_input("Initial Capital (₹)", value=100000.0, step=10000.0)
    start_date = c3.date_input("Start Date", value=date.today() - timedelta(days=30))
    end_date = c4.date_input("End Date", value=date.today())
    refresh = st.checkbox("Ignore local cache (re-fetch everything from Fyers)", value=False,
                          help="By default only symbol / date / resolution ranges missing from the local DuckDB cache are requested.")

    st.subheader("Strategy")
    strategy = STRATEGIES[st.selectbox("Strategy", list(STRATEGIES))]
    params, cols = {}, st.columns(len(strategy.params))
    for col, p in zip(cols, strategy.params):
        key = f"{strategy.name}.{p.key}"
        if p.kind == "int":
            params[p.key] = col.number_input(p.label, int(p.min), int(p.max), int(p.default), 1, key=key)
        elif p.kind == "float":
            params[p.key] = col.number_input(p.label, float(p.min), float(p.max), float(p.default), 1.0, key=key)
        else:
            params[p.key] = col.selectbox(p.label, p.options, index=p.options.index(p.default), key=key)

    st.subheader("Trade settings")
    t1, t2, t3, t4 = st.columns(4)
    if mode == "Option chain":
        direction = "Long only"
        t1.selectbox("Direction", ["Long only"], disabled=True)
    else:
        direction = t1.selectbox("Direction", ["Long only", "Short only", "Long & Short"])
    tgt_kind = t2.selectbox("Target", ["Off", "Points", "Percent"], index=1)
    tgt_val = t2.number_input("Target value", min_value=0.0, value=30.0, step=1.0)
    sl_kind = t3.selectbox("Stop loss", ["Off", "Points", "Percent"], index=1)
    sl_val = t3.number_input("Stop loss value", min_value=0.0, value=25.0, step=1.0)
    sizing = t4.selectbox("Position sizing", ["Lots from capital", "Fixed quantity"])
    lot_size = t4.number_input("Lot size", min_value=1, value=lot_default, step=1) if sizing == "Lots from capital" else 1
    fixed_qty = t4.number_input("Quantity", min_value=1, value=max(lot_default, 1), step=1) if sizing == "Fixed quantity" else 1

    intraday_res = bar_minutes(resolution) is not None
    w1, w2, w3, w4, w5 = st.columns(5)
    square_off = w1.checkbox("Intraday: square off daily", value=True, disabled=not intraday_res)
    win_start = w2.time_input("Entries from", value=dtime(9, 15), disabled=not (intraday_res and square_off))
    sq_at = w3.time_input("Square off at", value=dtime(15, 29), disabled=not (intraday_res and square_off))
    brokerage = w4.number_input("Brokerage per order (₹)", min_value=0.0, value=20.0, step=1.0)
    govt_pct = w5.number_input("Govt charges on sell (%)", min_value=0.0, value=0.05, step=0.01, format="%.3f")

    if st.button("📥 Fetch Historical Data & Run Backtest"):
        cfg = ExecConfig(
            direction={"Long only": "long", "Short only": "short", "Long & Short": "both"}[direction],
            target=rule(tgt_kind, tgt_val), stop=rule(sl_kind, sl_val),
            sizing="capital" if sizing == "Lots from capital" else "fixed",
            lot_size=int(lot_size), fixed_qty=int(fixed_qty),
            bar_minutes=bar_minutes(resolution), square_off=square_off,
            window_start=win_start, square_off_at=sq_at, brokerage=brokerage, govt_pct=govt_pct,
        )
        engine = BacktestEngine(initial_capital)
        st.session_state.pop("bt", None)

        if mode == "Option chain":
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

    bt = st.session_state.get("bt")
    if bt:
        summary, trades_df = bt["summary"], bt["trades"]
        d = bt.get("data") or {}
        st.success(f"{bt['label']}: {bt['n']} underlying candles. Data: {d.get('api_calls', 0)} Fyers API call(s) · "
                   f"{d.get('from_cache', 0)} symbol(s) served fully from the local cache · "
                   f"{d.get('fetched', 0)} fetched / topped up.")

        st.subheader("📊 Performance Summary")
        net, gross, charges = summary["Net Profit / Loss"], summary["Gross Profit / Loss"], summary["Total Charges"]
        ret = net / summary["Initial Capital"] * 100
        r1 = st.columns(5)
        r1[0].markdown(kpi("Initial Capital", inr(summary["Initial Capital"]), "Starting balance", BLUE), unsafe_allow_html=True)
        r1[1].markdown(kpi("Gross Profit / Loss", inr(gross), "Before charges", sign_color(gross)), unsafe_allow_html=True)
        r1[2].markdown(kpi("Charges", inr(charges),
                           f"Brokerage {inr(summary['Brokerage'])} · Govt {inr(summary['Govt Charges'])}", AMBER),
                       unsafe_allow_html=True)
        r1[3].markdown(kpi("Net Profit / Loss", inr(net), f"{ret:+.2f}% on capital", sign_color(net)), unsafe_allow_html=True)
        r1[4].markdown(kpi("Final Capital", inr(summary["Final Capital"]), "After charges", sign_color(net)), unsafe_allow_html=True)
        pf = summary["Profit Factor"]
        wr = summary["Win Rate (%)"]
        r2 = st.columns(5)
        r2[0].markdown(kpi("Total Trades", summary["Total Trades"], "Round trips", SLATE), unsafe_allow_html=True)
        r2[1].markdown(kpi("Win Rate", f"{wr:.2f}%", "Net of charges", GREEN if wr >= 50 else AMBER), unsafe_allow_html=True)
        r2[2].markdown(kpi("Profit Factor", "∞" if pf == float("inf") else f"{pf:.2f}", "Net wins / net losses",
                           GREEN if pf >= 1.5 else AMBER if pf >= 1 else RED), unsafe_allow_html=True)
        r2[3].markdown(kpi("Max Drawdown", f"{summary['Max Drawdown (%)']:.2f}%", "Peak to trough", RED), unsafe_allow_html=True)
        avg = net / summary["Total Trades"] if summary["Total Trades"] else 0.0
        r2[4].markdown(kpi("Avg Net / Trade", inr(avg), "Per round trip", sign_color(avg)), unsafe_allow_html=True)
        if summary["Skipped (insufficient capital)"]:
            st.warning(f"{summary['Skipped (insufficient capital)']} signal(s) skipped: capital below one lot.")

        if bt["info"]:
            info = bt["info"]
            with st.expander("🔗 Option chain details (strikes & contracts fetched)"):
                st.write(f"**Spot:** {info['Spot symbol']} · **Expiry:** {info['Expiry']} · **Strike rule:** {info['Strike rule']}")
                for side, ks in info["Strikes fetched"].items():
                    st.write(f"**{side} strikes selected by the LTP movement ({len(ks)}):** " + ", ".join(f"{k:g}" for k in ks))
                wide(st.dataframe, pd.DataFrame({"Contract": list(info["Contracts"]), "Candles": list(info["Contracts"].values())}),
                     hide_index=True)
                for issue in info["Issues"]:
                    st.warning(issue)

        st.subheader("📈 Equity Curve")
        if not trades_df.empty:
            fig = px.line(trades_df, x="Exit Time", y="Cumulative Capital", title="Strategy Equity Curve (net of charges)",
                          markers=True, hover_data=["Symbol", "Net PnL", "Exit Reason"])
            fig.update_traces(line_color=sign_color(net), marker_color=sign_color(net))
            wide(st.plotly_chart, fig)
        else:
            st.info("No completed trades generated during this period with current parameters.")

        st.subheader("📋 Executed Trades Detail Ledger")
        if not trades_df.empty:
            money = ["Entry Price", "Exit Price", "Gross PnL", "Brokerage", "Govt Charges", "Total Charges", "Net PnL",
                     "Cumulative Capital"]
            styler = trades_df.style.format({**{c: "{:,.2f}" for c in money}, "Return (%)": "{:+.2f}%"})
            paint = getattr(styler, "map", None) or styler.applymap
            styler = paint(pnl_style, subset=["Gross PnL", "Net PnL", "Return (%)"])
            styler = (getattr(styler, "map", None) or styler.applymap)(lambda v: f"color: {AMBER}", subset=["Brokerage", "Govt Charges", "Total Charges"])
            wide(st.dataframe, styler)
        else:
            wide(st.dataframe, trades_df)
        st.download_button(
            label="Download Trades Report as CSV",
            data=trades_df.to_csv(index=False).encode("utf-8"),
            file_name=f"backtest_trades_{bt['label'].replace(':', '_').replace(' ', '_')}.csv",
            mime="text/csv",
        )
