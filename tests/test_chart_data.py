"""Price chart payload: symbol dropdown order and labels, RSI series (pure functions, no Streamlit)."""
import numpy as np
import pandas as pd

from src.chart_data import build_payload, symbol_label, symbol_options

SPOT = "NSE:NIFTY50-INDEX"
# Weekly tickers run the expiry day and the strike together: NIFTY 26 + 11 (Nov) + 13 (day) + 24900.
CE_LOW, CE_HIGH = "NSE:NIFTY26111324900CE", "NSE:NIFTY26111325000CE"
PE_LOW, PE_HIGH = "NSE:NIFTY26111324900PE", "NSE:NIFTY26111325000PE"
STRIKES = {"CE": [24900.0, 25000.0], "PE": [24900.0, 25000.0]}


def test_options_underlying_first_then_ce_by_strike_then_pe_by_strike():
    shuffled = [PE_HIGH, CE_HIGH, SPOT, PE_LOW, CE_LOW]
    assert symbol_options(shuffled, SPOT, STRIKES) == [SPOT, CE_LOW, CE_HIGH, PE_LOW, PE_HIGH]


def test_longest_matching_strike_wins():
    assert symbol_label("NSE:NIFTY26111324900CE", SPOT, {"CE": [900.0, 24900.0]}) == "24900 CE"


def test_options_single_instrument_has_one_entry():
    assert symbol_options([SPOT], SPOT) == [SPOT]


def test_labels():
    assert symbol_label(CE_LOW, SPOT, STRIKES) == "24900 CE"
    assert symbol_label(PE_HIGH, SPOT, STRIKES) == "25000 PE"
    assert symbol_label(SPOT, SPOT, STRIKES) == SPOT
    assert symbol_label(CE_LOW, SPOT) == CE_LOW  # strikes unknown: keep the ticker
    assert symbol_label("NSE:SBIN-EQ", "NSE:SBIN-EQ") == "NSE:SBIN-EQ"


def _candles(n=6, rsi=True):
    ts = pd.date_range("2026-11-02 09:15", periods=n, freq="5min")
    df = pd.DataFrame({"timestamp": ts, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5})
    if rsi:
        df["rsi"] = [np.nan, np.nan, 40.123, 55.0, 70.456, 62.0][:n]
        df["rsi_ma"] = [np.nan, np.nan, np.nan, 50.0, 55.555, 58.0][:n]
    return df


def test_payload_rsi_series_align_with_bars_and_null_when_not_warm():
    payload = build_payload(_candles(), pd.DataFrame(), SPOT, SPOT, "2026-11-02", "5")
    assert len(payload["rsi"]) == len(payload["t"]) == len(payload["rsi_ma"]) == 6
    assert payload["rsi"][:2] == [None, None]
    assert payload["rsi"][2:5] == [40.12, 55.0, 70.46]
    assert payload["rsi_ma"][3] == 50.0


def test_payload_without_rsi_columns_has_no_rsi_keys():
    payload = build_payload(_candles(rsi=False), pd.DataFrame(), SPOT, SPOT, "2026-11-02", "5")
    assert "rsi" not in payload and "rsi_ma" not in payload


def test_payload_rsi_is_clipped_to_start_date_with_candles():
    df = _candles()
    payload = build_payload(df, pd.DataFrame(), SPOT, SPOT, df["timestamp"].iloc[2], "5")
    assert len(payload["t"]) == len(payload["rsi"]) == 4
    assert payload["rsi"][0] == 40.12
