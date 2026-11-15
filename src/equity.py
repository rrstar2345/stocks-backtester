"""Equity curve, drawdown and exit-reason series, and the colour rule (pure functions, no Streamlit)."""
import numpy as np
import pandas as pd

GREEN, RED, AMBER = "#16a34a", "#dc2626", "#f59e0b"
FLAT_BAND = 0.001  # net within +/-0.1% of capital counts as flat


def equity_curve(trades: pd.DataFrame, capital: float, timestamps) -> pd.DataFrame:
    """Cumulative equity (net of charges) on a regular time axis.

    More than one trading day: one point per day (end-of-day value, indexed by the day).
    A single day: one point per bar. `timestamps` are the candle times of the tested range.
    Returns columns `time` and `equity`.
    """
    ts = pd.Series(pd.to_datetime(timestamps)).dropna().sort_values().reset_index(drop=True)
    if ts.empty:
        return pd.DataFrame({"time": pd.to_datetime([]), "equity": []})

    days = ts.dt.normalize().drop_duplicates()
    daily = len(days) > 1
    grid = pd.DataFrame({"time": days.to_numpy() if daily else ts.to_numpy()})
    if trades.empty:
        grid["equity"] = float(capital)
        return grid

    exits = pd.DataFrame({
        "time": pd.to_datetime(trades["Exit Time"]).dt.normalize() if daily else pd.to_datetime(trades["Exit Time"]),
        "equity": trades["Cumulative Capital"].to_numpy(float),
    }).sort_values("time", kind="stable").drop_duplicates("time", keep="last")
    out = pd.merge_asof(grid, exits, on="time", direction="backward")
    out["equity"] = out["equity"].fillna(float(capital))
    return out


def drawdown_curve(trades: pd.DataFrame, capital: float, timestamps) -> pd.DataFrame:
    """Drawdown (%, zero or negative) from the running equity peak, on the same time axis as `equity_curve`.

    Worked out per trade, with the starting capital as the first peak, so the deepest point equals the
    summary's max drawdown. A day (or bar) with several exits shows its worst value; later points carry
    the last one. Returns columns `time` and `drawdown`.
    """
    grid = equity_curve(trades, capital, timestamps)[["time"]]
    if grid.empty or trades.empty:
        return grid.assign(drawdown=0.0)

    daily = grid["time"].dt.normalize().nunique() > 1  # same rule as equity_curve
    equity = trades["Cumulative Capital"].to_numpy(float)  # rows are in exit order
    peak = np.maximum.accumulate(np.maximum(equity, float(capital)))
    exits = pd.DataFrame({
        "time": pd.to_datetime(trades["Exit Time"]).dt.normalize() if daily else pd.to_datetime(trades["Exit Time"]),
        "dd": (equity - peak) / peak * 100.0,
    })
    per_time = exits.groupby("time").agg(last=("dd", "last"), worst=("dd", "min")).reset_index()
    out = pd.merge_asof(grid, per_time[["time", "last"]], on="time", direction="backward")
    out["drawdown"] = out["time"].map(per_time.set_index("time")["worst"]).fillna(out["last"]).fillna(0.0)
    return out[["time", "drawdown"]]


def exit_reasons(trades: pd.DataFrame) -> pd.DataFrame:
    """Trades, net P/L and win rate (%, net of charges) per exit reason, most frequent first."""
    cols = ["Exit reason", "Trades", "Net P/L", "Win rate (%)"]
    if trades.empty:
        return pd.DataFrame(columns=cols)
    g = trades.groupby("Exit Reason")
    out = pd.DataFrame({
        "Trades": g.size(),
        "Net P/L": g["Net PnL"].sum(),
        "Win rate (%)": g["Net PnL"].apply(lambda s: float((s > 0).mean() * 100)),
    })
    out = out.sort_values(["Trades", "Net P/L"], ascending=False).rename_axis("Exit reason").reset_index()
    return out[cols]


def gradient_color(capital: float, final: float) -> str:
    """Green above the flat band, red below it, amber inside it."""
    net = final - capital
    if abs(net) <= FLAT_BAND * capital:
        return AMBER
    return GREEN if net > 0 else RED
