import base64
import json
from datetime import date, datetime, timezone

import pytest

from api.errors import ApiError
from api.pagination import BARS_CURSOR, DAILY_CURSOR, UNIVERSE_CURSOR, decode_cursor, encode_cursor


def test_a_cursor_round_trips_through_encode_and_decode():
    values = {"ts": datetime(2026, 6, 30, 20, 54, tzinfo=timezone.utc), "symbol": "AAPL"}
    cursor = encode_cursor(UNIVERSE_CURSOR, values)

    # exact bytes, not just a successful round trip: this is what catches a reversed field order
    # or a renderer that swaps .isoformat()'s "T" for str()'s space (same row, different cursor)
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    assert raw == '{"ts":"2026-06-30T20:54:00+00:00","symbol":"AAPL"}'

    decoded = decode_cursor(UNIVERSE_CURSOR, cursor)
    assert decoded == values

    # encode validates nothing, so the renderer refusing a wrong type is the only loud failure --
    # and datetime subclasses date, so a lenient day renderer would drop the time and hand two
    # different rows the same cursor instead of raising
    with pytest.raises(TypeError):
        encode_cursor(DAILY_CURSOR, {"day": datetime(2026, 6, 30, 20, 54, tzinfo=timezone.utc)})
    # and the mirror: a naive ts encodes fine but this module's own decoder refuses it, so the
    # endpoint would issue a next_cursor that its own next request is then refused for
    with pytest.raises(TypeError):
        encode_cursor(BARS_CURSOR, {"ts": datetime(2026, 6, 30, 20, 54)})


def test_each_decode_step_reports_the_step_that_rejected_it():
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()

    cases = [
        (base64.urlsafe_b64encode(b"{not json").decode(), "not_json"),
        (enc({"day": "2026-06-30"}), "wrong_fields"),
        (enc({"ts": 12345}), "wrong_types"),
        (enc({"ts": "the thirtieth of June"}), "unparsable_ts"),
    ]
    for cursor, expected_reason in cases:
        with pytest.raises(ApiError) as excinfo:
            decode_cursor(BARS_CURSOR, cursor)
        assert excinfo.value.status == 400
        assert excinfo.value.code == "invalid_cursor"
        assert excinfo.value.detail["reason"] == expected_reason


def test_a_tampered_cursor_is_refused_rather_than_silently_decoded():
    good = encode_cursor(BARS_CURSOR, {"ts": datetime(2026, 6, 30, 20, 54, tzinfo=timezone.utc)})
    tampered = good[:4] + "!!!!" + good[4:]

    with pytest.raises(ApiError) as excinfo:
        decode_cursor(BARS_CURSOR, tampered)
    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_cursor"
    assert excinfo.value.detail["reason"] == "not_base64"


def test_a_cursor_whose_bytes_are_not_utf_eight_is_a_four_hundred():
    with pytest.raises(ApiError) as excinfo:
        decode_cursor(BARS_CURSOR, "__4=")
    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_cursor"
    assert excinfo.value.detail["reason"] == "not_base64"


def test_a_cursor_outside_the_requested_window_is_refused():
    cursor = encode_cursor(BARS_CURSOR, {"ts": datetime(2026, 6, 30, 20, 54, tzinfo=timezone.utc)})

    with pytest.raises(ApiError) as excinfo:
        decode_cursor(BARS_CURSOR, cursor, start=date(2025, 1, 1), end=date(2025, 3, 1))
    assert excinfo.value.detail["reason"] == "cursor_outside_window"


def test_a_cursor_exactly_on_either_window_bound_is_accepted():
    window = (date(2026, 6, 1), date(2026, 6, 30))
    on_end = encode_cursor(BARS_CURSOR, {"ts": datetime(2026, 6, 30, 20, 54, tzinfo=timezone.utc)})
    on_start = encode_cursor(BARS_CURSOR, {"ts": datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)})
    day_on_end = encode_cursor(DAILY_CURSOR, {"day": date(2026, 6, 30)})

    assert decode_cursor(BARS_CURSOR, on_end, *window)["ts"].minute == 54
    assert decode_cursor(BARS_CURSOR, on_start, *window)["ts"].hour == 0
    assert decode_cursor(DAILY_CURSOR, day_on_end, *window)["day"] == date(2026, 6, 30)


def test_a_cursor_just_past_the_end_of_day_bound_is_refused():
    window = (date(2026, 6, 1), date(2026, 6, 30))
    # one microsecond past datetime.combine(date(2026, 6, 30), time.max): the smallest value the
    # inclusive end-of-day bound excludes
    probes = [
        (BARS_CURSOR, encode_cursor(BARS_CURSOR, {"ts": datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)})),
        (BARS_CURSOR, encode_cursor(BARS_CURSOR, {"ts": datetime(2026, 7, 1, 20, 54, tzinfo=timezone.utc)})),
        (
            BARS_CURSOR,
            encode_cursor(
                BARS_CURSOR, {"ts": datetime(2026, 5, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)}
            ),
        ),
        (DAILY_CURSOR, encode_cursor(DAILY_CURSOR, {"day": date(2026, 7, 1)})),
    ]
    for shape, cursor in probes:
        with pytest.raises(ApiError) as excinfo:
            decode_cursor(shape, cursor, *window)
        assert excinfo.value.detail["reason"] == "cursor_outside_window"
