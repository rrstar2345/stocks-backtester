"""Option-chain data pipeline for backtesting.

Order of work (see build_chain):
  1. fetch the underlying (index / equity) candles for the range,
  2. list all strikes for underlying + expiry,
  3. pick the strike for every bar from the underlying's close (ATM / ITM n / OTM n),
     keeping only the distinct strikes that were ever selected,
  4. fetch candles for those strikes only.

Expiry is relative to today: Current / Next n come from the live symbol master. Previous n has
already expired, so it is not in the master: its date is derived from the expiry cadence and its
tickers are built with Fyers' naming rules (the rule is checked against the live master).
"""
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from src import symbols
from src.database import fetch_history_fyers

SPOT_INDEX = {
    "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX", "SENSEX": "BSE:SENSEX-INDEX", "BANKEX": "BSE:BANKEX-INDEX",
}
CASH_EXCHANGE = {"NFO": "NSE", "BFO": "BSE"}
TICKER_PREFIX = {"NFO": "NSE", "BFO": "BSE"}
MONTH_CODE = {**{m: str(m) for m in range(1, 10)}, 10: "O", 11: "N", 12: "D"}
EXPIRY_CHOICES = ["Current", "Previous 1", "Previous 2", "Previous 3", "Next 1", "Next 2", "Next 3"]
STRIKE_CHOICES = ["ATM"] + [f"ITM {i}" for i in range(1, 6)] + [f"OTM {i}" for i in range(1, 6)]


def parse_expiry_choice(choice):
    """Current -> 0, Previous n -> -n, Next n -> +n."""
    if choice == "Current":
        return 0
    kind, n = choice.split()
    return -int(n) if kind == "Previous" else int(n)


def parse_strike_choice(choice):
    """ATM -> 0, OTM n -> +n, ITM n -> -n (steps away from ATM, for the CE side)."""
    if choice == "ATM":
        return 0
    kind, n = choice.split()
    return int(n) if kind == "OTM" else -int(n)


# ------------------------------------------------------------------ symbols
def spot_symbol(master, exchange, underlying):
    """Fyers ticker of the underlying's spot (index or equity), or None."""
    ticker = SPOT_INDEX.get(underlying)
    if ticker and (master["brsymbol"] == ticker).any():
        return ticker
    cash = CASH_EXCHANGE[exchange]
    for exch in (cash, cash + "_INDEX"):
        hit = master[(master["exchange"] == exch) & (master["symbol"] == underlying)]
        if not hit.empty:
            return hit.iloc[0]["brsymbol"]
    return None


def option_rows(master, exchange, underlying):
    return symbols.contracts(master, exchange, "OPT", underlying)


def live_expiries(master, exchange, underlying, today):
    rows = option_rows(master, exchange, underlying)
    return sorted(pd.Timestamp(x) for x in rows["expiry"].dropna().unique() if pd.Timestamp(x) >= today)


def live_grid(master, exchange, underlying, expiry, opt):
    """(strike, brsymbol) rows for one listed expiry and option type, sorted by strike."""
    rows = option_rows(master, exchange, underlying)
    rows = rows[(rows["expiry"] == expiry) & (rows["instrumenttype"] == opt)]
    return rows.sort_values("strike")[["strike", "brsymbol"]]


def _fmt_strike(x):
    return str(int(x)) if float(x) == int(x) else str(x)


def make_ticker(exchange, underlying, expiry, strike, opt, monthly):
    """Fyers option ticker. Monthly: NSE:NIFTY26NOV24000CE. Weekly: NSE:NIFTY26N1724000CE
    (month code 1-9, O, N, D, then the day)."""
    code = expiry.strftime("%b").upper() if monthly else MONTH_CODE[expiry.month] + expiry.strftime("%d")
    return f"{TICKER_PREFIX[exchange]}:{underlying}{expiry.strftime('%y')}{code}{_fmt_strike(strike)}{opt}"


def is_weekly_cadence(live):
    """True when the underlying has weekly expiries (nearest two are about a week apart)."""
    return len(live) > 1 and (live[1] - live[0]).days <= 8


def _is_monthly(expiry, weekly):
    """Monthly-format ticker? Monthly-only underlyings always; otherwise the last expiry weekday
    of the month (no later same-weekday date within the month)."""
    return (not weekly) or (expiry + timedelta(days=7)).month != expiry.month


