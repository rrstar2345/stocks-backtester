"""Payload for the price chart component (pure functions, no Streamlit).

The chart uses the bar index as its time axis, so overnight and holiday gaps do not show
as flat stretches; `t` holds one label per bar for the axis and the crosshair.
"""
import numpy as np
import pandas as pd


def _strike_text(strike: float) -> str:
    return str(int(strike)) if float(strike) == int(strike) else str(strike)


def contract_parts(symbol: str, strikes: dict | None) -> tuple[float, str] | None:
    """(strike, 'CE'|'PE') of an option ticker, found by matching the ticker's end against the strikes the
    run fetched (`{"CE": [24900.0, ...], "PE": [...]}`). Weekly tickers have the expiry day and the strike
    run together (NSE:NIFTY26N1724000CE), so the strike cannot be read from the text alone; the longest
    matching strike wins."""
    for side in ("CE", "PE"):
        if symbol.endswith(side):
            hits = [k for k in (strikes or {}).get(side, []) if symbol.endswith(_strike_text(k) + side)]
            if hits:
                return max(hits, key=lambda k: len(_strike_text(k))), side
    return None


def symbol_options(symbols, default_symbol: str, strikes: dict | None = None) -> list[str]:
    """Dropdown order: the default instrument first, then CE contracts by strike, then PE contracts
    by strike, then anything that is not a recognised option contract."""
    rest = [s for s in symbols if s != default_symbol]
    parts = {s: contract_parts(s, strikes) for s in rest}
    contracts = sorted((s for s in rest if parts[s]), key=lambda s: (parts[s][1] != "CE", parts[s][0], s))
    other = sorted(s for s in rest if not parts[s])
    return [default_symbol, *contracts, *other]


def symbol_label(symbol: str, default_symbol: str, strikes: dict | None = None) -> str:
    """`24900 CE` for contracts; the underlying (or single instrument) keeps its Fyers symbol."""
    parts = contract_parts(symbol, strikes) if symbol != default_symbol else None
    return f"{parts[0]:g} {parts[1]}" if parts else symbol


def bar_labels(timestamps, daily: bool) -> list[str]:
    fmt = "%Y-%m-%d" if daily else "%Y-%m-%d %H:%M"
    return pd.DatetimeIndex(timestamps).strftime(fmt).tolist()


def _bar_index(bar_times: np.ndarray, when) -> int:
    """Index of the bar that contains `when` (the last bar starting at or before it)."""
    return int(np.searchsorted(bar_times, pd.Timestamp(when).to_datetime64(), side="right")) - 1


def _marker(i, entry: bool, side_long: bool, tone: str, text: str, trade: int) -> dict:
    # Long: entry arrow below the bar pointing up, exit above pointing down. Short is mirrored.
    below = side_long if entry else not side_long
    return {"i": i, "pos": "belowBar" if below else "aboveBar",
            "shape": "arrowUp" if below else "arrowDown", "tone": tone, "text": text, "trade": trade}


def trade_markers(trades: pd.DataFrame, bar_times: np.ndarray) -> tuple[list[dict], list[dict]]:
    """Entry and exit markers plus the tooltip rows, one per trade, for the trades given."""
    markers, tips = [], []
    for r in trades.to_dict("records"):
        n, qty, net = int(r["Trade #"]), int(r["Qty"]), float(r["Net PnL"])
        long = r["Type"] == "LONG"
        i_in, i_out = _bar_index(bar_times, r["Entry Time"]), _bar_index(bar_times, r["Exit Time"])
        if i_out < 0:
            continue  # trade closed before the charted range
        i_in = max(i_in, 0)
        tone = "pos" if net > 0 else "neg" if net < 0 else "flat"
        markers.append(_marker(i_in, True, long, "entry", f"#{n} · {qty}", len(tips)))
        markers.append(_marker(i_out, False, long, tone, f"#{n} · {net:+,.0f}", len(tips)))
        tips.append({"n": n, "rows": [
            ["Trade", f"#{n} · {r['Type']}"],
            ["Symbol", str(r["Symbol"])],
            ["Entry", f"{pd.Timestamp(r['Entry Time']):%d-%b %H:%M} @ {float(r['Entry Price']):,.2f}"],
            ["Exit", f"{pd.Timestamp(r['Exit Time']):%d-%b %H:%M} @ {float(r['Exit Price']):,.2f}"],
            ["Quantity", f"{qty:,}"],
            ["Gross", f"{float(r['Gross PnL']):+,.2f}"],
            ["Charges", f"{float(r['Total Charges']):,.2f}"],
            ["Net", f"{net:+,.2f} ({float(r['Return (%)']):+.2f}%)"],
            ["Exit reason", str(r["Exit Reason"])],
        ], "tone": tone})
    markers.sort(key=lambda m: m["i"])
    return markers, tips


def build_payload(candles: pd.DataFrame, trades: pd.DataFrame, symbol: str, default_symbol: str,
                  start, resolution) -> dict:
    """Candles of `symbol` from `start` on, with the trades that belong on this chart: every trade
    on the default instrument (chain mode: all contracts, drawn on the underlying), otherwise only
    the trades of that contract."""
    df = candles[candles["timestamp"] >= pd.Timestamp(start)].reset_index(drop=True)
    daily = str(resolution).upper() in ("D", "1D")
    shown = trades if symbol == default_symbol else trades[trades["Symbol"] == symbol]
    markers, tips = ([], []) if df.empty or shown.empty else trade_markers(shown, df["timestamp"].to_numpy())
    payload = {
        "symbol": symbol,
        "t": bar_labels(df["timestamp"], daily),
        "o": df["open"].round(4).tolist(), "h": df["high"].round(4).tolist(),
        "l": df["low"].round(4).tolist(), "c": df["close"].round(4).tolist(),
        "markers": markers, "tips": tips,
    }
    # RSI pane: only when the strategy produced it. NaN (no value yet) becomes null.
    if "rsi" in df.columns:
        for key, col in (("rsi", "rsi"), ("rsi_ma", "rsi_ma")):
            if col in df.columns:
                payload[key] = [None if pd.isna(v) else round(float(v), 2) for v in df[col]]
    return payload
