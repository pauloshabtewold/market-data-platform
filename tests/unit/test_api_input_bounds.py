from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic_core import PydanticCustomError

from api.main import create_app
from api.routes import _reject_a_window_bound_that_is_not_a_calendar_date, _reject_numeric_overflow

# unreachable and never entered as a context manager: every case here is decided by the
# validation tier, which answers before the lifespan would open a pool
DEAD_DSN = "postgresql://nobody:nobody@127.0.0.1:1/none"
_WINDOW = {"start": "2026-04-01", "end": "2026-04-02"}


def _client():
    # raise_server_exceptions=False: the default client re-raises an unhandled endpoint exception
    # out of the call instead of returning it, and reaching the never-opened pool raises one
    return TestClient(create_app(dsn=DEAD_DSN), raise_server_exceptions=False)


def test_min_move_pct_within_postgres_numeric_bounds_reaches_the_route():
    # 200-class here means the value passed validation and the handler tried to acquire a
    # connection from the never-opened pool, which is what turns into the 500 below -- not a 4xx.
    # The zeros are the edge a digit count gets wrong: Postgres stores a zero with no digits before
    # the point, so it reads 0E+131072 as 0 and refuses a zero's exponent only past its own limit
    client = _client()
    zeros = (
        "0E+131072",
        "-0E+131072",
        "0.0E+131073",
        "0E+1073741823",
        "-0E+1073741823",
        "0E-16383",
        "-0.0E-16382",
    )
    for value in ("1E+131071", "1e-16383", "9.999E+131071", *zeros):
        response = client.get(
            "/analytics/largest-moves", params={**_WINDOW, "min_move_pct": value}
        )
        assert response.status_code == 500, value
        assert response.json()["error"]["code"] == "internal"


def test_min_move_pct_past_postgres_numeric_bounds_is_a_four_hundred():
    client = _client()
    too_many_decimals = "0." + "1" + "0" * 16383
    zeros = ("0E+1073741824", "-0E+1073741824", "0.00E+1073741826", "0E-16384", "-0.0E-16383")
    for value in ("1E+131072", "10E+131071", "1e-16384", too_many_decimals, *zeros):
        response = client.get(
            "/analytics/largest-moves", params={**_WINDOW, "min_move_pct": value}
        )
        assert response.status_code == 400, value
        body = response.json()["error"]
        assert body["code"] == "invalid_params"
        assert body["detail"]["errors"] == [
            {"parameter": "min_move_pct", "location": "query", "type": "numeric_out_of_range"}
        ]


def test_a_131073_digit_min_move_pct_is_out_of_range():
    # httpx refuses to build a request whose URL exceeds 65536 characters, so this one case is
    # driven at the validator directly rather than through TestClient -- the same digits-before
    # count _reject_numeric_overflow computes for every other case in this file
    with pytest.raises(PydanticCustomError) as excinfo:
        _reject_numeric_overflow(Decimal("1" * 131_073))
    assert excinfo.value.type == "numeric_out_of_range"
    # and the message, which is asserted here because here is the only place it is readable: the
    # validation handler answers with INVALID_PARAMS_MESSAGE and publishes the slug above in
    # detail.errors, so this sentence never reaches a client and nothing else could pin it
    assert str(excinfo.value) == "min_move_pct is out of range for a Postgres numeric"


def test_min_move_pct_refuses_negative_and_non_finite_values_before_counting_digits():
    # a negative is refused for its sign whatever its size, so one past Postgres's range reads the
    # published minimum rather than numeric_out_of_range: Query(ge=0) sits ahead of AfterValidator
    client = _client()
    for value in ("-1", "-1E+131072", "-1e-16384", "-1E+1073741824"):
        response = client.get(
            "/analytics/largest-moves", params={**_WINDOW, "min_move_pct": value}
        )
        assert response.status_code == 400, value
        assert response.json()["error"]["detail"]["errors"] == [
            {"parameter": "min_move_pct", "location": "query", "type": "greater_than_equal"}
        ], value

    # the finite-number check is pydantic's own for Decimal, and it refuses before either of the two
    for value in ("NaN", "Infinity", "-Infinity"):
        response = client.get(
            "/analytics/largest-moves", params={**_WINDOW, "min_move_pct": value}
        )
        assert response.status_code == 400, value
        assert response.json()["error"]["detail"]["errors"] == [
            {"parameter": "min_move_pct", "location": "query", "type": "finite_number"}
        ], value
    # and reaching the validator directly they are no range to judge, since Postgres numeric reads
    # all three -- returned rather than failing on arithmetic with a non-numeric exponent
    for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        assert _reject_numeric_overflow(value) is value


