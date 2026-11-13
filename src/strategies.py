"""Strategy registry. Each strategy declares its parameters (rendered by the UI) and a
`compute(df, **params)` that adds indicator columns plus four boolean signal columns:
long_entry, long_exit, short_entry, short_exit (evaluated on bar close)."""
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from src import indicators as ta


@dataclass
class Param:
    key: str
    label: str
    kind: str  # "int" | "float" | "choice"
    default: object
    min: float = None
    max: float = None
    options: list = field(default_factory=list)


@dataclass
class Strategy:
    name: str
    params: list
    compute: Callable
    warmup: Callable = None  # params dict -> bars of history the indicators need


def rsi_adx(df, rsi_length=14, rsi_ma_type="EMA", rsi_ma_length=9, adx_length=14,
            adx_threshold=25.0):
    """RSI crossing its own smoothing line, filtered by ADX / DI trend.

    long entry : RSI crosses above its smoothing line AND +DI > -DI AND ADX > threshold
    long exit  : RSI crosses below its smoothing line
    short entry/exit: mirror image (-DI > +DI, cross below / cross above)
    """
    out = df.copy()
    rsi = ta.rsi(out["close"].to_numpy(), rsi_length)
    smooth = (ta.ema if rsi_ma_type == "EMA" else ta.sma)(rsi, rsi_ma_length)
    plus_di, minus_di, adx = ta.adx(out["high"], out["low"], out["close"], adx_length)
    out["rsi"], out["rsi_ma"] = rsi, smooth
    out["plus_di"], out["minus_di"], out["adx"] = plus_di, minus_di, adx

    r, m = pd.Series(rsi), pd.Series(smooth)
    cross_up = ((r > m) & (r.shift(1) <= m.shift(1))).to_numpy()
    cross_down = ((r < m) & (r.shift(1) >= m.shift(1))).to_numpy()
    strong = adx > adx_threshold  # NaN compares False
    out["long_entry"] = cross_up & (plus_di > minus_di) & strong
    out["long_exit"] = cross_down
    out["short_entry"] = cross_down & (minus_di > plus_di) & strong
    out["short_exit"] = cross_up
    return out


STRATEGIES = {
    "RSI cross + ADX trend": Strategy(
        name="RSI cross + ADX trend",
        params=[
            Param("rsi_length", "RSI length", "int", 14, 2, 100),
            Param("rsi_ma_type", "RSI smoothing type", "choice", "EMA", options=["EMA", "SMA"]),
            Param("rsi_ma_length", "RSI smoothing length", "int", 9, 2, 100),
            Param("adx_length", "ADX length", "int", 14, 2, 100),
            Param("adx_threshold", "ADX threshold", "float", 25.0, 0.0, 100.0),
        ],
        compute=rsi_adx,
        warmup=lambda p: max(p["rsi_length"] + p["rsi_ma_length"], 2 * p["adx_length"]) + 10,
    ),
}
