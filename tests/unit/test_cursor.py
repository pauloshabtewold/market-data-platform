import base64
import json
from datetime import date, datetime, timezone

import pytest

from api.errors import ApiError
from api.pagination import (
    BARS_CURSOR,
    DAILY_CURSOR,
    SYMBOLS_CURSOR,
    UNIVERSE_CURSOR,
    CursorWindowMisuse,
    decode_cursor,
    encode_cursor,
)


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
    # different rows the same cursor instead of raising.
    # The message is pinned by full-string equality for all three, written out rather than imported:
    # building the expected from the renderer moves both sides and the mutation survives (DL-010).
    with pytest.raises(TypeError) as excinfo:
        encode_cursor(DAILY_CURSOR, {"day": datetime(2026, 6, 30, 20, 54, tzinfo=timezone.utc)})
    assert str(excinfo.value) == "day renders a date, not datetime"
    # and the mirror: a naive ts encodes fine but this module's own decoder refuses it, so the
    # endpoint would issue a next_cursor that its own next request is then refused for
    with pytest.raises(TypeError) as excinfo:
        encode_cursor(BARS_CURSOR, {"ts": datetime(2026, 6, 30, 20, 54)})
    assert str(excinfo.value) == "ts renders an aware datetime, not datetime"
    # and the third renderer: bare str renders every type, so a NULL symbol column would encode to
    # the literal cursor value "None" and page against a symbol that does not exist
    with pytest.raises(TypeError) as excinfo:
        encode_cursor(UNIVERSE_CURSOR, {"ts": values["ts"], "symbol": None})
    assert str(excinfo.value) == "symbol renders a str, not NoneType"

    # exact type and not isinstance for the symbol renderer, which is what the day renderer's own
    # comment and the decoder's wrong_types guard say: a str subclass is the value that separates
    # them, and nothing else in this suite supplies one
    class _Symbolish(str):
        pass

    with pytest.raises(TypeError) as excinfo:
        encode_cursor(UNIVERSE_CURSOR, {"ts": values["ts"], "symbol": _Symbolish("AAPL")})
    assert str(excinfo.value) == "symbol renders a str, not _Symbolish"

    # the instant renderer is deliberately isinstance: for the other two the type check IS the whole
    # rule, so it is spelled exactly, while here the rule is aware versus naive -- a value -- and an
    # aware datetime subclass renders the identical bytes and decodes back to a plain datetime, so
    # exact type would refuse a value the rule accepts. A datetime through the DAY renderer is the
    # opposite case and stays exact: it renders differently and hands two rows one cursor.
    class _Instantish(datetime):
        pass

    subclassed = encode_cursor(BARS_CURSOR, {"ts": _Instantish(2026, 6, 30, 20, 54, tzinfo=timezone.utc)})
    raw_subclassed = base64.urlsafe_b64decode(subclassed.encode()).decode()
    assert raw_subclassed == '{"ts":"2026-06-30T20:54:00+00:00"}'
    assert type(decode_cursor(BARS_CURSOR, subclassed)["ts"]) is datetime
    # and the isinstance half still carries its own weight: without it a date reaches .tzinfo and
    # raises AttributeError, which nothing in this module turns into the 400 a bad cursor owes
    with pytest.raises(TypeError) as excinfo:
        encode_cursor(BARS_CURSOR, {"ts": date(2026, 6, 30)})
    assert str(excinfo.value) == "ts renders an aware datetime, not date"


def test_each_decode_step_reports_the_step_that_rejected_it():
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()

    cases = [
        (base64.urlsafe_b64encode(b"{not json").decode(), "not_json"),
        (enc({"day": "2026-06-30"}), "wrong_fields"),
        (enc({"ts": "2026-06-30T20:54:00+00:00", "extra": "x"}), "wrong_fields"),
        # the missing-field direction, where a subset check would accept and the parser would then
        # raise KeyError -- an unhandled 500 where spec line 503 requires a 400. The two cases above
        # are both rejected by a subset check as well, so neither of them can see it.
        (enc({}), "wrong_fields"),
        (enc({"ts": 12345}), "wrong_types"),
        (enc({"ts": "the thirtieth of June"}), "unparsable_ts"),
    ]
    for cursor, expected_reason in cases:
        with pytest.raises(ApiError) as excinfo:
            decode_cursor(BARS_CURSOR, cursor)
        assert excinfo.value.status == 400
        assert excinfo.value.code == "invalid_cursor"
        assert excinfo.value.message == "the cursor is not one this endpoint issued"
        assert excinfo.value.detail["reason"] == expected_reason


