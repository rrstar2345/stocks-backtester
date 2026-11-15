"""Engine output used by the UI: trade numbering, cumulative capital, candles kept for charts.
A stub strategy puts the signals on known bars, so every fill and P/L is easy to check by hand."""
import numpy as np
import pandas as pd

from src.backtester import BacktestEngine, ExecConfig
from src.optionchain import ChainData
from src.strategies import Strategy

CAPITAL = 100000.0
N = 12
SPOT = "NSE:NIFTY50-INDEX"
CE = "NSE:NIFTY26111324900CE"


def _signals(entries, exits):
    def compute(df, **_):
        out = df.copy()
        for col in ("long_entry", "long_exit", "short_entry", "short_exit"):
            out[col] = False
        out.loc[list(entries), "long_entry"] = True
        out.loc[list(exits), "long_exit"] = True
        return out
    return Strategy(name="stub", params=[], compute=compute)


def _candles(n=N, base=100.0):
    """Daily bars whose open is base + bar number, so the fill of bar i is base + i."""
    ts = pd.date_range("2026-11-02", periods=n, freq="D")
    i = np.arange(n, dtype=float)
    return pd.DataFrame({"timestamp": ts, "open": base + i, "high": base + i + 0.5,
                         "low": base + i - 0.5, "close": base + i + 0.25})


# Fixed quantity, no costs, no target / stop, daily bars: a trade is just (exit open - entry open) * qty.
CFG = ExecConfig(direction="long", sizing="fixed", fixed_qty=10, bar_minutes=None, square_off=False,
                 brokerage=0.0, govt_pct=0.0)


def test_trades_are_numbered_from_one_in_exit_order():
    # Signals on bar close, filled at the next open: enter at 2, exit at 4; enter at 6, exit at 8.
    res = BacktestEngine(CAPITAL).run(_candles(), _signals([1, 5], [3, 7]), {}, CFG, symbol=SPOT)
    t = res.trades
    assert t["Trade #"].tolist() == [1, 2]
    assert t.columns[0] == "Trade #"
    assert t["Entry Price"].tolist() == [102.0, 106.0]
    assert t["Exit Price"].tolist() == [104.0, 108.0]
    assert t["Net PnL"].tolist() == [20.0, 20.0]
    assert t["Cumulative Capital"].tolist() == [CAPITAL + 20, CAPITAL + 40]
    assert (t["Exit Time"] >= t["Entry Time"]).all()
    assert t["Exit Time"].is_monotonic_increasing


def test_open_position_is_closed_on_the_last_bar_and_numbered():
    res = BacktestEngine(CAPITAL).run(_candles(), _signals([9], []), {}, CFG, symbol=SPOT)
    assert res.trades["Trade #"].tolist() == [1]
    assert res.trades["Exit Reason"].tolist() == ["End of data"]
    assert res.trades["Exit Time"].iloc[0] == _candles()["timestamp"].iloc[-1]


def test_no_signals_gives_an_empty_ledger_and_a_zero_summary():
    res = BacktestEngine(CAPITAL).run(_candles(), _signals([], []), {}, CFG, symbol=SPOT)
    assert res.trades.empty
    assert res.summary["Total Trades"] == 0
    assert res.summary["Final Capital"] == CAPITAL


def test_summary_agrees_with_the_ledger():
    res = BacktestEngine(CAPITAL).run(_candles(), _signals([1, 5], [3, 7]), {}, CFG, symbol=SPOT)
    assert res.summary["Total Trades"] == 2
    assert res.summary["Net Profit / Loss"] == res.trades["Net PnL"].sum()
    assert res.summary["Final Capital"] == res.trades["Cumulative Capital"].iloc[-1]
    assert res.summary["Max Drawdown (%)"] == 0.0


def test_single_run_keeps_slim_candles_for_the_default_symbol():
    res = BacktestEngine(CAPITAL).run(_candles(), _signals([1], [3]), {}, CFG, symbol=SPOT)
    assert res.default_symbol == SPOT
    assert list(res.candles) == [SPOT]
    assert list(res.candles[SPOT].columns) == ["timestamp", "open", "high", "low", "close"]  # no signal columns
    assert len(res.candles[SPOT]) == N


def _chain():
    spot = _candles(base=25000.0)
    ce = _candles(base=100.0)
    return ChainData(underlying=spot, contracts={CE: ce},
                     selection={"CE": np.array([CE] * N, dtype=object)}, info={"Spot symbol": SPOT})


def test_chain_run_numbers_trades_and_charts_the_underlying_by_default():
    res = BacktestEngine(CAPITAL).run_chain(_chain(), _signals([1, 5], [3, 7]), {}, CFG)
    assert res.trades["Trade #"].tolist() == [1, 2]
    assert res.trades["Symbol"].tolist() == [CE, CE]  # trades are on the contract, not the spot
    assert res.default_symbol == SPOT
    assert set(res.candles) == {SPOT, CE}
    assert res.candles[SPOT]["close"].iloc[0] > 20000  # the underlying's own candles
