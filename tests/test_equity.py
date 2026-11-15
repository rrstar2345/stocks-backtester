"""Equity curve, drawdown, exit-reason breakdown and colour rule (pure functions, no Streamlit)."""
import numpy as np
import pandas as pd
import pytest

from src.equity import AMBER, GREEN, RED, drawdown_curve, equity_curve, exit_reasons, gradient_color

CAPITAL = 1000.0


def _trades(rows):
    """rows: (exit time, net pnl, exit reason). Cumulative capital is built the way the engine builds it."""
    df = pd.DataFrame(rows, columns=["Exit Time", "Net PnL", "Exit Reason"])
    df["Exit Time"] = pd.to_datetime(df["Exit Time"])
    df["Cumulative Capital"] = CAPITAL + df["Net PnL"].cumsum()
    return df


def _days(*days):
    return pd.to_datetime(list(days))


def _bars(day, n=4):
    return pd.date_range(f"{day} 09:15", periods=n, freq="5min")


# --- equity_curve ---

def test_daily_curve_is_end_of_day_and_forward_filled():
    trades = _trades([("2026-11-03 10:00", 50, "Target"), ("2026-11-03 14:00", -20, "Stop Loss"),
                      ("2026-11-05 11:00", 30, "Signal")])
    bars = _days("2026-11-02 09:15", "2026-11-03 09:15", "2026-11-04 09:15", "2026-11-05 09:15")
    out = equity_curve(trades, CAPITAL, bars)
    assert out["time"].tolist() == list(_days("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"))
    assert out["equity"].tolist() == [1000.0, 1030.0, 1030.0, 1060.0]  # last exit of the day wins; flat days carry


def test_single_day_curve_has_one_point_per_bar():
    bars = _bars("2026-11-03")
    trades = _trades([(bars[1], 10, "Signal"), (bars[3], -4, "Square-off")])
    out = equity_curve(trades, CAPITAL, bars)
    assert out["time"].tolist() == list(bars)
    assert out["equity"].tolist() == [1000.0, 1010.0, 1010.0, 1006.0]


def test_curve_without_trades_is_flat_at_capital():
    out = equity_curve(_trades([]), CAPITAL, _bars("2026-11-03"))
    assert out["equity"].tolist() == [CAPITAL] * 4


def test_curve_without_bars_is_empty():
    out = equity_curve(_trades([]), CAPITAL, [])
    assert out.empty and list(out.columns) == ["time", "equity"]


def test_curve_starts_at_capital_before_the_first_exit():
    bars = _days("2026-11-02 09:15", "2026-11-03 09:15")
    out = equity_curve(_trades([("2026-11-03 10:00", 25, "Signal")]), CAPITAL, bars)
    assert out["equity"].tolist() == [1000.0, 1025.0]


# --- gradient_color ---

@pytest.mark.parametrize("final, color", [
    (1100.0, GREEN), (900.0, RED), (1000.0, AMBER),
    (1000.9, AMBER), (999.1, AMBER),  # within +/-0.1% of capital
    (1001.1, GREEN), (998.9, RED),
])
def test_gradient_color(final, color):
    assert gradient_color(CAPITAL, final) == color


# --- drawdown_curve ---

def _max_drawdown(trades):
    """The summary's definition (BacktestEngine._summary): capital first, then each trade's equity."""
    equity = pd.concat([pd.Series([CAPITAL]), trades["Cumulative Capital"]])
    return float(((equity - equity.cummax()) / equity.cummax() * 100).min())


def test_drawdown_deepest_point_matches_the_summary():
    trades = _trades([("2026-11-02 10:00", 100, "Target"), ("2026-11-03 10:00", -55, "Stop Loss"),
                      ("2026-11-04 10:00", -55, "Stop Loss"), ("2026-11-05 10:00", 80, "Signal")])
    bars = _days("2026-11-02 09:15", "2026-11-03 09:15", "2026-11-04 09:15", "2026-11-05 09:15")
    out = drawdown_curve(trades, CAPITAL, bars)
    assert out["drawdown"].min() == pytest.approx(_max_drawdown(trades))
    assert out["drawdown"].iloc[0] == 0.0  # new peak
    assert out["drawdown"].iloc[3] == pytest.approx((1070 - 1100) / 1100 * 100)  # still below the 1100 peak


