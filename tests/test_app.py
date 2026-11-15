"""Smoke tests for app.py with `AppTest`: synthetic candles, no Fyers, no network.

Mocks sit on the helper modules the script imports (the symbol master download, the connection check and
the history fetch), as `AppTest` runs the script itself rather than importing it.
Not covered here (browser only): chart rendering, ledger row selection, the auth dialog's form.
"""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.backtester import BacktestEngine, ExecConfig
from src.database import AuthError
from src.optionchain import ChainData
from src.strategies import STRATEGIES

APP = str(Path(__file__).resolve().parent.parent / "app.py")
SPOT = "NSE:NIFTY50-INDEX"  # the symbol the app offers when the symbol master is unavailable
CE, PE = "NSE:NIFTY26111324900CE", "NSE:NIFTY26111324900PE"
STRIKES = {"CE": [24900.0], "PE": [24900.0]}


def _candles(base=25000.0, seed=7, days=8, vol=9.0, wave=40.0):
    """5-minute bars for the last `days` weekdays up to yesterday (inside the app's default 30-day range),
    from a seeded random walk with a slow wave on top so RSI crosses its average often."""
    rng = np.random.default_rng(seed)
    end = pd.Timestamp.today().normalize() - pd.Timedelta(days=1)
    sessions = pd.bdate_range(end=end, periods=days)
    ts = pd.DatetimeIndex([d + pd.Timedelta(hours=9, minutes=15 + 5 * i) for d in sessions for i in range(75)])
    n = len(ts)
    close = base + np.cumsum(rng.normal(0, vol, n)) + wave * np.sin(np.arange(n) / 9)
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"timestamp": ts, "open": open_, "high": np.maximum(open_, close) + 2,
                         "low": np.minimum(open_, close) - 2, "close": close, "volume": 1000})


@pytest.fixture
def connected():
    """Connected to Fyers (mocked), symbol master unavailable (single-instrument mode with a text symbol)."""
    with ExitStack() as stack:
        stack.enter_context(patch("src.auth_ui.session_status", return_value=("valid", "FY0001")))
        stack.enter_context(patch("src.auth.get_fyers_client", return_value=object()))
        stack.enter_context(patch("src.symbols.load_master", side_effect=RuntimeError("offline")))
        yield


def _run_backtest(at):
    at.button(key="run_backtest").click().run(timeout=30)
    return at


def test_disconnected_shows_the_connect_prompt_and_no_config():
    with patch("src.auth_ui.session_status", return_value=("missing", "")), \
         patch("src.symbols.load_master", side_effect=RuntimeError("offline")):
        at = AppTest.from_file(APP).run(timeout=30)
    assert not at.exception
    assert any("Connect to Fyers" in w.value for w in at.warning)
    assert len(at.get("echarts_chart")) == 0
    assert not [b for b in at.button if b.key == "run_backtest"]  # no configuration without a connection


def test_connected_start_shows_the_empty_state(connected):
    at = AppTest.from_file(APP).run(timeout=30)
    assert not at.exception
    assert at.button(key="run_backtest").disabled is False
    assert any("Run backtest" in i.value for i in at.info)
    assert len(at.metric) == 0


def test_single_instrument_run_shows_results(connected):
    with patch("src.database.fetch_history_fyers", return_value=_candles()):
        at = _run_backtest(AppTest.from_file(APP).run(timeout=30))
    assert not at.exception, [e.message for e in at.exception]
    bt = at.session_state["bt"]
    assert bt["default_symbol"] == SPOT and not bt["trades"].empty

    assert len(at.metric) == 10  # two rows of five KPI cards
    charts = at.get("echarts_chart")
    assert len(charts) == 2  # equity curve and drawdown
    ledger = at.dataframe(key=f"ledger:{bt['id']}")
    assert len(ledger.value) == len(bt["trades"])
    assert bt["trades"]["Trade #"].tolist() == list(range(1, len(bt["trades"]) + 1))

    picker = at.selectbox(key=f"chart_symbol:{bt['id']}")
    assert picker.disabled  # a single instrument has nothing to switch to
    assert len(at.dataframe) == 2  # ledger and exit reasons


def test_a_new_run_gets_a_new_result_id(connected):
    with patch("src.database.fetch_history_fyers", return_value=_candles()):
        at = _run_backtest(AppTest.from_file(APP).run(timeout=30))
        first = at.session_state["bt"]["id"]
        _run_backtest(at)
    assert at.session_state["bt"]["id"] != first
    assert not at.exception


