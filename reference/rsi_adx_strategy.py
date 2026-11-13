"""
Long options (F&O), RSI cross + ADX/DMI trend, on the option premium.

Buys one CE and/or PE of an index (NIFTY, BANKNIFTY, SENSEX ...) and holds one
position at a time. Everything is read from the option's own premium candles:

  entry : RSI crosses over its smoothing line  AND  +DI > -DI  AND  ADX > 25
  exit  : RSI crosses under its smoothing line, OR premium hits TARGET,
          OR premium hits STOP_LOSS, OR the trading window ends

Lots = floor(CAPITAL / (premium x lot size)). The strike is picked from the
live option chain (ATM, ATM+1 ... by strike), the expiry from OpenAlgo's expiry
list, and the entry is skipped when the option's |delta| is outside DELTA_RANGE.

Host it at /python: Add Strategy, choose this file, exchange NSE (BSE for
SENSEX / BANKEX). Analyzer mode decides whether orders go to the Sandbox or to
your broker. Built with the python-strategy skill.
"""

import logging
import math
import os
import re
import time as _time
from dataclasses import dataclass
from datetime import time as dtime
from datetime import timedelta

import pandas as pd
from openalgo import ta

# -----------------------------------------------------------------------------
# OpenAlgo connection. /python fills these in by itself, so leave them as they
# are there. To run the file somewhere else, put your own values in.
# -----------------------------------------------------------------------------

API_KEY = os.getenv("OPENALGO_API_KEY", "")
HOST = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
WEBSOCKET_URL = os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765")

# -----------------------------------------------------------------------------
# Strategy settings: change them here, before you upload.
# -----------------------------------------------------------------------------

STRATEGY_NAME = "NIFTY - Options Long RSI ADX"
UNDERLYING = "NIFTY"  # NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, SENSEX, BANKEX
CAPITAL = 100000  # INR; lots are sized from this
SIDE = "both"  # "call", "put" or "both"
EXPIRY = "current"  # "current", "next", "next+1", "next+2", "next+3"
STRIKE = "ATM"  # "ATM", "ATM+1", "ATM+2" ... "ATM-1", "ATM-5": steps of strike, up is +
DELTA_RANGE = (0.30, 0.70)  # (from, to) on |delta|; entry skipped outside it. None = off
INTERVAL = "5m"  # 1m 3m 5m 10m 15m 20m 25m 30m 45m 1h

RSI_LENGTH = 14
RSI_MA_TYPE = "EMA"  # "EMA" or "SMA"
RSI_MA_LENGTH = 9
ADX_LENGTH = 14
ADX_THRESHOLD = 25

# Trading window, IST: entries inside it; any open position is closed at its end.
TIME_RANGE = {"NSE": "0915-1545", "BSE": "0915-1545"}

# int = rupees of premium (30 -> +30 points), float = percent of entry (30.0 -> +30%).
# None switches the rule off.
TARGET = 30
STOP_LOSS = 25

PRODUCT = "MIS"  # MIS intraday, NRML to carry
CLOSE_ON_STOP = False  # True: Stop at /python also closes the open position

NO_BACKTEST_REASON = (
    "Not backtestable here: the strategy picks a different option contract by strike, "
    "expiry and delta as the market moves, and historical option chains and Greeks are "
    "not available. Use rehearse.py and the Sandbox (analyzer mode) instead."
)

# -----------------------------------------------------------------------------
# Derived from the settings above. Not settings.
# -----------------------------------------------------------------------------

INDEX_EXCHANGES = {
    "NIFTY": "NSE_INDEX",
    "BANKNIFTY": "NSE_INDEX",
    "FINNIFTY": "NSE_INDEX",
    "MIDCPNIFTY": "NSE_INDEX",
    "NIFTYNXT50": "NSE_INDEX",
    "SENSEX": "BSE_INDEX",
    "BANKEX": "BSE_INDEX",
}
EXCHANGE = INDEX_EXCHANGES.get(str(UNDERLYING).upper(), "NSE_INDEX")
# Interval -> (history interval the broker serves, bar length in minutes).
# 20m, 25m and 45m are built here from finer bars, anchored at the 09:15 open.
INTERVALS = {
    "1m": ("1m", 1),
    "3m": ("3m", 3),
    "5m": ("5m", 5),
    "10m": ("10m", 10),
    "15m": ("15m", 15),
    "20m": ("5m", 20),
    "25m": ("5m", 25),
    "30m": ("30m", 30),
    "45m": ("15m", 45),
    "1h": ("1h", 60),
}
OPTION_EXCHANGES = {"NSE_INDEX": "NFO", "BSE_INDEX": "BFO"}
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
BAR_DELAY_SECONDS = 2.0  # wait this long after a bar closes before reading it


