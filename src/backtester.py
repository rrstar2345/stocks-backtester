import math
from dataclasses import dataclass
from datetime import time as dtime
from typing import Optional

import numpy as np
import pandas as pd

from src.strategies import Strategy

RESOLUTIONS = ["1", "2", "3", "5", "10", "15", "20", "30", "60", "120", "240", "D"]
SIGNALS = ("long_entry", "long_exit", "short_entry", "short_exit")
# Columns kept per symbol for charting (indicator columns only when the strategy produced them).
CANDLE_COLS = ("timestamp", "open", "high", "low", "close")
INDICATOR_COLS = ("rsi", "rsi_ma")


def bar_minutes(resolution) -> Optional[int]:
    """Bar length in minutes, None for daily."""
    return None if str(resolution).upper() in ("D", "1D") else int(resolution)


def warmup_days(strategy: Strategy, params: dict, minutes: Optional[int]) -> int:
    """Calendar days of extra history so indicators are warm on the first bar of the range."""
    bars = strategy.warmup(params) if strategy.warmup else 50
    per_day = max(1, 375 // minutes) if minutes else 1
    days = int(math.ceil(bars / per_day) * 1.5) + 4
    return max(10, math.ceil(days / 10) * 10)  # bucketed: tweaking a length rarely changes the fetch range


@dataclass
class ExecConfig:
    direction: str = "long"            # "long" | "short" | "both"
    target: Optional[tuple] = None     # (kind, value) kind: "points" | "percent"
    stop: Optional[tuple] = None
    sizing: str = "capital"            # "capital": floor(capital / (price*lot)) lots | "fixed"
    lot_size: int = 1
    fixed_qty: int = 1
    bar_minutes: Optional[int] = None  # None = daily bars (no intraday window)
    square_off: bool = True            # flat by `square_off_at` every day (intraday only)
    window_start: dtime = dtime(9, 15)
    square_off_at: dtime = dtime(15, 29)
    brokerage: float = 20.0            # flat, per order (a round trip is 2 orders)
    govt_pct: float = 0.05             # % of sell-side turnover


def _level(entry, rule, side, is_target):
    """Absolute price for a target / stop rule, or None when switched off."""
    if not rule or not rule[1]:
        return None
    kind, value = rule
    dist = value if kind == "points" else entry * value / 100.0
    favourable = (side == 1) == is_target  # long target / short stop sit above entry
    return entry + dist if favourable else entry - dist


@dataclass
class BacktestResult:
    summary: dict
    trades: pd.DataFrame
    candles: dict                      # symbol -> slim candles (OHLC + chart indicators)
    default_symbol: str                # single instrument, or the underlying spot in chain mode


def _slim(df):
    """Only the columns the charts need, so session state stays small."""
    keep = [c for c in CANDLE_COLS + INDICATOR_COLS if c in df.columns]
    return df[keep].reset_index(drop=True)


def _pack(df, ts=None):
    """Arrays for the simulator. With `ts`, the candles are aligned to that timeline: bars that
    do not exist are flat at the previous close and flagged has=False (no signals, no entries)."""
    if ts is not None:
        df = df.set_index("timestamp").reindex(pd.DatetimeIndex(ts))
    has = df["close"].notna().to_numpy()
    close = df["close"].ffill()
    out = {k: df[k].where(has, close).to_numpy(float) for k in ("open", "high", "low")}
    out["close"], out["has"] = close.to_numpy(float), has
    for k in SIGNALS:
        out[k] = df[k].eq(True).to_numpy()
    return out


class BacktestEngine:
    """Signals are read on bar close and filled at the next bar's open. Targets and stops are
    checked against each bar's high/low (stop first when both are touched in the same bar;
    gaps fill at the open)."""

    def __init__(self, initial_capital=100000.0):
        self.initial_capital = initial_capital

    # -- single instrument
    def run(self, df, strategy: Strategy, params: dict, cfg: ExecConfig, symbol=""):
        df = strategy.compute(df.reset_index(drop=True), **params)
        series = {symbol: _pack(df)}
        trades, skipped = self._simulate(df["timestamp"], series, lambda i: [symbol], cfg)
        return BacktestResult(self._summary(trades, skipped), trades, {symbol: _slim(df)}, symbol)

    # -- rolling option chain: the contract is re-selected on every bar from the underlying's LTP
    def run_chain(self, chain, strategy: Strategy, params: dict, cfg: ExecConfig):
        ts = chain.underlying["timestamp"]
        computed = {sym: strategy.compute(df.reset_index(drop=True), **params)
                    for sym, df in chain.contracts.items()}
        series = {sym: _pack(df, ts) for sym, df in computed.items()}
        sels = list(chain.selection.values())

        def candidates(i):
            return [s[i] for s in sels if s[i] in series]

        trades, skipped = self._simulate(ts, series, candidates, cfg)

        # The default chart shows the underlying, so its indicators use the same parameters
        # (they are not what traded; the contracts' own indicators are).
        spot = chain.info.get("Spot symbol", "underlying")
        candles = {spot: _slim(strategy.compute(chain.underlying.reset_index(drop=True), **params))}
        candles.update({sym: _slim(df) for sym, df in computed.items()})
        return BacktestResult(self._summary(trades, skipped), trades, candles, spot)

    def _simulate(self, ts, S, candidates, cfg):
        n = len(ts)
        intraday = cfg.bar_minutes is not None
        sq = cfg.square_off and intraday
        day = ts.dt.normalize().to_numpy()
        close_min = (ts.dt.hour * 60 + ts.dt.minute + (cfg.bar_minutes or 0)).to_numpy()
        win_start = cfg.window_start.hour * 60 + cfg.window_start.minute
        sq_min = cfg.square_off_at.hour * 60 + cfg.square_off_at.minute
        can_long, can_short = cfg.direction in ("long", "both"), cfg.direction in ("short", "both")

        rows, skipped = [], 0
        pos = entry_i = qty = 0
        cur = None
        entry_px = stop_px = target_px = 0.0
        pending, pending_exit = None, False

        def close(i, price, reason):
            nonlocal pos
            gross = (price - entry_px) * qty * pos
            brokerage = cfg.brokerage * 2
            govt = (price if pos == 1 else entry_px) * qty * cfg.govt_pct / 100.0
            net = gross - brokerage - govt
            rows.append({
                "Symbol": cur, "Entry Time": ts[entry_i], "Exit Time": ts[i],
                "Type": "LONG" if pos == 1 else "SHORT", "Entry Price": entry_px,
                "Exit Price": price, "Qty": qty, "Gross PnL": gross, "Brokerage": brokerage,
                "Govt Charges": govt, "Total Charges": brokerage + govt, "Net PnL": net,
                "Return (%)": net / (entry_px * qty) * 100, "Exit Reason": reason,
            })
            pos = 0

        for i in range(n):
            # 1. fills at this bar's open, from signals on the previous bar's close
            if pos and pending_exit:
                close(i, S[cur]["open"][i], "Signal")
            pending_exit = False
            if pos == 0 and pending and not (sq and day[i] != day[i - 1]):
                key, side = pending
                a = S[key]
                price = a["open"][i]
                if a["has"][i] and not np.isnan(price):
                    q = (cfg.fixed_qty if cfg.sizing == "fixed"
                         else int(self.initial_capital // (price * cfg.lot_size)) * cfg.lot_size)
                    if q < 1:
                        skipped += 1
                    else:
                        pos, cur, qty, entry_px, entry_i = side, key, q, price, i
                        stop_px = _level(price, cfg.stop, pos, False)
                        target_px = _level(price, cfg.target, pos, True)
            pending = None

            # 2. stop / target inside this bar
            if pos:
                a = S[cur]
                o, h, l = a["open"][i], a["high"][i], a["low"][i]
                if pos == 1:
                    if stop_px is not None and l <= stop_px:
                        close(i, min(o, stop_px), "Stop Loss")
                    elif target_px is not None and h >= target_px:
                        close(i, max(o, target_px), "Target")
                else:
                    if stop_px is not None and h >= stop_px:
                        close(i, max(o, stop_px), "Stop Loss")
                    elif target_px is not None and l <= target_px:
                        close(i, min(o, target_px), "Target")

            # 3. intraday square-off (first bar closing at/after the time, or last bar of the day)
            if pos and sq and (close_min[i] >= sq_min or i == n - 1 or day[i + 1] != day[i]):
                close(i, S[cur]["close"][i], "Square-off")

            # 4. signals on this bar's close
            if pos:
                a = S[cur]
                if a["long_exit"][i] if pos == 1 else a["short_exit"][i]:
                    pending_exit = True
            if (pos == 0 or pending_exit) and (not sq or win_start <= close_min[i] < sq_min):
                for key in candidates(i):
                    if can_long and S[key]["long_entry"][i]:
                        pending = (key, 1)
                        break
                    if can_short and S[key]["short_entry"][i]:
                        pending = (key, -1)
                        break

        if pos:
            close(n - 1, S[cur]["close"][n - 1], "End of data")

        trades = pd.DataFrame(rows)
        if not trades.empty:
            trades.insert(0, "Trade #", np.arange(1, len(trades) + 1))  # rows are in exit order
            trades["Cumulative Capital"] = self.initial_capital + trades["Net PnL"].cumsum()
        return trades, skipped

    def _summary(self, t, skipped):
        total = len(t)
        col = lambda c: float(t[c].sum()) if total else 0.0
        wins = t.loc[t["Net PnL"] > 0, "Net PnL"].sum() if total else 0.0
        losses = -t.loc[t["Net PnL"] < 0, "Net PnL"].sum() if total else 0.0
        max_dd = 0.0
        if total:
            equity = pd.concat([pd.Series([self.initial_capital]), t["Cumulative Capital"]])
            max_dd = float(((equity - equity.cummax()) / equity.cummax() * 100).min())
        net = col("Net PnL")
        return {
            "Initial Capital": self.initial_capital,
            "Final Capital": self.initial_capital + net,
            "Gross Profit / Loss": col("Gross PnL"),
            "Brokerage": col("Brokerage"),
            "Govt Charges": col("Govt Charges"),
            "Total Charges": col("Total Charges"),
            "Net Profit / Loss": net,
            "Total Trades": total,
            "Win Rate (%)": (t["Net PnL"] > 0).mean() * 100 if total else 0.0,
            "Profit Factor": (wins / losses) if losses > 0 else (float("inf") if wins > 0 else 0.0),
            "Max Drawdown (%)": max_dd,
            "Skipped (insufficient capital)": skipped,
        }
