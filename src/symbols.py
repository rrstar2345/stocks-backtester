"""Fyers symbol master: download once a day, cache, and resolve symbols.

Source files / layout follow fyers-openalgo/database/master_contract_db.py.
Every row is normalised to: exchange, symbol, name, brsymbol (the Fyers ticker
used for API calls), token, lotsize, tick_size, instrumenttype, underlying,
expiry, strike.
"""
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")
BASE_URL = "https://public.fyers.in/sym_details"
CSV_FILES = {
    "NSE_CM": "NSE_CM.csv",
    "BSE_CM": "BSE_CM.csv",
    "NSE_FO": "NSE_FO.csv",
    "BSE_FO": "BSE_FO.csv",
}
JSON_FILES = {
    "NSE_CD": "NSE_CD_sym_master.json",
    "MCX_COM": "MCX_COM_sym_master.json",
}
CSV_HEADERS = [
    "Fytoken", "Symbol Details", "Exchange Instrument type", "Minimum lot size", "Tick size",
    "ISIN", "Trading Session", "Last update date", "Expiry date", "Symbol ticker", "Exchange",
    "Segment", "Scrip code", "Underlying symbol", "Underlying scrip code", "Strike price",
    "Option type", "Underlying FyToken", "Reserved column1", "Reserved column2",
    "Reserved column3",
]
EXCHANGES = ["NSE", "BSE", "NSE_INDEX", "BSE_INDEX", "NFO", "BFO", "CDS", "MCX"]
DERIVATIVE_EXCHANGES = ["NFO", "BFO", "CDS", "MCX"]
COLUMNS = ["exchange", "symbol", "name", "brsymbol", "token", "lotsize", "tick_size",
           "instrumenttype", "underlying", "expiry", "strike", "search_key"]


def _download(name):
    resp = requests.get(f"{BASE_URL}/{name}", timeout=60)
    resp.raise_for_status()
    return resp.content


def _fmt_strike(x):
    return str(int(x)) if x == int(x) else str(x)


def _cash_frame(raw, eq_exchange, idx_exchange, eq_types):
    t = pd.to_numeric(raw["Exchange Instrument type"], errors="coerce")
    out = []
    eq = raw[t.isin(eq_types)]
    out.append(pd.DataFrame({
        "exchange": eq_exchange, "symbol": eq["Underlying symbol"], "name": eq["Symbol Details"],
        "brsymbol": eq["Symbol ticker"], "token": eq["Fytoken"], "lotsize": 1,
        "tick_size": pd.to_numeric(eq["Tick size"], errors="coerce"), "instrumenttype": "EQ",
        "underlying": eq["Underlying symbol"],
    }))
    idx = raw[t == 10]
    idx_symbol = idx["Underlying symbol"].str.upper().str.replace(r"[\s-]", "", regex=True)
    out.append(pd.DataFrame({
        "exchange": idx_exchange, "symbol": idx_symbol, "name": idx["Symbol Details"],
        "brsymbol": idx["Symbol ticker"], "token": idx["Fytoken"], "lotsize": 1,
        "tick_size": pd.to_numeric(idx["Tick size"], errors="coerce"), "instrumenttype": "INDEX",
        "underlying": idx_symbol,
    }))
    return pd.concat(out, ignore_index=True)


def _deriv_frame(exchange, underlying, name, token, lot, tick, expiry_epoch, strike, opt, ticker):
    opt = opt.fillna("XX").replace({"XX": "FUT"})
    expiry = (pd.to_datetime(pd.to_numeric(expiry_epoch, errors="coerce"), unit="s", utc=True)
              .dt.tz_convert(IST).dt.tz_localize(None).dt.normalize())
    strike = pd.to_numeric(strike, errors="coerce").fillna(0.0)
    code = expiry.dt.strftime("%d%b%y").str.upper()
    is_opt = opt.isin(["CE", "PE"])
    symbol = underlying + code + opt.where(~is_opt, "")  # FUT suffix for futures
    symbol = symbol.where(~is_opt, underlying + code + strike.map(_fmt_strike) + opt)
    return pd.DataFrame({
        "exchange": exchange, "symbol": symbol, "name": name, "brsymbol": ticker, "token": token,
        "lotsize": pd.to_numeric(lot, errors="coerce").fillna(1).astype(int),
        "tick_size": pd.to_numeric(tick, errors="coerce"), "instrumenttype": opt,
        "underlying": underlying, "expiry": expiry, "strike": strike.where(is_opt),
    })


def _read_csv(content):
    return pd.read_csv(io.BytesIO(content), names=CSV_HEADERS, header=None, dtype=str)


def download_master():
    """Download all Fyers master files and return one normalised DataFrame."""
    frames = []
    raw = {k: _read_csv(_download(v)) for k, v in CSV_FILES.items()}
    frames.append(_cash_frame(raw["NSE_CM"], "NSE", "NSE_INDEX", [0, 9]))
    frames.append(_cash_frame(raw["BSE_CM"], "BSE", "BSE_INDEX", [0, 4, 50]))
    for key, exch in (("NSE_FO", "NFO"), ("BSE_FO", "BFO")):
        r = raw[key]
        frames.append(_deriv_frame(
            exch, r["Symbol Details"].str.split().str[0], r["Symbol Details"], r["Fytoken"],
            r["Minimum lot size"], r["Tick size"], r["Expiry date"], r["Strike price"],
            r["Option type"], r["Symbol ticker"]))
    for key, exch in (("NSE_CD", "CDS"), ("MCX_COM", "MCX")):
        r = pd.read_json(io.BytesIO(_download(JSON_FILES[key])), orient="index")
        frames.append(_deriv_frame(
            exch, r["symDetails"].str.split().str[0], r["symbolDetails"], r["fyToken"].astype(str),
            r["qtyMultiplier"], r["tickSize"], r["expiryDate"], r["strikePrice"],
            r["optType"], r["symTicker"]))
    df = pd.concat(frames, ignore_index=True)
    df["search_key"] = (df["symbol"].fillna("") + " " + df["name"].fillna("")).str.upper()
    return df[COLUMNS]


def today_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")


@st.cache_data(persist="disk", show_spinner="Downloading Fyers symbol master (once a day)...",
               max_entries=2)
def load_master(day: str) -> pd.DataFrame:
    """Cached per IST calendar day, so the download happens once a day (also across restarts)."""
    return download_master()


# ---------------------------------------------------------------- lookups
def search_symbols(master, exchange, query, limit=100):
    d = master[master["exchange"] == exchange]
    q = query.strip().upper()
    if q:
        d = d[d["search_key"].str.contains(q, regex=False)]
        rank = (d["symbol"] != q).astype(int) * 2 + (~d["symbol"].str.startswith(q)).astype(int)
        d = d.assign(_r=rank).sort_values(["_r", "symbol"]).drop(columns="_r")
    return d.head(limit)


def contracts(master, exchange, kind, underlying=None):
    """Derivative rows. kind: 'OPT' (CE/PE) or 'FUT'."""
    d = master[master["exchange"] == exchange]
    d = d[d["instrumenttype"].isin(["CE", "PE"])] if kind == "OPT" else d[d["instrumenttype"] == "FUT"]
    return d if underlying is None else d[d["underlying"] == underlying]
