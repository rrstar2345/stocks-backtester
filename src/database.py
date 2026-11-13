import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

DB_PATH = "market_data.duckdb"
IST = "Asia/Kolkata"
COLS = ["epoch", "open", "high", "low", "close", "volume"]
# Fyers limits per request: ~100 days intraday, ~366 days daily; stay conservative.
CHUNK_DAYS = {"D": 300, "1D": 300}
INTRADAY_CHUNK_DAYS = 60
MIN_REQUEST_GAP = 0.35  # Fyers allows 10 requests/s but only 200/min
_last_call = 0.0
DAY = timedelta(days=1)


def _throttle():
    global _last_call
    wait = MIN_REQUEST_GAP - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _now_ist():
    return datetime.now(ZoneInfo(IST))


def init_db():
    conn = duckdb.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            symbol VARCHAR, resolution VARCHAR, epoch BIGINT,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,
            PRIMARY KEY (symbol, resolution, epoch)
        )
    """)
    # Date ranges already requested from Fyers (also when Fyers had nothing: holidays, illiquid
    # strikes), so a missing candle is not mistaken for a missing download.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_coverage (
            symbol VARCHAR, resolution VARCHAR, from_date DATE, to_date DATE
        )
    """)
    conn.close()


def _with_timestamp(df):
    """Add an IST (naive) 'timestamp' column from epoch seconds."""
    df = df.copy()
    df["timestamp"] = (pd.to_datetime(df["epoch"], unit="s", utc=True)
                       .dt.tz_convert(IST).dt.tz_localize(None))
    return df


def store_candles(symbol, resolution, df):
    if df.empty:
        return
    init_db()
    rows = df[COLS].copy()
    rows["volume"] = rows["volume"].fillna(0).astype("int64")
    rows.insert(0, "resolution", str(resolution))
    rows.insert(0, "symbol", symbol)
    conn = duckdb.connect(DB_PATH)
    conn.register("df_candles", rows)
    conn.execute("INSERT OR REPLACE INTO candles SELECT * FROM df_candles")
    conn.close()


def fetch_candles_from_db(symbol, resolution, start_date, end_date):
    init_db()
    lo = int(pd.Timestamp(start_date).tz_localize(IST).timestamp())
    hi = int((pd.Timestamp(end_date) + DAY).tz_localize(IST).timestamp())
    conn = duckdb.connect(DB_PATH)
    df = conn.execute(
        "SELECT epoch, open, high, low, close, volume FROM candles "
        "WHERE symbol = ? AND resolution = ? AND epoch >= ? AND epoch < ? ORDER BY epoch",
        [symbol, str(resolution), lo, hi],
    ).fetchdf()
    conn.close()
    return _with_timestamp(df) if not df.empty else df


# ------------------------------------------------------------------ coverage bookkeeping
def _merge(intervals):
    """Union of inclusive date intervals; touching / adjacent ones are joined."""
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1] + DAY:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _gaps(start, end, covered):
    """Parts of [start, end] not inside the (merged, sorted) covered intervals."""
    gaps, cur = [], start
    for s, e in covered:
        if e < cur:
            continue
        if s > end:
            break
        if s > cur:
            gaps.append((cur, s - DAY))
        cur = max(cur, e + DAY)
        if cur > end:
            break
    if cur <= end:
        gaps.append((cur, end))
    return gaps


def _get_coverage(symbol, resolution):
    init_db()
    conn = duckdb.connect(DB_PATH)
    rows = conn.execute("SELECT from_date, to_date FROM fetch_coverage WHERE symbol = ? AND resolution = ?",
                        [symbol, str(resolution)]).fetchall()
    conn.close()
    return _merge(rows)


def _add_coverage(symbol, resolution, new):
    if not new:
        return
    merged = _merge(_get_coverage(symbol, resolution) + new)
    conn = duckdb.connect(DB_PATH)
    conn.execute("DELETE FROM fetch_coverage WHERE symbol = ? AND resolution = ?", [symbol, str(resolution)])
    conn.executemany("INSERT INTO fetch_coverage VALUES (?, ?, ?, ?)",
                     [[symbol, str(resolution), s, e] for s, e in merged])
    conn.close()


def _complete_through(symbol):
    """Last date whose candles are final. Today only counts once its session has closed, so a
    partial day is never recorded as fetched."""
    now = _now_ist()
    today = now.date()
    if symbol.startswith("MCX:"):
        close = (23, 59)
    elif re.match(r"^NSE:(USD|EUR|GBP|JPY)INR", symbol):
        close = (17, 10)
    else:
        close = (15, 40)
    if today.weekday() >= 5 or (now.hour, now.minute) >= close:
        return today
    return today - DAY


# ------------------------------------------------------------------ fetching
def fetch_history_fyers(fyers_client, symbol, resolution="1", start_date="2026-01-01",
                        end_date="2026-03-01", use_cache=True, stats=None):
    """Return candles (with an IST 'timestamp' column) for symbol/resolution/range.

    Only date ranges not already recorded in the local DuckDB cache are requested from Fyers
    (use_cache=False re-fetches the whole range). `stats`, if given, is updated with
    api_calls / from_cache / fetched counters. Raises RuntimeError if Fyers returned errors and
    there is no data at all."""
    stats = stats if stats is not None else {}
    for k in ("api_calls", "from_cache", "fetched"):
        stats.setdefault(k, 0)

    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = min(datetime.strptime(end_date, "%Y-%m-%d").date(), _now_ist().date())
    if start > end:
        return pd.DataFrame()

    covered = _get_coverage(symbol, resolution) if use_cache else []
    gaps = _gaps(start, end, covered)
    step = timedelta(days=CHUNK_DAYS.get(str(resolution), INTRADAY_CHUNK_DAYS) - 1)
    final_day = _complete_through(symbol)

    errors, new_cover = [], []
    for g_start, g_end in gaps:
        cur = g_start
        while cur <= g_end:
            cur_end = min(cur + step, g_end)
            _throttle()
            stats["api_calls"] += 1
            resp = fyers_client.history(data={
                "symbol": symbol,
                "resolution": str(resolution),
                "date_format": "1",  # epoch
                "range_from": cur.strftime("%Y-%m-%d"),
                "range_to": cur_end.strftime("%Y-%m-%d"),
                "cont_flag": "1",
            })
            status = resp.get("s")
            if status in ("ok", "no_data"):
                if resp.get("candles"):
                    chunk = pd.DataFrame([c[:6] for c in resp["candles"]], columns=COLS)
                    store_candles(symbol, resolution, chunk.drop_duplicates(subset=["epoch"]))
                if min(cur_end, final_day) >= cur:
                    new_cover.append((cur, min(cur_end, final_day)))
            else:
                errors.append(str(resp.get("message") or resp))
            cur = cur_end + DAY
    _add_coverage(symbol, resolution, new_cover)
    stats["fetched" if gaps else "from_cache"] += 1

    df = fetch_candles_from_db(symbol, resolution, str(start), str(end))
    if df.empty and errors:
        raise RuntimeError(f"Fyers history error for {symbol}: {errors[0]}")
    return df
