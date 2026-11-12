import streamlit as st
import pandas as pd
import plotly.express as px
from src.auth import save_credentials, load_credentials, get_auth_url, generate_access_token, get_fyers_client
from src.database import fetch_history_fyers, fetch_candles_from_db
from src.backtester import BacktestEngine, get_expiry_dates, resolve_option_symbols

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

# --- Main Dashboard Configuration ---
fyers = get_fyers_client()
if not fyers:
    st.warning("⚠️ Please authenticate via the sidebar to connect to Fyers API.")
else:
    st.success("Connected to Fyers API Active Session.")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        symbol = st.text_input("Asset Symbol", value="NSE:NIFTY50-INDEX")
    with col2:
        resolution = st.selectbox("Resolution", ["1", "5", "15", "60", "D"], index=0)
    with col3:
        initial_capital = st.number_input("Initial Capital (₹)", value=500000.0, step=50000.0)

    col4, col5 = st.columns(2)
    with col4:
        start_date = st.date_input("Start Date", value=pd.to_datetime("2026-01-01"))
    with col5:
        end_date = st.date_input("End Date", value=pd.to_datetime("2026-03-01"))

    # Option Chain integration test / pre-fetch
    is_options = st.checkbox("Enable Dynamic Option Chain Symbol Discovery")
    target_symbol = symbol
    
    if is_options:
        st.info("Fetching option chain expiries...")
        expiries = get_expiry_dates(fyers, symbol)
        if expiries:
            expiry_dates_list = [exp.get("date") for exp in expiries]
            selected_expiry = st.selectbox("Select Expiry Date", expiry_dates_list)
            
            # Fetch recent spot price to discover ATM strike
            spot_df = fetch_history_fyers(fyers, symbol, resolution="D", start_date=str(start_date), end_date=str(end_date))
            if not spot_df.empty:
                last_spot = spot_df.iloc[-1]["close"]
                opt_symbols = resolve_option_symbols(symbol, selected_expiry, last_spot)
                strike_choices = [item[0] for item in opt_symbols]
                chosen_strike = st.selectbox("Select Strike Price", strike_choices)
                option_type = st.radio("Option Leg", ["CE", "PE"])
                
                for s_val, ce_s, pe_s in opt_symbols:
                    if s_val == chosen_strike:
                        target_symbol = ce_s if option_type == "CE" else pe_s
                st.write(f"Target Resolved Option Contract: **{target_symbol}**")
        else:
            st.warning("Could not fetch option chain. Defaulting to underlying symbol.")

    if st.button("📥 Fetch Historical Data & Run Backtest"):
        with st.spinner("Fetching data and executing simulation..."):
            # 1. Fetch / Cache data locally in DuckDB
            df_hist = fetch_history_fyers(fyers, target_symbol, resolution=resolution, start_date=str(start_date), end_date=str(end_date))
            if df_hist.empty:
                # Fallback to DB check
                df_hist = fetch_candles_from_db(target_symbol, resolution, str(start_date), str(end_date))
            
            if df_hist.empty:
                st.error("No historical data found for the selected range/symbol.")
            else:
                st.success(f"Loaded {len(df_hist)} historical candles (Cached in DuckDB local storage).")
                
                # 2. Run Strategy Backtest
                engine = BacktestEngine(initial_capital=initial_capital)
                summary, trades_df, processed_df = engine.run_sma_crossover(df_hist)
                
                # 3. Display Summary Metrics
                st.subheader("📊 Performance Summary")
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Initial Capital", f"₹{summary['Initial Capital']:,.2f}")
                m2.metric("Final Capital", f"₹{summary['Final Capital']:,.2f}")
                m3.metric("Net PnL", f"₹{summary['Net Profit / Loss']:,.2f}", delta=f"{summary['Net Profit / Loss']:,.2f}")
                m4.metric("Win Rate", f"{summary['Win Rate (%)']:.2f}%")
                m5.metric("Max Drawdown", f"{summary['Max Drawdown (%)']:.2f}%")
                
                # 4. Charts & Visualizations
                st.subheader("📈 Equity Curve")
                if not trades_df.empty:
                    fig = px.line(trades_df, x="Exit Time", y="Cumulative Capital", title="Strategy Equity Curve Over Time", markers=True)
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("No completed trades generated during this period with current parameters.")

                # 5. Trade Ledger & Exports
                st.subheader("📋 Executed Trades Detail Ledger")
                st.dataframe(trades_df, use_container_width=True)
                
                # Export Options
                csv_data = trades_df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="Download Trades Report as CSV",
                    data=csv_data,
                    file_name=f"backtest_trades_{target_symbol.replace(':', '_')}.csv",
                    mime="text/csv"
                )