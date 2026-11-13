"""Indicators (Wilder / TradingView conventions): RSI, ADX with +DI/-DI, EMA, SMA."""
import numpy as np
import pandas as pd


def _smooth(values, n, alpha):
    """Recursive smoothing seeded with the SMA of the first n valid values."""
    v = np.asarray(values, dtype=float)
    out = np.full(len(v), np.nan)
    valid = np.flatnonzero(~np.isnan(v))
    if len(valid) < n:
        return out
    seed = valid[0] + n - 1
    out[seed] = v[valid[0]:seed + 1].mean()
    for i in range(seed + 1, len(v)):
        out[i] = out[i - 1] if np.isnan(v[i]) else alpha * v[i] + (1 - alpha) * out[i - 1]
    return out


def rma(values, n):
    return _smooth(values, n, 1.0 / n)


def ema(values, n):
    return _smooth(values, n, 2.0 / (n + 1))


def sma(values, n):
    return pd.Series(values, dtype=float).rolling(n).mean().to_numpy()


def rsi(close, n=14):
    c = np.asarray(close, dtype=float)
    delta = np.diff(c, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain[0] = loss[0] = np.nan
    ag, al = rma(gain, n), rma(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100 - 100 / (1 + ag / al)
    out = np.where(al == 0, 100.0, out)
    return np.where(np.isnan(ag), np.nan, out)


def adx(high, low, close, n=14):
    """Returns (+DI, -DI, ADX)."""
    h, l, c = (np.asarray(x, dtype=float) for x in (high, low, close))
    up = h - np.roll(h, 1)
    down = np.roll(l, 1) - l
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = np.maximum.reduce([h - l, np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))])
    plus_dm[0] = minus_dm[0] = tr[0] = np.nan
    atr = rma(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100 * rma(plus_dm, n) / atr
        minus_di = 100 * rma(minus_dm, n) / atr
        total = plus_di + minus_di
        dx = np.abs(plus_di - minus_di) / np.where(total == 0, 1, total)
    return plus_di, minus_di, rma(100 * dx, n)
