import json
from datetime import UTC, datetime

import pytest

from config import settings
from db.session import connect
from ingest.calendar import load_calendar


class StubClient:
    """Stands in for AlpacaClient so the suite never spends the shared request budget."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def get_json(self, base_url, path, params, phase):
        self.calls.append((base_url, path, params, phase))
        return self._rows


def _rows(fixtures_dir):
    return json.loads((fixtures_dir / "calendar_sample.json").read_text())


def _market_days(dsn):
    with connect(dsn) as conn:
        return {
            row[0].isoformat(): row[1:]
            for row in conn.execute("SELECT day, open_ts, close_ts, session_minutes FROM market_days")
        }


def test_calendar_load_converts_wall_clock_through_new_york(migrated_dsn, fixtures_dir):
    client = StubClient(_rows(fixtures_dir))
    with connect(migrated_dsn) as conn:
        summary = load_calendar(conn, client)

    assert summary.days == 4
    assert (summary.first, summary.last) == ("2025-11-26", "2026-06-01")
    # the trading host is account-dependent and the wrong one returns a 403 that reads like a bad key, so the request records where it went
    base_url, path, params, phase = client.calls[0]
    assert (base_url, path, phase) == (settings.ALPACA_TRADING_HOST, "/v2/calendar", "calendar")
    assert params == {"start": settings.INGEST_START.isoformat(), "end": settings.INGEST_END.isoformat()}

    days = _market_days(migrated_dsn)
    assert days["2026-01-02"][0] == datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    assert days["2026-06-01"][0] == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    assert days["2025-11-28"][2] == 210
    assert days["2025-11-26"][2] == 390
    assert "2025-11-27" not in days


def test_calendar_load_is_idempotent(migrated_dsn, fixtures_dir):
    rows = _rows(fixtures_dir)
    with connect(migrated_dsn) as conn:
        load_calendar(conn, StubClient(rows))
    first = _market_days(migrated_dsn)

    with connect(migrated_dsn) as conn:
        load_calendar(conn, StubClient(rows))
    assert _market_days(migrated_dsn) == first


@pytest.mark.parametrize(
    "bad",
    [
        {"date": "2026-06-02", "open": None, "close": "16:00"},
        {"date": "2026-06-02", "open": "09:30", "close": None},
        # a row with no day at all reaches the insert as a NULL primary key, which is a different error in a later place
        {"date": None, "open": "09:30", "close": "16:00"},
    ],
    ids=["no open", "no close", "no day"],
)
def test_a_day_with_no_session_times_aborts_before_anything_is_stored(migrated_dsn, fixtures_dir, bad):
    rows = _rows(fixtures_dir) + [bad]

    with connect(migrated_dsn) as conn:
        with pytest.raises(RuntimeError, match="no session times"):
            load_calendar(conn, StubClient(rows))

    # a null open stores as a NULL open_ts and a NULL session_minutes, which drops the day out of the expected-minute total
    assert _market_days(migrated_dsn) == {}


@pytest.mark.parametrize(
    "closing",
    # equal is the half that had no case: it stores session_minutes = 0, so a day nothing covers reads as fully covered
    ["09:30", "09:00"],
    ids=["equal to its open", "before its open"],
)
def test_a_close_on_or_before_its_open_aborts(migrated_dsn, closing):
    rows = [{"date": "2026-06-02", "open": "09:30", "close": closing}]

    with connect(migrated_dsn) as conn:
        with pytest.raises(RuntimeError, match="before its"):
            load_calendar(conn, StubClient(rows))


def test_a_corrected_session_replaces_the_one_already_stored(migrated_dsn):
    full = [{"date": "2026-11-27", "open": "09:30", "close": "16:00"}]
    half = [{"date": "2026-11-27", "open": "09:30", "close": "13:00"}]

    with connect(migrated_dsn) as conn:
        load_calendar(conn, StubClient(full))
    assert _market_days(migrated_dsn)["2026-11-27"][2] == 390

    with connect(migrated_dsn) as conn:
        load_calendar(conn, StubClient(half))

    # the idempotency test reloads identical rows, which passes under DO NOTHING and DO UPDATE alike -- only a changed value tells them apart
    assert _market_days(migrated_dsn)["2026-11-27"][2] == 210