# every endpoint that takes a window, with the rest of its parameters legal
_WINDOWED = (
    "/symbols/AAA/bars",
    "/symbols/AAA/daily",
    "/analytics/volatility?symbol=AAA",
    "/analytics/gaps?symbol=AAA",
    "/analytics/largest-moves",
)
# each of these is accepted for a date field by pydantic when its wall-clock time is midnight, and
# the offset is then discarded in favour of the literal calendar date: 2026-04-01T00:00:00+12:00 is
# the instant 2026-03-31T12:00:00Z, so the window served would be a day away from the one named.
# EVERY separator pydantic accepts is driven, not the obvious one: the set is t, T, _ and a space,
# measured by feeding a date adapter all of string.printable, and a rule written from T and a space
# alone leaves the other two reinterpreted in silence
_DATE_TIME_SEPARATORS = ("T", "t", "_", " ")
_BOUNDS_CARRYING_A_TIME = tuple(
    f"2026-04-01{separator}00:00:00{offset}"
    for separator in _DATE_TIME_SEPARATORS
    for offset in ("", "+12:00", "-05:00", "Z")
)


def _refused_bound(client, url, name, bound):
    # params= and never an interpolated query string: a raw `+` in a query string decodes to a SPACE,
    # so writing start=2026-04-01T00:00:00+12:00 into the URL delivers 2026-04-01T00:00:00 12:00 --
    # which is not the value under test, and is the one spelling every finding here exhibited
    other = "end" if name == "start" else "start"
    base, _, query = url.partition("?")
    params = {name: bound, other: "2026-04-01" if other == "start" else "2026-04-02"}
    if query:
        key, _, value = query.partition("=")
        params[key] = value
    response = client.get(base, params=params)
    assert response.status_code == 400, (url, name, bound, response.text)
    detail = response.json()["error"]["detail"]
    assert detail["reason"] == "invalid_parameter", (url, name, bound)
    return detail["errors"]


def test_a_window_bound_carrying_a_time_or_an_offset_is_refused_on_every_endpoint():
    client = _client()
    for url in _WINDOWED:
        for bound in _BOUNDS_CARRYING_A_TIME:
            for name in ("start", "end"):
                assert _refused_bound(client, url, name, bound) == [
                    {"parameter": name, "location": "query", "type": "date_carries_a_time"}
                ], (url, name, bound)


# A timestamp is the second shape pydantic accepts for a date field: seconds, or milliseconds above
# its own 2e10 threshold, with or without a sign or a fractional part, accepted when the instant is
# exactly UTC midnight. The published schema says `format: date`, which admits none of these, so a
# client generated from it cannot send one
_BOUNDS_THAT_ARE_TIMESTAMPS = (
    "1774915200",
    "+1774915200",
    "1774915200.0",
    "1774915200000",
    "0",
    "-2208988800",
)
# Values pydantic itself REFUSES, every one of which the rule must leave alone. These are the cases a
# pattern-matched rule claims and this one must not: a month of 13, a day of 32, an hour of 24, a
# separator with nothing after it, a date that is not one, and the four whitespace and NUL paddings
# that a strip() would have folded onto a legal shape
_BOUNDS_THE_RULE_MUST_NOT_CLAIM = (
    "2026-13-01T00:00:00",
    "2026-04-32 00:00:00",
    "0000-00-00T00:00:00",
    "9999-99-99T",
    "2026-04-01t",
    "2026-04-01Tnonsense",
    "2026-04-01T24:00:00+12:00",
    " 2026-04-01T00:00:00+12:00",
    "2026-04-01T00:00:00+12:00 ",
    "\t2026-04-01T00:00:00+12:00",
    "2026-04-01T00:00:00+12:00\x00",
    "2026/04/01",
    "nope",
    "",
    "2026-04-01x00:00:00",
    "20260401T000000+1200",
    " 2026-04-01",
    "2026-4-1",
)


def test_every_bound_under_test_reaches_the_service_as_it_is_written():
    # the delivery itself, because four of the sixteen time-carrying bounds carry a `+` and a query
    # string decodes one to a SPACE: a test that interpolates them into the URL drives
    # 2026-04-01T00:00:00 12:00 in place of the positive-offset spelling every finding in this area
    # exhibited, and a rule matching on the separator alone claims the mangled value too -- so the
    # test and the rule were wrong in a way that cancelled out
    client = _client()
    for bound in (*_BOUNDS_CARRYING_A_TIME, *_BOUNDS_THAT_ARE_TIMESTAMPS):
        response = client.get("/symbols/AAA/bars", params={"start": bound, "end": "2026-04-02"})
        delivered = parse_qs(urlparse(str(response.request.url)).query)["start"][0]
        assert delivered == bound, (bound, delivered)


