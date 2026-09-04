from datetime import datetime, timezone

import pytest

from api.pagination import BARS_CURSOR, PaginationError, decode_cursor, paginate


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
