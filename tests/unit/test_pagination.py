from datetime import date, datetime, timezone

import pytest

import api.pagination
from api.pagination import (
    BARS_CURSOR,
    DAILY_CURSOR,
    SYMBOLS_CURSOR,
    CursorWindowMisuse,
    PaginationError,
    decode_cursor,
    encode_cursor,
    paginate,
)


def _row(minute):
    return {"ts": datetime(2026, 6, 30, 14, minute, tzinfo=timezone.utc)}


def test_the_next_cursor_names_the_last_returned_row_rather_than_the_probe_row():
    rows = [_row(i) for i in range(4)]
    page = paginate(rows, limit=3, shape=BARS_CURSOR)
    assert len(page.data) == 3
    # reads the cursor's value rather than just checking next_cursor is not None -- that weaker
    # form passes both the correct implementation and the off-by-one that emits the probe row
    assert decode_cursor(BARS_CURSOR, page.next_cursor)["ts"].minute == 2


def test_a_page_exactly_the_limit_long_reports_no_next_page():
    rows = [_row(i) for i in range(3)]
    page = paginate(rows, limit=3, shape=BARS_CURSOR)
    assert len(page.data) == 3
    assert page.next_cursor is None


def test_an_over_fetch_is_refused_rather_than_truncated():
    rows = [_row(i) for i in range(5)]
    with pytest.raises(PaginationError):
        paginate(rows, limit=3, shape=BARS_CURSOR)
    # the other end of the same contract: at limit=0, rows[limit - 1] is the probe row itself
    with pytest.raises(PaginationError):
        paginate(rows[:1], limit=0, shape=BARS_CURSOR)
    # and the smallest value that must be accepted, which a floor tested only from below cannot see:
    # `< 1` written as `<= 1` or `< 2` refuses it, and a ?limit=1 request would answer 500
    page = paginate(rows[:2], limit=1, shape=BARS_CURSOR)
    assert len(page.data) == 1
    assert decode_cursor(BARS_CURSOR, page.next_cursor)["ts"].minute == 0


def test_every_refusal_this_module_raises_says_what_it_refused():
    # the wire messages of a 400 are pinned at the endpoints; these are the ones no client sees --
    # the programming errors and the diagnostics an operator reads out of a traceback. Each of them
    # is replaceable by None with the rest of the suite green, which is the endpoints' own gap one
    # layer down
    with pytest.raises(ValueError, match="naive timestamp"):
        api.pagination._parse_utc_instant("2026-06-30T14:00:00")
    with pytest.raises(TypeError, match="day renders a date, not datetime"):
        encode_cursor(DAILY_CURSOR, {"day": datetime(2026, 6, 30, tzinfo=timezone.utc)})
    with pytest.raises(TypeError, match="symbol renders a str, not int"):
        encode_cursor(SYMBOLS_CURSOR, {"symbol": 7})
    with pytest.raises(TypeError, match="ts renders an aware datetime, not date"):
        encode_cursor(BARS_CURSOR, {"ts": date(2026, 6, 30)})
    with pytest.raises(
        CursorWindowMisuse, match="the symbols cursor has no date window"
    ):
        decode_cursor(
            SYMBOLS_CURSOR,
            encode_cursor(SYMBOLS_CURSOR, {"symbol": "AAA"}),
            date(2026, 6, 1),
            date(2026, 6, 30),
        )
    rows = [_row(i) for i in range(5)]
    with pytest.raises(PaginationError, match="a limit of 0 cannot page anything"):
        paginate(rows[:1], limit=0, shape=BARS_CURSOR)
    with pytest.raises(PaginationError, match="5 rows for a limit of 3"):
        paginate(rows, limit=3, shape=BARS_CURSOR)