def check_ticker_format(master, exchange, underlying, live):
    """Share of listed contracts whose ticker make_ticker() reproduces (1.0 = rule verified)."""
    rows = option_rows(master, exchange, underlying)
    if rows.empty:
        return 0.0
    rows = rows.iloc[::max(1, len(rows) // 300)]
    weekly = is_weekly_cadence(live)
    ok = sum(
        make_ticker(exchange, underlying, r.expiry, r.strike, r.instrumenttype,
                    _is_monthly(r.expiry, weekly)) == r.brsymbol
        for r in rows.itertuples()
    )
    return ok / len(rows)


# ------------------------------------------------------------------ expiry
@dataclass
class ExpiryInfo:
    date: pd.Timestamp
    monthly: bool   # monthly-format ticker (only used to build tickers of expired contracts)
    listed: bool    # True: in the live symbol master; False: derived from the cadence


def _last_weekday_of_month(period, weekday):
    d = period.to_timestamp(how="end").normalize()
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def resolve_expiry(live, rel):
    if not live:
        raise ValueError("No live option expiries found for this underlying in the symbol master.")
    if rel >= 0:
        if rel >= len(live):
            raise ValueError(f"Only {len(live)} expiries are listed; cannot pick 'Next {rel}'.")
        return ExpiryInfo(live[rel], _is_monthly(live[rel], is_weekly_cadence(live)), True)
    weekly = is_weekly_cadence(live)
    weekday = int(pd.Series([x.weekday() for x in live[:6]]).mode().iloc[0])
    anchor = live[0] + timedelta(days=weekday - live[0].weekday())  # undo a holiday shift
    if weekly:
        d = anchor - timedelta(days=7 * -rel)
        return ExpiryInfo(d, _is_monthly(d, True), False)
    return ExpiryInfo(_last_weekday_of_month(anchor.to_period("M") - (-rel), weekday), True, False)


# ------------------------------------------------------------------ strikes
def infer_step(grid):
    """Most common strike gap close to the middle of a listed strike grid."""
    g = np.sort(np.unique(grid))
    centre = np.median(g)
    near = np.diff(g[np.abs(g - centre) <= 0.05 * centre])
    near = near[near > 0]
    return float(pd.Series(near).mode().min()) if len(near) else float(np.diff(g).min())


def synth_grid(ltp, step, offset):
    pad = (abs(offset) + 3) * step
    lo = np.floor(np.nanmin(ltp) / step) * step - pad
    hi = np.ceil(np.nanmax(ltp) / step) * step + pad
    return np.arange(lo, hi + step / 2, step)


def pick_strike_idx(ltp, grid, offset, opt):
    """Index into the sorted strike grid for every LTP. offset: OTM n = +n, ITM n = -n, ATM = 0.
    ATM = nearest strike (ties go lower). CE moves up for OTM, PE moves down for OTM."""
    j = np.clip(np.searchsorted(grid, ltp), 1, len(grid) - 1)
    atm = np.where(ltp - grid[j - 1] <= grid[j] - ltp, j - 1, j)
    return np.clip(atm + (offset if opt == "CE" else -offset), 0, len(grid) - 1)


# ------------------------------------------------------------------ pipeline
@dataclass
class ChainData:
    underlying: pd.DataFrame
    contracts: dict                 # symbol -> candles
    selection: dict                 # "CE"/"PE" -> object array, symbol chosen on every underlying bar
    info: dict = field(default_factory=dict)


def _day(ts):
    return ts.strftime("%Y-%m-%d")


def build_chain(client, master, exchange, underlying, spot, expiry_choice, strike_choice, sides,
                resolution, start, end, warmup_days=10, progress=None, expiry_override=None,
                use_cache=True):
    say = progress or (lambda frac, text: None)
    stats = {}
    fetch = lambda sym, a, b: fetch_history_fyers(client, sym, resolution, a, b, use_cache=use_cache, stats=stats)
    today = pd.Timestamp(symbols.today_ist())
    live = live_expiries(master, exchange, underlying, today)
    exp = resolve_expiry(live, parse_expiry_choice(expiry_choice))
    if expiry_override is not None:
        o = pd.Timestamp(expiry_override)
        exp = ExpiryInfo(o, _is_monthly(o, is_weekly_cadence(live)), o in live)
    start, end = pd.Timestamp(start), min(pd.Timestamp(end), exp.date)
    if start > end:
        raise ValueError(f"Start date is after the expiry ({exp.date:%d-%b-%Y}); pick a range on or before it.")

    # 1. underlying
    say(0.0, f"Fetching underlying {spot}...")
    und = fetch(spot, _day(start), _day(end))
    if und.empty:
        raise RuntimeError(f"No candles returned for the underlying {spot}.")
    und = und[und["timestamp"].dt.normalize() <= end].reset_index(drop=True)
    ltp = und["close"].to_numpy(float)

    # 2. strikes for underlying + expiry
    offset = parse_strike_choice(strike_choice)
    grids, listed = {}, {}
    for side in sides:
        if exp.listed:
            g = live_grid(master, exchange, underlying, exp.date, side)
            if g.empty:
                raise RuntimeError(f"No {side} strikes listed for {underlying} {exp.date:%d-%b-%Y}.")
            grids[side], listed[side] = g["strike"].to_numpy(float), dict(zip(g["strike"], g["brsymbol"]))
        else:
            ref = live_grid(master, exchange, underlying, live[0], side)["strike"].to_numpy(float)
            grids[side] = synth_grid(ltp, infer_step(ref), offset)

    # 3. strikes selected by the LTP movement, per bar; keep the distinct ones
    chosen = {s: grids[s][pick_strike_idx(ltp, grids[s], offset, s)] for s in sides}
    needed = {s: sorted(np.unique(chosen[s])) for s in sides}

    def ticker(side, strike, used):
        if exp.listed:
            return listed[side][strike]
        return make_ticker(exchange, underlying, used, strike, side, exp.monthly)

    # expired contracts: if the derived date has no data (holiday-shifted expiry) try earlier days
    used, fetched = exp.date, {}
    c_start, c_end = _day(start - timedelta(days=warmup_days)), _day(end)
    if not exp.listed:
        probe_side = sides[0]
        for shift in range(4):
            used = exp.date - timedelta(days=shift)
            sym = ticker(probe_side, needed[probe_side][0], used)
            say(0.05, f"Checking expired contract {sym}...")
            try:
                df = fetch(sym, c_start, c_end)
            except RuntimeError:
                df = pd.DataFrame()
            if not df.empty:
                fetched[sym] = df
                break
        else:
            raise RuntimeError(
                f"No data for expired {underlying} contracts near {exp.date:%d-%b-%Y} "
                f"(last tried {sym}). Fyers may not serve expired contracts, or the derived expiry "
                f"date / ticker is off; try a Current or Next expiry.")

    # 4. candles for the filtered strikes
    symbol_for = {(s, k): ticker(s, k, used) for s in sides for k in needed[s]}
    todo = list(dict.fromkeys(symbol_for.values()))
    errors = []
    for n, sym in enumerate(todo):
        say(0.1 + 0.9 * n / max(len(todo), 1), f"Fetching {sym} ({n + 1}/{len(todo)})")
        if sym in fetched:
            continue
        try:
            df = fetch(sym, c_start, c_end)
        except RuntimeError as e:
            errors.append(str(e))
            continue
        if df.empty:
            errors.append(f"No data for {sym}")
        else:
            fetched[sym] = df
    if not fetched:
        raise RuntimeError("No option candles could be fetched. " + "; ".join(errors[:3]))

    selection = {s: np.array([symbol_for[(s, k)] for k in chosen[s]], dtype=object) for s in sides}
    say(1.0, "Running backtest...")
    return ChainData(und, fetched, selection, {
        "Spot symbol": spot,
        "Expiry": f"{exp.date:%d-%b-%Y} ({'listed' if exp.listed else 'derived from cadence'})"
                  + ("" if used == exp.date else f", data found on {used:%d-%b-%Y}"),
        "Strike rule": strike_choice,
        "Strikes fetched": {s: [float(k) for k in needed[s]] for s in sides},
        "Contracts": {sym: len(df) for sym, df in fetched.items()},
        "Issues": errors,
        "Data source": stats,
    })
