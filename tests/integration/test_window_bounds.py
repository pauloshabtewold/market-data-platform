from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.market_fixture import load_run_calendar, load_run_symbol, run_days

# the committed /daily and /largest-moves fixtures load data only inside the window the test then
# asks for, so a handler that widened its own bound would have nothing extra to return
DAYS = run_days(40)
NARROW_END = DAYS[9]
LATE_START = DAYS[30]


@pytest.fixture
def client(migrated_dsn):
    load_run_calendar(migrated_dsn, DAYS)
    # a close that moves every session, so every bar qualifies at min_move_pct = 0 and a widened
    # window has rows to leak rather than merely a wider plan
    load_run_symbol(migrated_dsn, "AAA", [100.0 + i for i in range(len(DAYS))], DAYS)
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


def test_daily_answers_no_day_after_the_end_the_client_asked_for(client):
    body = client.get(
        "/symbols/AAA/daily",
        params={"start": DAYS[0].isoformat(), "end": NARROW_END.isoformat(), "limit": 1000},
    ).json()
    days = [date.fromisoformat(row["day"]) for row in body["data"]]
    assert days, "the fixture answered nothing, so this assertion cannot discriminate"
    assert max(days) <= NARROW_END, (
        f"/daily answered {max(days)} for a window ending {NARROW_END}:"
        f" {len(days)} rows, last five {days[-5:]}"
    )


def test_largest_moves_answers_no_bar_before_the_start_the_client_asked_for(client):
    # this one catches the PAIR: the handler bounds the window by day and by instant, so widening
    # either alone still leaves the other constraining and only both together leak a row
    body = client.get(
        "/analytics/largest-moves",
        params={"start": LATE_START.isoformat(), "end": DAYS[-1].isoformat(), "limit": 1000},
    ).json()
    stamps = [row["ts"] for row in body["data"]]
    assert stamps, "the fixture answered nothing, so this assertion cannot discriminate"
    first = min(date.fromisoformat(stamp[:10]) for stamp in stamps)
    assert first >= LATE_START, (
        f"/analytics/largest-moves answered a bar on {first} for a window starting {LATE_START}:"
        f" {len(stamps)} rows"
    )