def test_a_window_bound_written_as_a_timestamp_is_refused_on_every_endpoint():
    client = _client()
    for url in _WINDOWED:
        for bound in _BOUNDS_THAT_ARE_TIMESTAMPS:
            for name in ("start", "end"):
                assert _refused_bound(client, url, name, bound) == [
                    {
                        "parameter": name,
                        "location": "query",
                        "type": "date_is_not_a_calendar_date",
                    }
                ], (url, name, bound)


def test_a_plain_calendar_bound_still_reaches_the_handler_and_other_bad_dates_keep_their_own_slug():
    # the two refusals above are scoped to the two shapes pydantic would otherwise accept and
    # misread: a plain date passes validation and reaches the never-opened pool, which is the 500
    # below, and a date malformed in any OTHER way keeps pydantic's own slug rather than either of
    # theirs. That half is what makes the rule a statement about what pydantic accepts instead of a
    # pattern over what a bad date looks like, so it is driven on every case that merely resembles
    # one -- including the paddings a strip() folds onto a legal shape
    client = _client()
    for url in _WINDOWED:
        joiner = "&" if "?" in url else "?"
        legal = client.get(f"{url}{joiner}start=2026-04-01&end=2026-04-02")
        assert legal.status_code == 500, (url, legal.text)
        assert legal.json()["error"]["code"] == "internal", url
    for value in _BOUNDS_THE_RULE_MUST_NOT_CLAIM:
        response = client.get("/symbols/AAA/bars", params={"start": value, "end": "2026-04-02"})
        assert response.status_code == 400, (value, response.text)
        slug = response.json()["error"]["detail"]["errors"][0]["type"]
        assert slug == "date_from_datetime_parsing", (value, slug)


def test_both_window_bound_refusals_say_what_they_refused():
    # asserted here because here is the only place either sentence is readable: the validation
    # handler answers with INVALID_PARAMS_MESSAGE and publishes the slug alone in detail.errors, so
    # neither message ever reaches a client and nothing else could pin it
    for value, slug, message in (
        (
            "2026-04-01T00:00:00+12:00",
            "date_carries_a_time",
            "a window bound is a calendar date (YYYY-MM-DD); a time or an offset on one cannot be"
            " honoured, so it is refused rather than dropped",
        ),
        (
            "1774915200",
            "date_is_not_a_calendar_date",
            "a window bound is a calendar date (YYYY-MM-DD); a timestamp is read as a different day"
            " than it names, and the published schema admits no other spelling",
        ),
    ):
        with pytest.raises(PydanticCustomError) as excinfo:
            _reject_a_window_bound_that_is_not_a_calendar_date(value)
        assert excinfo.value.type == slug, value
        assert str(excinfo.value) == message, value
    # and a legal bound is returned unchanged rather than reparsed
    assert _reject_a_window_bound_that_is_not_a_calendar_date("2026-04-01") == "2026-04-01"


def test_every_type_pydantic_accepts_for_a_date_is_judged_and_not_only_the_string_one():
    # a query parameter is always a str over the wire, so these are reachable from an in-process
    # caller alone -- but pydantic misreads bytes and an offset-aware datetime exactly as it misreads
    # the text forms, and a guard scoped to str leaves the same defect one type over
    refuse = _reject_a_window_bound_that_is_not_a_calendar_date
    for value, slug in (
        (b"2026-04-01t00:00:00+12:00", "date_carries_a_time"),
        (b"1774915200", "date_is_not_a_calendar_date"),
        (1774915200, "date_is_not_a_calendar_date"),
        (1774915200.0, "date_is_not_a_calendar_date"),
        (True, "date_is_not_a_calendar_date"),
        (datetime(2026, 4, 1, 0, 0, tzinfo=timezone(timedelta(hours=12))), "date_carries_a_time"),
        (datetime(2026, 4, 1, 2, 30), "date_carries_a_time"),
    ):
        with pytest.raises(PydanticCustomError) as excinfo:
            refuse(value)
        assert excinfo.value.type == slug, value
    # and the ones that are already the value they mean pass through untouched
    assert refuse(b"2026-04-01") == "2026-04-01"
    assert refuse(date(2026, 4, 1)) == date(2026, 4, 1)
    assert refuse(datetime(2026, 4, 1, 0, 0)) == datetime(2026, 4, 1, 0, 0)
    # and a value no reading can decode is left for pydantic to refuse under its own slug
    assert refuse(b"\xff\xfe") == b"\xff\xfe"
    assert refuse(object) is object