def test_drawdown_is_zero_for_a_losing_start_only_after_a_peak():
    # Loss on the first trade: the starting capital is the peak, so drawdown shows at once.
    trades = _trades([("2026-11-02 10:00", -100, "Stop Loss")])
    out = drawdown_curve(trades, CAPITAL, _days("2026-11-02 09:15", "2026-11-03 09:15"))
    assert out["drawdown"].tolist() == pytest.approx([-10.0, -10.0])


def test_drawdown_without_trades_is_zero():
    out = drawdown_curve(_trades([]), CAPITAL, _bars("2026-11-03"))
    assert out["drawdown"].tolist() == [0.0] * 4
    assert list(out.columns) == ["time", "drawdown"]


def test_drawdown_shows_the_worst_trade_of_a_day_not_just_the_last():
    # Two exits on 3 Nov: equity falls to 900 then recovers to 1000. End-of-day equity is flat, the dip is not.
    trades = _trades([("2026-11-03 10:00", -100, "Stop Loss"), ("2026-11-03 14:00", 100, "Target")])
    bars = _days("2026-11-02 09:15", "2026-11-03 09:15", "2026-11-04 09:15")
    out = drawdown_curve(trades, CAPITAL, bars)
    assert out["drawdown"].tolist() == pytest.approx([0.0, -10.0, 0.0])
    assert out["drawdown"].min() == pytest.approx(_max_drawdown(trades))


def test_drawdown_matches_equity_axis():
    trades = _trades([("2026-11-03 10:00", 10, "Signal")])
    bars = _days("2026-11-02 09:15", "2026-11-03 09:15", "2026-11-04 09:15")
    assert drawdown_curve(trades, CAPITAL, bars)["time"].tolist() == equity_curve(trades, CAPITAL, bars)["time"].tolist()


# --- exit_reasons ---

def test_exit_reasons_counts_pnl_and_win_rate_most_frequent_first():
    trades = _trades([("2026-11-02 10:00", 50, "Target"), ("2026-11-02 11:00", -20, "Stop Loss"),
                      ("2026-11-02 12:00", -10, "Stop Loss"), ("2026-11-02 13:00", 5, "Stop Loss"),
                      ("2026-11-02 14:00", 8, "Signal")])
    out = exit_reasons(trades)
    assert list(out.columns) == ["Exit reason", "Trades", "Net P/L", "Win rate (%)"]
    assert out["Exit reason"].tolist() == ["Stop Loss", "Target", "Signal"]  # 3, then the 1-trade ties by net P/L
    stop = out.set_index("Exit reason").loc["Stop Loss"]
    assert (stop["Trades"], stop["Net P/L"]) == (3, -25.0)
    assert stop["Win rate (%)"] == pytest.approx(100 / 3)
    assert out["Trades"].sum() == len(trades)


def test_exit_reasons_empty():
    out = exit_reasons(_trades([]))
    assert out.empty and list(out.columns) == ["Exit reason", "Trades", "Net P/L", "Win rate (%)"]


def test_exit_reasons_ties_are_ordered_by_net_pnl():
    trades = _trades([("2026-11-02 10:00", -5, "Signal"), ("2026-11-02 11:00", 9, "Target")])
    assert exit_reasons(trades)["Exit reason"].tolist() == ["Target", "Signal"]


def test_numpy_floats_are_plain_numbers():
    out = exit_reasons(_trades([("2026-11-02 10:00", 1, "Signal")]))
    assert isinstance(out["Win rate (%)"].iloc[0], (float, np.floating))