def test_a_cursor_in_another_offset_decodes_to_the_same_instant_in_utc():
    # aware comparison is by instant, so every other assertion in this file passes whether or not
    # the parser normalises -- the tzinfo has to be read directly
    offset = base64.urlsafe_b64encode(
        json.dumps({"ts": "2026-06-30T22:54:00+02:00"}).encode()
    ).decode()
    decoded = decode_cursor(BARS_CURSOR, offset)["ts"]
    assert decoded.tzinfo is timezone.utc
    assert (decoded.hour, decoded.minute) == (20, 54)


def test_a_tampered_cursor_is_refused_rather_than_silently_decoded():
    good = encode_cursor(BARS_CURSOR, {"ts": datetime(2026, 6, 30, 20, 54, tzinfo=timezone.utc)})
    tampered = good[:4] + "!!!!" + good[4:]

    with pytest.raises(ApiError) as excinfo:
        decode_cursor(BARS_CURSOR, tampered)
    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_cursor"
    # written out rather than imported (DL-010): building the expected from the constant moves both
    # sides, and this step's message is the one the other five steps' test does not reach
    assert excinfo.value.message == "the cursor is not one this endpoint issued"
    assert excinfo.value.detail["reason"] == "not_base64"


def test_a_cursor_whose_bytes_are_not_utf_eight_is_a_four_hundred():
    with pytest.raises(ApiError) as excinfo:
        decode_cursor(BARS_CURSOR, "__4=")
    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_cursor"
    assert excinfo.value.detail["reason"] == "not_base64"


def test_a_cursor_outside_the_requested_window_is_refused():
    # both sides, because they are two separate raise sites and every other test here drives the
    # end one: past the end, and before the start
    past_end = encode_cursor(BARS_CURSOR, {"ts": datetime(2026, 6, 30, 20, 54, tzinfo=timezone.utc)})
    before_start = encode_cursor(BARS_CURSOR, {"ts": datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)})

    for cursor in (past_end, before_start):
        with pytest.raises(ApiError) as excinfo:
            decode_cursor(BARS_CURSOR, cursor, start=date(2025, 1, 1), end=date(2025, 3, 1))
        # the whole error and not just its slug: neither branch's status or message was asserted by
        # anything, so either could answer 401, or carry a status of None and fail to serialise
        assert excinfo.value.status == 400
        assert excinfo.value.code == "invalid_cursor"
        assert excinfo.value.message == "the cursor is not one this endpoint issued"
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


def test_the_symbols_cursor_round_trips_through_encode_and_decode():
    cursor = encode_cursor(SYMBOLS_CURSOR, {"symbol": "AAA"})

    # exact bytes, not just a successful round trip: this is what catches a fields or renderers
    # entry naming the wrong key
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    assert raw == '{"symbol":"AAA"}'
    assert decode_cursor(SYMBOLS_CURSOR, cursor) == {"symbol": "AAA"}

    # bare str renders everything, so a NULL symbol column would encode to the literal cursor
    # value "None" and page against a symbol that does not exist
    with pytest.raises(TypeError) as excinfo:
        encode_cursor(SYMBOLS_CURSOR, {"symbol": None})
    assert str(excinfo.value) == "symbol renders a str, not NoneType"


def test_the_symbols_cursor_refuses_a_date_window_rather_than_comparing_a_string_to_a_date():
    cursor = encode_cursor(SYMBOLS_CURSOR, {"symbol": "AAA"})

    # both sides, because the two window checks are separate raise sites
    for window in ({"start": date(2026, 1, 1)}, {"end": date(2026, 1, 1)}):
        with pytest.raises(CursorWindowMisuse) as excinfo:
            decode_cursor(SYMBOLS_CURSOR, cursor, **window)
        assert "has no date window" in str(excinfo.value)
        # the assertion that survives the two window checks ever being moved inside decode_cursor's
        # own `except (ValueError, TypeError)`, which would turn this programming error into a
        # 400 unparsable_ts and page from a cursor nothing validated. The class is what this pins,
        # not the status: over HTTP a plausible-but-wrong window_bounds answers 500 internal too.
        assert not isinstance(excinfo.value, (ValueError, TypeError))


def test_a_cursor_carrying_a_urlsafe_alphabet_character_decodes_rather_than_being_refused():
    # synthetic, and it has to be: base64 emits "-" or "_" only from group4 = b2 & 63, which needs
    # a payload byte of ">", "?", "~" or 0x7f at an index congruent to 2 mod 3 -- and the 11-byte
    # JSON prefix puts those at symbol[0] and symbol[3], where no ticker over [A-Z0-9.] and no ISO
    # date can reach one. No payload this project can produce ever exercises the altchars argument,
    # which is why it is unreachable rather than merely unobserved -- do not delete this test.
    cursor = encode_cursor(SYMBOLS_CURSOR, {"symbol": "?ABC"})
    assert cursor == "eyJzeW1ib2wiOiI_QUJDIn0="
    assert decode_cursor(SYMBOLS_CURSOR, cursor) == {"symbol": "?ABC"}
