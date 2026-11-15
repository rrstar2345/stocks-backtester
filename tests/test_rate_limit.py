"""Rate limiting of Fyers history requests (fake clock and fake client; no network, no real waiting)."""
from collections import deque
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src import database as db

IST = ZoneInfo("Asia/Kolkata")
LIMIT_RESP = {"s": "error", "code": -360, "message": "You've reached the Standard plan limit of 50 requests/minute."}


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch, tmp_path):
    clk = FakeClock()
    monkeypatch.setattr(db, "time", SimpleNamespace(monotonic=clk.monotonic, sleep=clk.sleep))
    monkeypatch.setattr(db, "_calls", deque())
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "market.duckdb"))
    # A Saturday, so "today" counts as a complete day and ranges are cached up to the end date.
    monkeypatch.setattr(db, "_now_ist", lambda: datetime(2026, 11, 14, 12, 0, tzinfo=IST))
    return clk


class FakeFyers:
    """Answers history() with one candle per requested day, or with `error` from call number `fail_at`."""

    def __init__(self, fail_at=None, error=LIMIT_RESP):
        self.calls, self.fail_at, self.error = 0, fail_at, error

    def history(self, data):
        self.calls += 1
        if self.fail_at is not None and self.calls >= self.fail_at:
            return dict(self.error)
        days = pd.date_range(data["range_from"], data["range_to"])
        epoch = [int(d.tz_localize(IST).timestamp()) + 33300 for d in days]
        return {"s": "ok", "candles": [[e, 1, 2, 0.5, 1.5, 10] for e in epoch]}


def test_throttle_stays_under_standard_plan_limits(clock):
    times = []
    for _ in range(200):
        db._throttle()
        times.append(clock.t)
    for t in times:
        assert sum(1 for x in times if t - 60 < x <= t) <= 50  # per minute
        assert sum(1 for x in times if t - 1 < x <= t) <= 5    # per second


def test_throttle_reports_the_wait(clock):
    waits = []
    for _ in range(db.RATE_PER_MINUTE):
        db._throttle()
    db._throttle(on_wait=waits.append)
    assert waits and waits[0] > 30  # the per-minute budget is used up; it must wait for the window


def test_rate_limit_stops_at_once_and_keeps_progress(clock):
    start, end = "2026-01-01", "2026-11-14"  # 6 requests of 60 days at 5-minute bars
    client = FakeFyers(fail_at=3)
    with pytest.raises(db.RateLimitError):
        db.fetch_history_fyers(client, "NSE:X", "5", start, end)
    assert client.calls == 3  # nothing is sent after the first rate-limit response

    resumed = FakeFyers()
    df = db.fetch_history_fyers(resumed, "NSE:X", "5", start, end)
    assert resumed.calls == 4  # only the 4 chunks that were not saved (2 were stored before the limit)
    assert df["timestamp"].dt.date.min() == date(2026, 1, 1)
    assert df["timestamp"].dt.date.max() == date(2026, 11, 14)

    again = FakeFyers()
    db.fetch_history_fyers(again, "NSE:X", "5", start, end)
    assert again.calls == 0


def test_other_errors_are_not_rate_limits(clock):
    bad = {"s": "error", "code": -300, "message": "Please provide a valid symbol"}
    client = FakeFyers(fail_at=1, error=bad)
    with pytest.raises(RuntimeError):
        db.fetch_history_fyers(client, "NSE:X", "5", "2026-01-01", "2026-11-14")
    assert client.calls == 6  # as before: the remaining chunks are still tried


def test_rate_limit_error_is_not_swallowed_as_a_failed_symbol():
    # optionchain.build_chain skips a symbol on RuntimeError; a rate limit must stop the whole run.
    assert not issubclass(db.RateLimitError, RuntimeError)