def test_no_candles_reports_it_and_keeps_no_result(connected):
    with patch("src.database.fetch_history_fyers", return_value=pd.DataFrame()), \
         patch("src.database.fetch_candles_from_db", return_value=pd.DataFrame()):
        at = _run_backtest(AppTest.from_file(APP).run(timeout=30))
    assert not at.exception
    assert any("No historical data" in e.value for e in at.error)
    assert "bt" not in at.session_state


def test_expired_token_mid_run_offers_reauthentication(connected):
    boom = AuthError("Fyers rejected the access token while fetching NSE:NIFTY50-INDEX: token expired")
    with patch("src.database.fetch_history_fyers", side_effect=boom), \
         patch("src.database.fetch_candles_from_db", return_value=pd.DataFrame()):
        at = _run_backtest(AppTest.from_file(APP).run(timeout=30))
    assert not at.exception
    expired = [e for e in at.error if "token expired" in e.value]
    assert expired and "Log in again" in expired[0].value
    assert at.button(key="auth_expired_reauth").label == "Re-authenticate"


def test_expired_token_still_uses_the_local_cache(connected):
    boom = AuthError("Fyers rejected the access token while fetching NSE:NIFTY50-INDEX: token expired")
    with patch("src.database.fetch_history_fyers", side_effect=boom), \
         patch("src.database.fetch_candles_from_db", return_value=_candles()):
        at = _run_backtest(AppTest.from_file(APP).run(timeout=30))
    assert not at.exception
    assert at.button(key="auth_expired_reauth").label == "Re-authenticate"  # the prompt is there ...
    assert "bt" in at.session_state  # ... and the cached candles were still backtested


def _chain_result():
    """A finished option-chain run, built with the real engine on synthetic data."""
    spot = _candles()
    ce, pe = _candles(base=120.0, seed=3, vol=2.0, wave=3.0), _candles(base=110.0, seed=5, vol=2.0, wave=3.0)
    chain = ChainData(
        underlying=spot, contracts={CE: ce, PE: pe},
        selection={"CE": np.array([CE] * len(spot), dtype=object), "PE": np.array([PE] * len(spot), dtype=object)},
        info={"Spot symbol": SPOT, "Expiry": "13-Nov-2026", "Strike rule": "ATM", "Strikes fetched": STRIKES,
              "Contracts": {CE: len(ce), PE: len(pe)}, "Issues": [], "Data source": {}})
    cfg = ExecConfig(direction="long", target=("points", 6.0), stop=("points", 4.0), sizing="fixed", fixed_qty=50,
                     bar_minutes=5)
    res = BacktestEngine(100000.0).run_chain(chain, STRATEGIES["RSI cross + ADX trend"], {
        "rsi_length": 14, "rsi_ma_type": "EMA", "rsi_ma_length": 9, "adx_length": 14, "adx_threshold": 15.0}, cfg)
    return dict(id="chain-run", resolution="5", start=str(spot["timestamp"].min().date()), strategy="stub",
                params={}, label="NIFTY chain", n=len(spot), summary=res.summary, trades=res.trades,
                info=chain.info, data={}, candles=res.candles, default_symbol=res.default_symbol)


def test_chain_result_offers_the_underlying_then_contracts(connected):
    bt = _chain_result()
    assert not bt["trades"].empty, "the synthetic data should produce trades"
    at = AppTest.from_file(APP)
    at.session_state["bt"] = bt
    at.run(timeout=30)
    assert not at.exception, [e.message for e in at.exception]

    picker = at.selectbox(key="chart_symbol:chain-run")
    assert picker.options == [SPOT, "24900 CE", "24900 PE"]  # the labels shown: underlying, then contracts
    assert not picker.disabled
    assert picker.value == SPOT  # opens on the underlying

    picker.select(CE).run(timeout=30)
    assert not at.exception
    assert at.session_state["bt"]["id"] == "chain-run"  # switching contracts never reruns the backtest
    assert at.selectbox(key="chart_symbol:chain-run").value == CE


def test_empty_ledger_shows_the_no_trades_message(connected):
    bt = _chain_result()
    bt["trades"] = bt["trades"].iloc[0:0]
    at = AppTest.from_file(APP)
    at.session_state["bt"] = bt
    at.run(timeout=30)
    assert not at.exception, [e.message for e in at.exception]
    assert any("No completed trades" in i.value for i in at.info)
    assert len(at.get("echarts_chart")) == 0  # no equity or drawdown chart without trades