@dataclass
class Settings:
    interval: str
    base: str
    base_minutes: int
    minutes: int
    sides: list
    expiry_index: int
    strike_offset: int
    delta_low: float | None
    delta_high: float | None
    ma_type: str
    window_start: dtime
    window_end: dtime
    exit_at: dtime
    option_exchange: str
    calendar: str
    target: tuple
    stop: tuple


@dataclass
class Trade:
    symbol: str
    quantity: int
    entry: float
    risk: object
    retry_exit_at: float = 0.0


def _rule(value, label):
    """TARGET / STOP_LOSS -> (percent, points). int is points, float is percent."""
    if value is None:
        return (None, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SystemExit(f"{label} must be a positive int (points), a positive float (%), or None.")
    if isinstance(value, int):
        return (None, float(value))
    if label == "STOP_LOSS" and value >= 100:
        raise SystemExit("STOP_LOSS as a percentage must be below 100.")
    return (float(value), None)


def _hhmm(text):
    digits = str(text).replace(":", "").strip()
    return dtime(int(digits[:2]), int(digits[2:4]))


def read_settings():
    interval = str(INTERVAL).strip().lower()
    if interval not in INTERVALS:
        raise SystemExit(f"INTERVAL {INTERVAL!r} is not one of {', '.join(INTERVALS)}.")
    base, minutes = INTERVALS[interval]
    base_minutes = int(re.sub(r"\D", "", base)) * (60 if base.endswith("h") else 1)

    side = str(SIDE).strip().lower()
    sides = {"call": ["CE"], "ce": ["CE"], "put": ["PE"], "pe": ["PE"], "both": ["CE", "PE"]}.get(
        side
    )
    if not sides:
        raise SystemExit('SIDE must be "call", "put" or "both".')

    found = re.fullmatch(r"(current|next)\s*(?:\+\s*(\d+))?", str(EXPIRY).strip().lower())
    if not found or (found.group(1) == "current" and found.group(2)):
        raise SystemExit('EXPIRY must be "current", "next", "next+1", "next+2" or "next+3".')
    expiry_index = 0 if found.group(1) == "current" else 1 + int(found.group(2) or 0)

    found = re.fullmatch(r"ATM\s*(?:([+-])\s*(\d+))?", str(STRIKE).strip().upper())
    if not found:
        raise SystemExit('STRIKE must look like "ATM", "ATM+2" or "ATM-5".')
    strike_offset = int(found.group(2) or 0) * (-1 if found.group(1) == "-" else 1)

    low = high = None
    if DELTA_RANGE is not None:
        low, high = float(DELTA_RANGE[0]), float(DELTA_RANGE[1])
        if not 0 <= low <= high <= 1:
            raise SystemExit("DELTA_RANGE must be (from, to) with 0 <= from <= to <= 1.")

    ma_type = str(RSI_MA_TYPE).strip().upper()
    if ma_type not in ("EMA", "SMA"):
        raise SystemExit('RSI_MA_TYPE must be "EMA" or "SMA".')

    underlying = str(UNDERLYING).upper()
    if underlying not in INDEX_EXCHANGES:
        raise SystemExit(f"UNDERLYING {UNDERLYING!r} is not one of {', '.join(INDEX_EXCHANGES)}.")
    calendar = "BSE" if EXCHANGE == "BSE_INDEX" else "NSE"
    window = str(TIME_RANGE.get(calendar, "")).split("-")
    if len(window) != 2:
        raise SystemExit(f'TIME_RANGE needs an entry for {calendar}, like "0915-1545".')
    start, end = _hhmm(window[0]), _hhmm(window[1])
    # Orders after the close are rejected, so the exit cannot wait for a later end.
    exit_at = min(end, dtime(MARKET_CLOSE.hour, MARKET_CLOSE.minute - 1))
    return Settings(
        interval=interval,
        base=base,
        base_minutes=base_minutes,
        minutes=minutes,
        sides=sides,
        expiry_index=expiry_index,
        strike_offset=strike_offset,
        delta_low=low,
        delta_high=high,
        ma_type=ma_type,
        window_start=start,
        window_end=end,
        exit_at=exit_at,
        option_exchange=OPTION_EXCHANGES[EXCHANGE],
        calendar=calendar,
        target=_rule(TARGET, "TARGET"),
        stop=_rule(STOP_LOSS, "STOP_LOSS"),
    )


# -----------------------------------------------------------------------------
# Bars and indicators
# -----------------------------------------------------------------------------


def _to_ist(index):
    return index.tz_localize(IST) if index.tz is None else index.tz_convert(IST)


def _session_end(index):
    return index.normalize() + timedelta(hours=MARKET_CLOSE.hour, minutes=MARKET_CLOSE.minute)


def option_bars(client, symbol, exchange, s, now):
    """Closed bars of `s.interval` for an option, oldest first, IST index. A bar
    counts only when every finer bar inside it has closed. None when no data."""
    need = max(RSI_LENGTH + RSI_MA_LENGTH, 2 * ADX_LENGTH) + 60
    per_day = max(1, 375 // s.minutes)
    days = math.ceil(need / per_day) + 6
    raw = client.history(
        symbol=symbol,
        exchange=exchange,
        interval=s.base,
        start_date=(now - timedelta(days=days)).strftime("%Y-%m-%d"),
        end_date=now.strftime("%Y-%m-%d"),
        source="api",
    )
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        say_now_and_then(
            logging.WARNING, "hist" + symbol, "No history for %s: %s", symbol, why(raw)
        )
        return None
    df = raw.copy()
    df.index = _to_ist(df.index)
    ends = df.index + timedelta(minutes=s.base_minutes)
    ends = ends.where(ends <= _session_end(df.index), _session_end(df.index))
    df = df[ends <= now]
    if df.empty:
        return None
    last_base_end = min(
        df.index[-1] + timedelta(minutes=s.base_minutes), _session_end(df.index[-1:])[0]
    )
    if s.minutes != s.base_minutes:
        agg = {
            col: how
            for col, how in (
                ("open", "first"),
                ("high", "max"),
                ("low", "min"),
                ("close", "last"),
                ("volume", "sum"),
            )
            if col in df.columns
        }
        parts = []
        for day, part in df.groupby(df.index.normalize()):
            origin = day + timedelta(hours=MARKET_OPEN.hour, minutes=MARKET_OPEN.minute)
            parts.append(
                part.resample(f"{s.minutes}min", origin=origin, label="left", closed="left")
                .agg(agg)
                .dropna(subset=["open"])
            )
        df = pd.concat(parts)
    bar_ends = df.index + timedelta(minutes=s.minutes)
    bar_ends = bar_ends.where(bar_ends <= _session_end(df.index), _session_end(df.index))
    df = df[bar_ends <= last_base_end]
    return df if len(df) else None


def analyse(bars, s):
    """Indicator values on the last closed bar, or None while there are too few bars."""
    close = bars["close"].astype(float)
    rsi = pd.Series(ta.rsi(close.values, RSI_LENGTH), index=bars.index)
    smooth_fn = ta.ema if s.ma_type == "EMA" else ta.sma
    smooth = pd.Series(smooth_fn(rsi.values, RSI_MA_LENGTH), index=bars.index)
    plus_di, minus_di, adx = ta.adx(
        bars["high"].astype(float).values,
        bars["low"].astype(float).values,
        close.values,
        ADX_LENGTH,
    )
    plus_di = pd.Series(plus_di, index=bars.index)
    minus_di = pd.Series(minus_di, index=bars.index)
    adx = pd.Series(adx, index=bars.index)
    cross_up = (rsi > smooth) & (rsi.shift(1) <= smooth.shift(1))
    cross_down = (rsi < smooth) & (rsi.shift(1) >= smooth.shift(1))
    uptrend = (plus_di > minus_di) & (adx > ADX_THRESHOLD)
    last = bars.index[-1]
    values = (rsi[last], smooth[last], plus_di[last], minus_di[last], adx[last])
    if any(pd.isna(v) for v in values) or len(bars) < 3:
        return None
    return {
        "bar": last,
        "rsi": float(values[0]),
        "smooth": float(values[1]),
        "plus_di": float(values[2]),
        "minus_di": float(values[3]),
        "adx": float(values[4]),
        "cross_up": bool(cross_up[last]),
        "cross_down": bool(cross_down[last]),
        "uptrend": bool(uptrend[last]),
    }


def describe(a):
    return (
        f"RSI {a['rsi']:.1f} vs {a['smooth']:.1f} | +DI {a['plus_di']:.1f} "
        f"-DI {a['minus_di']:.1f} ADX {a['adx']:.1f} | cross-up {a['cross_up']} "
        f"cross-down {a['cross_down']} uptrend {a['uptrend']}"
    )


# -----------------------------------------------------------------------------
# Contract selection
# -----------------------------------------------------------------------------


def expiry_code(client, s, cache):
    """DDMMMYY of the chosen expiry; asked for once a day."""
    today = now_ist().date()
    if cache.get("day") != today:
        dates = expiry_dates(client, str(UNDERLYING).upper(), s.option_exchange, "options")
        if len(dates) <= s.expiry_index:
            raise SystemExit(
                f"EXPIRY {EXPIRY!r} needs {s.expiry_index + 1} upcoming expiries for "
                f"{UNDERLYING}; OpenAlgo lists {len(dates)}."
            )
        cache["day"] = today
        cache["code"] = dates[s.expiry_index].strftime("%d%b%y").upper()
        log.info("Expiry %s is %s", EXPIRY, cache["code"])
    return cache["code"]


def option_chain(client, s, code):
    reply = client.optionchain(
        underlying=str(UNDERLYING).upper(),
        exchange=EXCHANGE,
        expiry_date=code,
        strike_count=abs(s.strike_offset) + 3,
        with_greeks=True,
    )
    if not ok(reply) or not reply.get("chain"):
        say_now_and_then(logging.WARNING, "chain", "Option chain not available: %s", why(reply))
        return None
    return reply


def pick_leg(chain, s, kind):
    """The CE or PE leg at ATM + offset strikes (strike steps, up is +), or None."""
    rows = sorted(chain["chain"], key=lambda r: r["strike"])
    strikes = [float(r["strike"]) for r in rows]
    if len(strikes) < 2:
        return None
    gaps = sorted(b - a for a, b in zip(strikes, strikes[1:], strict=False))
    step = gaps[len(gaps) // 2]
    wanted = float(chain["atm_strike"]) + s.strike_offset * step
    for row in rows:
        if abs(float(row["strike"]) - wanted) < step / 2:
            leg = row.get("ce" if kind == "CE" else "pe") or {}
            if leg.get("symbol") and leg.get("ltp"):
                return {**leg, "strike": float(row["strike"]), "atm": float(chain["atm_strike"])}
            return None
    say_now_and_then(logging.WARNING, "strike", "Strike %.0f is not in the option chain.", wanted)
    return None


# -----------------------------------------------------------------------------
# The strategy
# -----------------------------------------------------------------------------


def latest_boundary(now, minutes):
    """The most recent bar close at or before `now`, on the 09:15 grid. None before the first."""
    open_at = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0)
    if now < open_at:
        return None
    k = int((now - open_at).total_seconds() // (minutes * 60))
    return open_at + timedelta(minutes=k * minutes) if k > 0 else None


def run_live():
    s = read_settings()
    name = strategy_name()
    underlying = str(UNDERLYING).upper()
    client = connect()
    check_connection(client)
    check_calendar(EXCHANGE)
    if s.window_end > MARKET_CLOSE:
        log.warning(
            "TIME_RANGE ends %s, after the %s close. Positions are closed at %s instead.",
            s.window_end.strftime("%H:%M"),
            MARKET_CLOSE.strftime("%H:%M"),
            s.exit_at.strftime("%H:%M"),
        )
    log.info(
        "Starting %s: %s %s options, %s bars, capital %s, expiry %s, strike %s, product %s",
        name,
        underlying,
        "+".join(s.sides),
        s.interval,
        CAPITAL,
        EXPIRY,
        STRIKE,
        PRODUCT,
    )
    prices = Prices(client, [(EXCHANGE, underlying)])
    prices.start()
    state = {"trade": None, "realized": 0.0}
    cache = {}
    option_pattern = re.compile(rf"^{underlying}\d{{2}}[A-Z]{{3}}\d{{2}}\d+(CE|PE)$")

    def adopt_open_position():
        book = client.positionbook()
        for row in (book.get("data") or []) if ok(book) else []:
            symbol = str(row.get("symbol", "")).upper()
            quantity = int(float(row.get("quantity") or 0))
            if (
                quantity > 0
                and option_pattern.match(symbol)
                and str(row.get("exchange")).upper() == s.option_exchange
                and str(row.get("product")).upper() == PRODUCT
            ):
                entry = float(row.get("average_price") or 0) or prices.get(
                    s.option_exchange, symbol
                )
                if not entry:
                    continue
                prices.add(s.option_exchange, symbol)
                state["trade"] = Trade(symbol, quantity, entry, new_risk(symbol, entry, quantity))
                log.warning(
                    "Found an open long of %d %s at the broker (average %.2f). Managing it "
                    "with this strategy's target, stop loss and exit rules.",
                    quantity,
                    symbol,
                    entry,
                )
                return

    def new_risk(symbol, entry, quantity):
        built = make_risk(
            symbol,
            "BUY",
            entry,
            quantity,
            stop_pct=s.stop[0],
            stop_points=s.stop[1],
            target_pct=s.target[0],
            target_points=s.target[1],
        )
        return built

    def close_trade(reason, price):
        trade = state["trade"]
        if trade is None or _time.monotonic() < trade.retry_exit_at:
            return False
        log.info("EXIT %s %s: %s", trade.quantity, trade.symbol, reason)
        fill = flatten(client, name, trade.symbol, s.option_exchange, PRODUCT, "Exit", "BUY")
        if fill.status == "unconfirmed":
            left, _ = broker_position(client, name, trade.symbol, s.option_exchange, PRODUCT)
            fill = Fill("complete", price) if left == 0 else fill
        if fill.status not in ("complete", "matched"):
            trade.retry_exit_at = _time.monotonic() + 10.0
            log.error("The exit did not go through; the position is still open. Retrying in 10s.")
            return False
        exit_price = fill.price or price or trade.entry
        pnl = (exit_price - trade.entry) * trade.quantity
        state["realized"] += pnl
        log.info("Closed at %.2f, P&L %.2f (today %.2f)", exit_price, pnl, state["realized"])
        state["trade"] = None
        return True

    def open_trade(leg, lot, analysis):
        symbol = leg["symbol"]
        ltp = float(leg["ltp"])
        lots = int(CAPITAL // (ltp * lot))
        if lots < 1:
            log.warning(
                "Capital %s is not enough for one lot of %s: %.2f x %d = %.2f. Skipped.",
                CAPITAL,
                symbol,
                ltp,
                lot,
                ltp * lot,
            )
            return False
        quantity = lots * lot
        log.info(
            "ENTRY BUY %d lots (%d) %s near %.2f, capital used %.2f of %s | %s",
            lots,
            quantity,
            symbol,
            ltp,
            quantity * ltp,
            CAPITAL,
            describe(analysis),
        )
        prices.add(s.option_exchange, symbol)
        fill = smart_order(
            client, name, symbol, s.option_exchange, PRODUCT, "BUY", quantity, quantity, "Entry"
        )
        if fill.status in ("unconfirmed", "matched"):
            held, average = broker_position(client, name, symbol, s.option_exchange, PRODUCT)
            if held != quantity:
                log.error(
                    "The entry was not confirmed and the broker shows %s; none assumed.", held
                )
                return False
            fill = Fill("complete", average or ltp)
        if fill.status != "complete":
            return False
        entry = fill.price or ltp
        risk_state = new_risk(symbol, entry, quantity)
        state["trade"] = Trade(symbol, quantity, entry, risk_state)
        log.info(
            "Entered at %.2f. Stop %s, target %s.",
            entry,
            _fmt(risk_state.stop_price),
            _fmt(risk_state.target_price),
        )
        return True

    def on_bar(boundary, now, late):
        """Returns True when this bar is dealt with, False to ask again in a few seconds."""
        expected = boundary - timedelta(minutes=s.minutes)
        trade = state["trade"]
        if trade is not None:
            bars = option_bars(client, trade.symbol, s.option_exchange, s, now)
            if bars is None or bars.index[-1] != expected:
                return late
            analysis = analyse(bars, s)
            if analysis is None:
                return True
            log.info("%s %s", trade.symbol, describe(analysis))
            if analysis["cross_down"]:
                close_trade(
                    "RSI crossed under its smoothing line",
                    prices.get(s.option_exchange, trade.symbol),
                )
            return True
        if not (s.window_start <= now.time() < s.exit_at):
            return True
        chain = option_chain(client, s, expiry_code(client, s, cache))
        if chain is None:
            return late
        waiting = False
        for kind in s.sides:
            leg = pick_leg(chain, s, kind)
            if leg is None:
                continue
            bars = option_bars(client, leg["symbol"], s.option_exchange, s, now)
            if bars is None or bars.index[-1] != expected:
                waiting = True
                continue
            analysis = analyse(bars, s)
            if analysis is None:
                say_now_and_then(
                    logging.INFO,
                    "warm" + leg["symbol"],
                    "%s: %d bars so far, indicators are still warming up.",
                    leg["symbol"],
                    len(bars),
                )
                continue
            delta = leg.get("delta")
            delta_text = "n/a" if delta is None else f"{abs(float(delta)):.2f}"
            log.info(
                "%s %s strike %.0f (ATM %.0f) ltp %.2f |delta| %s | %s",
                kind,
                leg["symbol"],
                leg["strike"],
                leg["atm"],
                float(leg["ltp"]),
                delta_text,
                describe(analysis),
            )
            if not (analysis["cross_up"] and analysis["uptrend"]):
                continue
            if s.delta_low is not None:
                if delta is None or not s.delta_low <= abs(float(delta)) <= s.delta_high:
                    log.info(
                        "Signal on %s skipped: |delta| %s is outside %.2f-%.2f.",
                        leg["symbol"],
                        delta_text,
                        s.delta_low,
                        s.delta_high,
                    )
                    continue
            if open_trade(leg, int(leg.get("lotsize") or 1), analysis):
                return True
        return late or not waiting

    adopt_open_position()
    last_boundary = latest_boundary(now_ist() - timedelta(seconds=BAR_DELAY_SECONDS), s.minutes)
    log.info("Waiting for the next %s bar to close.", s.interval)
    next_try, next_reconcile = 0.0, 0.0
    try:
        while not STOP.is_set():
            now = now_ist()
            trade = state["trade"]
            price = prices.get(s.option_exchange, trade.symbol) if trade else None

            if trade and price:
                trade.risk, decision = step_risk(trade.risk, price)
                if decision.breached:
                    close_trade(decision.detail, price)

            if state["trade"] and _time.monotonic() >= next_reconcile:
                next_reconcile = _time.monotonic() + 30.0
                held = state["trade"]
                quantity, _avg = broker_position(
                    client, name, held.symbol, s.option_exchange, PRODUCT
                )
                if quantity == 0:
                    log.warning(
                        "The broker shows no %s position (closed outside this strategy).",
                        held.symbol,
                    )
                    state["trade"] = None
                elif quantity and quantity != held.quantity:
                    log.warning(
                        "The broker shows %d %s, not %d. Using the broker's.",
                        quantity,
                        held.symbol,
                        held.quantity,
                    )
                    held.quantity = quantity

            if state["trade"] and now.time() >= s.exit_at:
                close_trade("end of the trading window", price)

            boundary = latest_boundary(now - timedelta(seconds=BAR_DELAY_SECONDS), s.minutes)
            if (
                boundary is not None
                and boundary != last_boundary
                and now.time() < MARKET_CLOSE
                and _time.monotonic() >= next_try
            ):
                late = now > boundary + timedelta(seconds=max(30, s.base_minutes * 60))
                if on_bar(boundary, now, late):
                    last_boundary = boundary
                else:
                    next_try = _time.monotonic() + 3.0
            pause(1.0)
    finally:
        trade = state["trade"]
        if trade and CLOSE_ON_STOP:
            flatten(
                client,
                name,
                trade.symbol,
                s.option_exchange,
                PRODUCT,
                "Close on stop",
                "BUY",
                wait=False,
            )
        elif trade:
            log.warning(
                "Stopped with %d %s still open at the broker. It stays open; this strategy "
                "manages it again when started.",
                trade.quantity,
                trade.symbol,
            )
        prices.stop()
        client.close()
        log.info("Stopped. Realized P&L today: %.2f", state["realized"])