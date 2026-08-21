import json
from datetime import UTC, datetime

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
    assert client.calls[0][1] == "/v2/calendar"
    assert client.calls[0][3] == "calendar"

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
