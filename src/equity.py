"""Equity curve series and colour rule (pure functions, no Streamlit)."""
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


def gradient_color(capital: float, final: float) -> str:
    """Green above the flat band, red below it, amber inside it."""
    net = final - capital
    if abs(net) <= FLAT_BAND * capital:
        return AMBER
    return GREEN if net > 0 else RED
