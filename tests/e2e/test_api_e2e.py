import json
import math
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import pytest

from config import settings

_UNKNOWN_SYMBOL = "ZZZZ-NOT-A-SYMBOL"
_BAD_CURSOR = "not-a-cursor"
_FOUR_PLACES = Decimal("0.0001")
_SIX_PLACES = Decimal("0.000001")
_TWO_PLACES = Decimal("0.01")
_NEW_YORK = ZoneInfo("America/New_York")
# every NYSE session opens at 09:30 New York time; a half day moves only the close
_SESSION_OPEN = time(9, 30)
# a symbol has at most one minute bar per minute, which bounds any walk over one day without
# trusting the service under test to end it
_MINUTES_PER_DAY = 1440
# bounds a /symbols walk at BARS_PAGE_MAX against a service that never terminates a cursor
_SYMBOLS_PAGE_BUDGET = 100
# enough sessions for several gaps and several hundred returns, few enough to recompute cheaply
_SUB_WINDOW_SESSIONS = 8
# the wire carries each value as a binary double, which moves one of more than fifteen significant
# digits by about 1e-16 of itself, and Postgres divides numerics to sixteen significant digits: this
# absorbs both and stays far below any rounding step published, even on a six-figure price
_RELATIVE_SLACK = Decimal("1e-12")
_ABSOLUTE_SLACK = Decimal("1e-12")

_ROUTES = {
    "/health",
    "/symbols",
    "/symbols/{symbol}/bars",
    "/symbols/{symbol}/daily",
    "/analytics/volatility",
    "/analytics/gaps",
    "/analytics/largest-moves",
}


def _page(response, label) -> dict:
    # status and envelope before anything reads the body, so a refusal fails naming its status and
    # path rather than as a KeyError on data
    assert response.status_code == 200, (label, response.status_code, response.text[:200])
    # every JSON fraction as a Decimal: a float keeps only the digits a double holds, and the
    # recomputations below compare against values Postgres rounded in decimal
    body = json.loads(response.text, parse_float=Decimal)
    assert set(body) == {"data", "next_cursor"}, label
    return body


def _refusal(response, status: int, code: str, label) -> dict:
    assert response.status_code == status, (label, response.status_code, response.text[:200])
    body = response.json()
    assert set(body) == {"error"}, label
    error = body["error"]
    assert set(error) == {"code", "message", "detail"}, label
    assert error["code"] == code, label
    assert isinstance(error["message"], str) and error["message"], label
    assert isinstance(error["detail"], dict), label
    return error


def _ts_key(row: dict) -> datetime:
    # parsed rather than compared as text: ISO strings sort in time order only while every value
    # carries the same precision, and nothing about a later database promises that
    return datetime.fromisoformat(row["ts"])


def _ts_symbol_key(row: dict) -> tuple:
    return _ts_key(row), row["symbol"]


def _dec(value) -> Decimal:
    # a whole-dollar price arrives as a JSON integer and every other one as a fraction
    return value if isinstance(value, Decimal) else Decimal(value)


def _slack(value: Decimal) -> Decimal:
    return abs(value) * _RELATIVE_SLACK + _ABSOLUTE_SLACK


def _publishes_rounded(published, exact, places: Decimal) -> bool:
    # Postgres round() takes a tie away from zero, which is ROUND_HALF_UP; rounding both ends of the
    # slack lets a value sitting on a rounding boundary publish as either neighbour
    if exact is None or published is None:
        return exact is None and published is None
    low = (exact - _slack(exact)).quantize(places, rounding=ROUND_HALF_UP)
    high = (exact + _slack(exact)).quantize(places, rounding=ROUND_HALF_UP)
    return low <= _dec(published) <= high


def _stddev_samp(values: list):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)).sqrt()


def _percentile_disc(ordered: list, fraction: Decimal) -> Decimal:
    # Postgres returns the first value whose position reaches the fraction of the row count
    return ordered[max(math.ceil(fraction * len(ordered)), 1) - 1]


def _assert_strictly_ascending(keys: list, label) -> None:
    # strict, because every key these endpoints order by is unique, so a repeated row is a defect
    assert all(a < b for a, b in zip(keys, keys[1:])), label


def _cursor_endpoints(window: dict, symbol: str):
    # the routes that declare a cursor, shared so the tampered-cursor and limit-over-cap cases test
    # one set rather than two lists that can drift apart
    return [
        ("/symbols", {}, settings.BARS_PAGE_MAX),
        (f"/symbols/{symbol}/bars", window, settings.BARS_PAGE_MAX),
        (f"/symbols/{symbol}/daily", window, settings.AGG_PAGE_MAX),
        ("/analytics/largest-moves", window, settings.AGG_PAGE_MAX),
    ]


def _page_budget(max_rows: int, limit: int) -> int:
    return max_rows // limit + 2


def _walk(client, path: str, params: dict, limit: int, max_pages: int) -> tuple[list, dict]:
    # every page to the end, returned with the request that produced the last one so that request
    # can be repeated at exactly the size of its answer
    rows: list = []
    request_params = {**params, "limit": limit}
    for page_number in range(max_pages):
        page = _page(client.get(path, params=request_params), path)
        # a cursor promises a next page, so an empty page behind one is a full last page the
        # service miscounted as a partial one
        assert page_number == 0 or page["data"], (path, "a next_cursor led to an empty page")
        rows.extend(page["data"])
        if page["next_cursor"] is None:
            return rows, {"params": request_params, "data": page["data"]}
        request_params = {**params, "limit": limit, "cursor": page["next_cursor"]}
    pytest.fail(f"{path}: no last page within {max_pages} pages")


def _assert_an_exactly_full_last_page_ends_the_walk(client, path: str, last: dict) -> None:
    assert last["data"], (path, "the walk returned no rows to repeat")
    params = {**last["params"], "limit": len(last["data"])}
    page = _page(client.get(path, params=params), path)
    assert page["data"] == last["data"], path
    # _page has already asserted the key is present, so this reads present-and-null
    assert page["next_cursor"] is None, (path, "an exactly full last page handed out a cursor")


def _served_range(client) -> tuple[date, date]:
    # the service's own ingested range, from the refusal that publishes it: a deployment may serve a
    # narrower range than this machine's INGEST_START, and only its answer says which. An unknown
    # symbol on purpose: this must be a 422 from the range tier, never a 404 from a lookup
    start = settings.INGEST_START - timedelta(days=10)
    end = settings.INGEST_START - timedelta(days=5)
    response = client.get(
        f"/symbols/{_UNKNOWN_SYMBOL}/bars",
        params={"start": start.isoformat(), "end": end.isoformat()},
    )
    error = _refusal(response, 422, "invalid_range", "/symbols/{symbol}/bars before INGEST_START")
    assert error["detail"]["reason"] == "outside_ingested_range"
    return date.fromisoformat(error["detail"]["min"]), date.fromisoformat(error["detail"]["max"])


def _cap_windows(client) -> tuple[dict, dict]:
    # one day over the cap and exactly at it, both inside the range the service itself reports and
    # reaching E2E_END where that range allows, so each holds data from the configured window
    served_min, served_max = _served_range(client)
    over_days = timedelta(days=settings.AGG_MAX_WINDOW_DAYS + 1)
    end = min(served_max, max(settings.E2E_END, served_min + over_days))
    start = end - over_days
    if start < served_min:
        pytest.fail(
            f"the served range {served_min}..{served_max} is shorter than AGG_MAX_WINDOW_DAYS + 1 ="
            f" {over_days.days} days, so no over-the-cap window fits inside it",
            pytrace=False,
        )
    over = {"start": start.isoformat(), "end": end.isoformat()}
    at = {"start": (start + timedelta(days=1)).isoformat(), "end": end.isoformat()}
    return over, at


def _sub_window(daily_rows: list) -> dict:
    # the symbol's own first sessions rather than a calendar span, so every day in it is a session
    # that symbol traded, whatever E2E_START falls on
    days = [row["day"] for row in daily_rows[:_SUB_WINDOW_SESSIONS]]
    return {"start": days[0], "end": days[-1]}


def _assert_second_page_follows(
    client, path: str, params: dict, cap: int, key, order_key=None
) -> None:
    # order_key defaults to key: for a single-field cursor (ts or day) the two coincide and the
    # field has no ties, so ">=" and uniqueness together are as strong as strict ordering. For the
    # universe cursor, order_key is passed as ts alone -- the tie-break field is symbol, and the
    # database's collation is not Python's string order (BF.B sorts after BFAM in en_US.utf8, before
    # it in codepoint order), so only ts may be compared as an order and symbol only as identity
    order_key = order_key or key
    limit = min(50, cap)
    first = _page(client.get(path, params={**params, "limit": limit}), path)
    assert first["next_cursor"] is not None, path
    second = _page(
        client.get(path, params={**params, "limit": limit, "cursor": first["next_cursor"]}), path
    )
    assert second["data"], path
    boundary = order_key(first["data"][-1])
    seen = {key(row) for row in first["data"]}
    for row in second["data"]:
        assert order_key(row) >= boundary, path
        assert key(row) not in seen, path


def _collect_prefix(
    client, path: str, params: dict, page_limit: int, min_rows: int, max_pages=60
) -> list:
    # walks with a small page size until either the service says there is no more, or enough rows
    # are in hand to compare against a single larger page -- never the whole window
    rows: list = []
    cursor = None
    for _ in range(max_pages):
        request_params = {**params, "limit": page_limit}
        if cursor is not None:
            request_params["cursor"] = cursor
        page = _page(client.get(path, params=request_params), path)
        rows.extend(page["data"])
        cursor = page["next_cursor"]
        if cursor is None or len(rows) >= min_rows:
            return rows
    pytest.fail(f"{path}: did not reach {min_rows} rows or a last page within {max_pages} pages")


def _assert_walk_matches_one_larger_page(
    client, path: str, params: dict, small_limit: int, big_limit: int, cap: int
) -> None:
    big_limit = min(big_limit, cap)
    small_limit = min(small_limit, big_limit)
    walked = _collect_prefix(client, path, params, small_limit, big_limit)
    big = _page(client.get(path, params={**params, "limit": big_limit}), path)
    # a walk that stops short of what one bigger page actually holds is itself the defect -- a
    # cursor the service treats as final while more rows exist, whether or not the rows it did
    # return happen to match
    assert len(walked) >= min(big_limit, len(big["data"])), (path, len(walked), len(big["data"]))
    n = min(len(walked), len(big["data"]))
    assert n > 0, path
    assert walked[:n] == big["data"][:n], path


@pytest.fixture(scope="module")
def daily_rows(client, window, symbols) -> list:
    # the first configured symbol's sessions in the window, read from /daily rather than assumed:
    # nothing promises E2E_START or E2E_END is a session, or that the symbol traded on either
    span = (settings.E2E_END - settings.E2E_START).days + 1
    rows, _ = _walk(
        client,
        f"/symbols/{symbols[0]}/daily",
        window,
        settings.AGG_PAGE_MAX,
        _page_budget(span, settings.AGG_PAGE_MAX),
    )
    assert len(rows) >= 3, (
        f"{symbols[0]} has {len(rows)} sessions in {window}; the windowed cases need at least three"
    )
    return rows


@pytest.fixture(scope="module")
def universe(client) -> tuple[list, dict]:
    return _walk(client, "/symbols", {}, settings.BARS_PAGE_MAX, _SYMBOLS_PAGE_BUDGET)


def test_health_answers_ok_with_a_real_version(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"] and body["version"] != "unknown"


def test_healths_version_matches_the_generated_documentations_version(client):
    health = client.get("/health").json()
    document = client.get("/openapi.json").json()
    assert health["version"] == document["info"]["version"]


def test_every_data_endpoint_answers_the_envelope_inside_the_configured_window(
    client, window, symbols
):
    symbol = symbols[0]
    cases = [
        ("/symbols", {}),
        (f"/symbols/{symbol}/bars", window),
        (f"/symbols/{symbol}/daily", window),
        ("/analytics/volatility", {**window, "symbol": symbol}),
        ("/analytics/largest-moves", window),
    ]
    for path, params in cases:
        body = _page(client.get(path, params=params), path)
        assert isinstance(body["data"], list) and body["data"], path
        assert body["next_cursor"] is None or isinstance(body["next_cursor"], str), path

    # gaps answers one aggregate row on any window, loaded or not, so a non-empty check could not
    # fail there: the shape is asserted, and a gap count above zero is what shows the window loaded
    gaps = _page(
        client.get("/analytics/gaps", params={**window, "symbol": symbol}), "/analytics/gaps"
    )
    assert len(gaps["data"]) == 1
    assert gaps["data"][0]["gaps"] > 0
    assert gaps["next_cursor"] is None


def test_an_unknown_symbol_is_refused_with_the_one_error_shape(client, window):
    cases = [
        (f"/symbols/{_UNKNOWN_SYMBOL}/bars", window),
        (f"/symbols/{_UNKNOWN_SYMBOL}/daily", window),
        ("/analytics/volatility", {**window, "symbol": _UNKNOWN_SYMBOL}),
        ("/analytics/gaps", {**window, "symbol": _UNKNOWN_SYMBOL}),
    ]
    for path, params in cases:
        error = _refusal(client.get(path, params=params), 404, "unknown_symbol", path)
        assert error["detail"] == {"reason": "unknown_symbol", "symbol": _UNKNOWN_SYMBOL}, path


def test_an_inverted_window_is_refused_before_the_symbol_is_looked_up(client, window):
    # an unknown symbol on purpose: a handler that looked the symbol up first would answer 404, so
    # only the range tier running before any checkout gives this 422. The end is a day before the
    # start so that a one-day window inverts too
    before_start = (settings.E2E_START - timedelta(days=1)).isoformat()
    inverted = {"start": window["end"], "end": before_start}
    response = client.get(f"/symbols/{_UNKNOWN_SYMBOL}/bars", params=inverted)
    error = _refusal(response, 422, "invalid_range", "/symbols/{symbol}/bars")
    assert error["detail"]["reason"] == "start_after_end"


def test_bars_refuses_a_window_before_the_ingested_range_ahead_of_any_symbol_lookup(client):
    served_min, served_max = _served_range(client)
    assert served_min <= settings.E2E_START <= settings.E2E_END <= served_max, (
        f"the service serves {served_min}..{served_max}, which does not hold the configured window"
    )


def test_the_ingested_range_is_enforced_at_both_edges_ahead_of_any_symbol_lookup(client):
    served_min, served_max = _served_range(client)
    one_day = timedelta(days=1)
    # each edge day itself is inside, so an unknown symbol gets past the range tier to its 404; one
    # day beyond either edge must stop at the 422 instead
    inside = [(served_min, served_min), (served_max, served_max)]
    outside = [
        (served_min - one_day, served_min),
        (served_max, served_max + one_day),
        (served_max + one_day, served_max + 10 * one_day),
    ]
    for path in (f"/symbols/{_UNKNOWN_SYMBOL}/bars", f"/symbols/{_UNKNOWN_SYMBOL}/daily"):
        for start, end in inside:
            params = {"start": start.isoformat(), "end": end.isoformat()}
            _refusal(client.get(path, params=params), 404, "unknown_symbol", (path, params))
        for start, end in outside:
            params = {"start": start.isoformat(), "end": end.isoformat()}
            error = _refusal(client.get(path, params=params), 422, "invalid_range", (path, params))
            assert error["detail"]["reason"] == "outside_ingested_range", (path, params)

    past_the_end = {"start": served_max.isoformat(), "end": (served_max + one_day).isoformat()}
    error = _refusal(
        client.get("/analytics/largest-moves", params=past_the_end),
        422,
        "invalid_range",
        "/analytics/largest-moves past the served range",
    )
    assert error["detail"]["reason"] == "outside_ingested_range"


def test_a_window_over_the_cap_is_refused_on_daily_and_the_analytics_endpoints(client, symbols):
    params, _ = _cap_windows(client)
    symbol = symbols[0]
    cases = [
        (f"/symbols/{symbol}/daily", params),
        ("/analytics/volatility", {**params, "symbol": symbol}),
        ("/analytics/gaps", {**params, "symbol": symbol}),
        ("/analytics/largest-moves", params),
    ]
    for path, case_params in cases:
        error = _refusal(client.get(path, params=case_params), 422, "invalid_range", path)
        assert error["detail"]["reason"] == "window_too_long", path


def test_a_window_of_exactly_the_cap_is_accepted_on_daily_and_the_analytics_endpoints(
    client, symbols
):
    _, params = _cap_windows(client)
    symbol = symbols[0]
    cases = [
        (f"/symbols/{symbol}/daily", {**params, "limit": 1}),
        ("/analytics/volatility", {**params, "symbol": symbol}),
        ("/analytics/gaps", {**params, "symbol": symbol}),
        ("/analytics/largest-moves", {**params, "limit": 1}),
    ]
    for path, case_params in cases:
        _page(client.get(path, params=case_params), (path, "a window of exactly the cap"))


def test_bars_has_no_window_length_cap_where_the_analytics_endpoints_do(client, symbols):
    # spec line 630: /bars takes the whole ingested range. limit=1 keeps this cheap regardless of
    # how wide the window is -- the point is acceptance, not the row count
    over, _ = _cap_windows(client)
    body = _page(
        client.get(f"/symbols/{symbols[0]}/bars", params={**over, "limit": 1}),
        "/bars over the analytics cap",
    )
    assert body["data"]


def test_bars_accepts_its_own_page_max_and_defaults_to_its_own_page_length(client, window, symbols):
    symbol = symbols[0]
    at_cap = _page(
        client.get(f"/symbols/{symbol}/bars", params={**window, "limit": settings.BARS_PAGE_MAX}),
        "/bars at BARS_PAGE_MAX",
    )
    assert at_cap["data"]

    unspecified = _page(client.get(f"/symbols/{symbol}/bars", params=window), "/bars default page")
    assert len(unspecified["data"]) == settings.BARS_PAGE_DEFAULT, (
        "the configured window must hold more than BARS_PAGE_DEFAULT bars for this to be meaningful"
    )
    assert unspecified["next_cursor"] is not None


def test_a_tampered_cursor_is_refused_on_every_endpoint_that_declares_one(client, window, symbols):
    for path, params, _cap in _cursor_endpoints(window, symbols[0]):
        response = client.get(path, params={**params, "cursor": _BAD_CURSOR})
        _refusal(response, 400, "invalid_cursor", path)


def test_a_cursor_from_another_window_is_refused_in_both_directions_on_every_windowed_endpoint(
    client, symbols, daily_rows
):
    first, second, third = (row["day"] for row in daily_rows[:3])
    # each cursor is page 1's at limit 1, so it points at its window's first session; the target
    # windows sit wholly after the early cursor and wholly before the late one
    early, late = {"start": first, "end": second}, {"start": second, "end": third}
    after_early, before_late = {"start": third, "end": third}, {"start": first, "end": first}
    for path in (
        f"/symbols/{symbols[0]}/bars",
        f"/symbols/{symbols[0]}/daily",
        "/analytics/largest-moves",
    ):
        cases = []
        for source, target, direction in (
            (early, after_early, "a cursor from an earlier window"),
            (late, before_late, "a cursor from a later window"),
        ):
            page = _page(client.get(path, params={**source, "limit": 1}), (path, source))
            assert page["next_cursor"] is not None, (path, source)
            cases.append((page["next_cursor"], target, direction))
        for cursor, target, direction in cases:
            error = _refusal(
                client.get(path, params={**target, "cursor": cursor}),
                400,
                "invalid_cursor",
                (path, direction),
            )
            assert error["detail"]["reason"] == "cursor_outside_window", (path, direction)


def test_a_limit_over_the_cap_is_refused_on_every_endpoint_that_declares_one(
    client, window, symbols
):
    for path, params, cap in _cursor_endpoints(window, symbols[0]):
        response = client.get(path, params={**params, "limit": cap + 1})
        error = _refusal(response, 400, "invalid_params", path)
        assert error["detail"]["reason"] == "limit_out_of_range", path


def test_volatility_and_gaps_silently_ignore_an_undeclared_limit_and_cursor(
    client, window, symbols
):
    # over the cap by construction: AGG_PAGE_MAX is the pair both handlers pass to the range tier,
    # and neither declares a limit a request could use to reach it
    params = {
        **window,
        "symbol": symbols[0],
        "limit": settings.AGG_PAGE_MAX + 1,
        "cursor": _BAD_CURSOR,
    }
    for path in ("/analytics/volatility", "/analytics/gaps"):
        body = _page(client.get(path, params=params), path)
        assert body["next_cursor"] is None, path


def test_a_bars_second_page_follows_the_cursor_without_repeating_a_row(client, window, symbols):
    _assert_second_page_follows(
        client, f"/symbols/{symbols[0]}/bars", window, settings.BARS_PAGE_MAX, _ts_key
    )


def test_a_largest_moves_second_page_follows_the_cursor_without_repeating_a_row(client, window):
    # order_key=_ts_key, not the (ts, symbol) tuple: the tie-break is the database's collation, and
    # Python string order is not that collation
    _assert_second_page_follows(
        client,
        "/analytics/largest-moves",
        window,
        settings.AGG_PAGE_MAX,
        _ts_symbol_key,
        order_key=_ts_key,
    )


def test_an_exactly_full_last_page_ends_the_walk_with_an_explicit_null_cursor(
    client, window, symbols, daily_rows, universe
):
    _, symbols_last = universe
    _assert_an_exactly_full_last_page_ends_the_walk(client, "/symbols", symbols_last)

    daily_path = f"/symbols/{symbols[0]}/daily"
    span = (settings.E2E_END - settings.E2E_START).days + 1
    _, daily_last = _walk(
        client, daily_path, window, settings.AGG_PAGE_MAX, _page_budget(span, settings.AGG_PAGE_MAX)
    )
    _assert_an_exactly_full_last_page_ends_the_walk(client, daily_path, daily_last)

    bars_path = f"/symbols/{symbols[0]}/bars"
    day = daily_rows[-1]["day"]
    _, bars_last = _walk(
        client,
        bars_path,
        {"start": day, "end": day},
        settings.BARS_PAGE_MAX,
        _page_budget(_MINUTES_PER_DAY, settings.BARS_PAGE_MAX),
    )
    _assert_an_exactly_full_last_page_ends_the_walk(client, bars_path, bars_last)


def test_the_generated_documentation_publishes_every_route_and_redoc_is_off(client):
    document = client.get("/openapi.json")
    assert document.status_code == 200
    assert _ROUTES <= set(document.json()["paths"])

    page = client.get("/docs")
    assert page.status_code == 200
    assert page.headers["content-type"].split(";")[0] == "text/html"

    _refusal(client.get("/redoc"), 404, "invalid_params", "/redoc")


def test_a_negative_min_move_pct_is_refused(client, window):
    response = client.get("/analytics/largest-moves", params={**window, "min_move_pct": -1})
    _refusal(response, 400, "invalid_params", "/analytics/largest-moves")


def test_largest_moves_default_threshold_matches_an_explicit_zero_and_zero_is_accepted(
    client, daily_rows
):
    day = daily_rows[0]["day"]
    day_window = {"start": day, "end": day, "limit": min(50, settings.AGG_PAGE_MAX)}
    default = _page(client.get("/analytics/largest-moves", params=day_window), "default threshold")
    explicit_zero = _page(
        client.get("/analytics/largest-moves", params={**day_window, "min_move_pct": 0}),
        "explicit min_move_pct=0",
    )
    assert default["data"], "default threshold"
    assert default["data"] == explicit_zero["data"]


def test_symbols_has_no_duplicate_symbols(client):
    # not Python string ascending: the database collation ignores punctuation (BF.B sorts after
    # BFAM), which codepoint order does not, so order is judged only against the service's own
    # larger page, in the small-limit walk test
    body = _page(client.get("/symbols", params={"limit": settings.BARS_PAGE_MAX}), "/symbols")
    names = [row["symbol"] for row in body["data"]]
    assert names
    assert len(set(names)) == len(names)


def test_symbols_active_filter_partitions_the_unfiltered_set_on_every_page(client, universe):
    everything, _ = universe
    assert everything
    # several pages per filtered walk, so a filter honoured on the first page alone shows
    limit = max(1, min(settings.BARS_PAGE_MAX, len(everything) // 4))
    for flag in (True, False):
        walked, _ = _walk(
            client, "/symbols", {"active": flag}, limit, _page_budget(len(everything), limit)
        )
        assert all(row["active"] is flag for row in walked), f"active={flag}"
        # the unfiltered order restricted to the flag: the partition, and the collation's order
        # without comparing symbols in Python
        assert walked == [row for row in everything if row["active"] is flag], f"active={flag}"


def test_a_bars_page_is_strictly_ascending_for_every_configured_symbol(client, window, symbols):
    for symbol in symbols:
        params = {**window, "limit": min(200, settings.BARS_PAGE_MAX)}
        body = _page(client.get(f"/symbols/{symbol}/bars", params=params), symbol)
        assert body["data"], symbol
        _assert_strictly_ascending([_ts_key(row) for row in body["data"]], symbol)


def test_a_daily_page_is_strictly_ascending_for_every_configured_symbol(client, window, symbols):
    for symbol in symbols:
        body = _page(client.get(f"/symbols/{symbol}/daily", params=window), symbol)
        assert body["data"], symbol
        _assert_strictly_ascending([row["day"] for row in body["data"]], symbol)


def test_a_largest_moves_page_has_non_decreasing_timestamps_and_every_row_clears_the_threshold(
    client, window
):
    # a positive threshold, because at the default of 0 every row clears it and the check could not
    # fail; 0.1 has fewer decimals than the projection keeps, so rounding cannot carry a qualifying
    # row below it
    threshold = Decimal("0.1")
    params = {**window, "limit": min(200, settings.AGG_PAGE_MAX), "min_move_pct": str(threshold)}
    body = _page(client.get("/analytics/largest-moves", params=params), "/analytics/largest-moves")
    assert body["data"]
    # ts non-decreasing and no duplicate (ts, symbol) key -- not "symbol ascending", which is a
    # Python string comparison the database collation does not agree with
    timestamps = [_ts_key(row) for row in body["data"]]
    assert all(a <= b for a, b in zip(timestamps, timestamps[1:])), "largest-moves"
    keys = [_ts_symbol_key(row) for row in body["data"]]
    assert len(set(keys)) == len(keys), "largest-moves"
    for row in body["data"]:
        assert set(row) == {"ts", "symbol", "open", "close", "move_pct"}, row
        assert abs(_dec(row["move_pct"])) >= threshold, row


def test_symbols_pages_walked_at_a_small_limit_concatenate_to_one_larger_page(client):
    _assert_walk_matches_one_larger_page(
        client, "/symbols", {}, small_limit=2, big_limit=5, cap=settings.BARS_PAGE_MAX
    )


def test_bars_pages_walked_at_a_small_limit_concatenate_to_one_larger_page(client, window, symbols):
    _assert_walk_matches_one_larger_page(
        client,
        f"/symbols/{symbols[0]}/bars",
        window,
        small_limit=7,
        big_limit=30,
        cap=settings.BARS_PAGE_MAX,
    )


def test_daily_pages_walked_at_a_small_limit_concatenate_to_one_larger_page(
    client, window, symbols
):
    _assert_walk_matches_one_larger_page(
        client,
        f"/symbols/{symbols[0]}/daily",
        window,
        small_limit=3,
        big_limit=10,
        cap=settings.AGG_PAGE_MAX,
    )


def test_largest_moves_pages_walked_at_a_small_limit_concatenate_to_one_larger_page(
    client, daily_rows
):
    # a single session, never the configured 90-day window: universe-wide rows are dense enough
    # that a whole-window walk would cost thousands of requests for no more assurance than one gives
    day = daily_rows[0]["day"]
    _assert_walk_matches_one_larger_page(
        client,
        "/analytics/largest-moves",
        {"start": day, "end": day},
        small_limit=2,
        big_limit=8,
        cap=settings.AGG_PAGE_MAX,
    )


def test_the_configured_windows_first_and_last_session_are_not_dropped_by_any_endpoint(
    client, window, symbols, daily_rows
):
    symbol = symbols[0]
    reported_first, reported_last = daily_rows[0]["day"], daily_rows[-1]["day"]

    for day, label in ((reported_first, "first"), (reported_last, "last")):
        day_window = {"start": day, "end": day}
        daily = _page(
            client.get(f"/symbols/{symbol}/daily", params=day_window),
            f"/daily on its own {label} day",
        )
        assert [row["day"] for row in daily["data"]] == [day], (
            f"/daily over the single day {day} does not report that session"
        )

        bars = _page(
            client.get(
                f"/symbols/{symbol}/bars", params={**day_window, "limit": settings.BARS_PAGE_MAX}
            ),
            f"/bars on daily's own {label} day",
        )
        assert bars["data"], f"/bars has no rows on daily's own {label} day {day}"
        assert all(_ts_key(row).date().isoformat() == day for row in bars["data"]), label

        moves = _page(
            client.get(
                "/analytics/largest-moves",
                params={**day_window, "limit": min(50, settings.AGG_PAGE_MAX)},
            ),
            f"/analytics/largest-moves on daily's own {label} day",
        )
        assert moves["data"], (
            f"/analytics/largest-moves has no rows on daily's own {label} day {day}"
        )
        assert all(_ts_key(row).date().isoformat() == day for row in moves["data"]), label

    # ground truth from a codepath the rollup never touches: a largest-moves row for the symbol on
    # the window's literal edge is a regular-session bar there, so the rollup must report that day
    # and not a neighbour. A /bars row would not do, since a pre- or post-market bar is no session
    for literal, reported, label in (
        (window["start"], reported_first, "first"),
        (window["end"], reported_last, "last"),
    ):
        edge = _page(
            client.get(
                "/analytics/largest-moves",
                params={"start": literal, "end": literal, "limit": settings.AGG_PAGE_MAX},
            ),
            f"/analytics/largest-moves at the window's literal {label} day",
        )
        if any(row["symbol"] == symbol for row in edge["data"]):
            assert reported == literal, f"daily dropped the window's {label} session"


def test_largest_moves_values_agree_with_bars_and_daily_for_one_symbol(
    client, symbols, daily_rows, universe
):
    symbol = symbols[0]
    daily_row = daily_rows[-1]
    day = daily_row["day"]
    day_window = {"start": day, "end": day}

    bars_rows, _ = _walk(
        client,
        f"/symbols/{symbol}/bars",
        day_window,
        settings.BARS_PAGE_MAX,
        _page_budget(_MINUTES_PER_DAY, settings.BARS_PAGE_MAX),
    )
    assert bars_rows, "/bars for the day"
    bars_by_ts = {row["ts"]: row for row in bars_rows}
    for row in bars_rows:
        o, h, lo, c = (_dec(row[k]) for k in ("open", "high", "low", "close"))
        assert h >= max(o, c), row["ts"]
        assert lo <= min(o, c), row["ts"]

    # universe-wide and one whole session -- min_move_pct=0 makes every regular-session bar of
    # every symbol a row, so this is the full day's worth of pages
    everything, _ = universe
    moves, moves_last = _walk(
        client,
        "/analytics/largest-moves",
        {**day_window, "min_move_pct": 0},
        settings.AGG_PAGE_MAX,
        _page_budget(len(everything) * _MINUTES_PER_DAY, settings.AGG_PAGE_MAX),
    )
    _assert_an_exactly_full_last_page_ends_the_walk(client, "/analytics/largest-moves", moves_last)
    moves_for_symbol = {row["ts"]: row for row in moves if row["symbol"] == symbol}
    assert moves_for_symbol, "no largest-moves rows for the configured symbol on its own last day"

    for ts, move_row in moves_for_symbol.items():
        assert ts in bars_by_ts, f"largest-moves named a ts /bars does not have: {ts}"
        bar_open, bar_close = _dec(bars_by_ts[ts]["open"]), _dec(bars_by_ts[ts]["close"])
        assert _publishes_rounded(move_row["open"], bar_open, _FOUR_PLACES), (ts, move_row)
        assert _publishes_rounded(move_row["close"], bar_close, _FOUR_PLACES), (ts, move_row)
        expected_move = (bar_close - bar_open) * 100 / bar_open
        assert _publishes_rounded(move_row["move_pct"], expected_move, _FOUR_PLACES), (ts, move_row)

    # the regular-session subset of /bars that day is exactly the set largest-moves names -- the
    # rollup's own OHLCV must agree with recomputing it from that subset alone
    regular_bars = [bars_by_ts[ts] for ts in sorted(moves_for_symbol, key=datetime.fromisoformat)]
    assert _dec(daily_row["open"]) == _dec(regular_bars[0]["open"])
    assert _dec(daily_row["close"]) == _dec(regular_bars[-1]["close"])
    assert _dec(daily_row["high"]) == max(_dec(r["high"]) for r in regular_bars)
    assert _dec(daily_row["low"]) == min(_dec(r["low"]) for r in regular_bars)
    assert daily_row["volume"] == sum(r["volume"] for r in regular_bars)
    assert daily_row["bars"] == len(regular_bars)


def test_gaps_and_volatility_totals_agree_with_the_daily_rollup_over_a_sub_window(
    client, symbols, daily_rows
):
    symbol = symbols[0]
    sub_window = _sub_window(daily_rows)

    daily, _ = _walk(
        client,
        f"/symbols/{symbol}/daily",
        sub_window,
        settings.AGG_PAGE_MAX,
        _page_budget(_SUB_WINDOW_SESSIONS, settings.AGG_PAGE_MAX),
    )
    n_days = len(daily)
    assert n_days >= 2, "the sub-window must hold at least two sessions to exercise the identity"

    gaps = _page(
        client.get("/analytics/gaps", params={**sub_window, "symbol": symbol}), "/analytics/gaps"
    )
    # 03_gaps.sql: one gap per rollup row that has a previous rollup row, over the same session
    # definition and the same window as /daily
    assert gaps["data"][0]["gaps"] == n_days - 1

    volatility = _page(
        client.get("/analytics/volatility", params={**sub_window, "symbol": symbol}),
        "/analytics/volatility",
    )
    assert volatility["data"]
    for row in volatility["data"]:
        assert set(row) == {
            "bucket_minute", "returns", "avg_minutes_per_return", "stddev_pct", "annualized_pct",
        }, row
    # 01_volatility.sql: one return per bar that has a previous bar in its own session, so the
    # total across every bucket is the daily rollup's own bar count minus one, per day
    total_returns = sum(row["returns"] for row in volatility["data"])
    expected_returns = sum(row["bars"] - 1 for row in daily)
    assert total_returns == expected_returns


def test_gaps_values_agree_with_the_daily_rollups_opens_and_prior_closes(
    client, symbols, daily_rows
):
    symbol = symbols[0]
    sub_window = _sub_window(daily_rows)
    daily, _ = _walk(
        client,
        f"/symbols/{symbol}/daily",
        sub_window,
        settings.AGG_PAGE_MAX,
        _page_budget(_SUB_WINDOW_SESSIONS, settings.AGG_PAGE_MAX),
    )
    # 03_gaps.sql pairs each rollup row with the one before it, never with a calendar neighbour
    gap_pcts = [
        (_dec(row["open"]) - _dec(prev["close"])) * 100 / _dec(prev["close"])
        for prev, row in zip(daily, daily[1:])
        if _dec(prev["close"]) != 0
    ]
    assert gap_pcts, "the sub-window must hold at least two sessions"

    body = _page(
        client.get("/analytics/gaps", params={**sub_window, "symbol": symbol}), "/analytics/gaps"
    )
    assert len(body["data"]) == 1
    published = body["data"][0]
    assert published["gaps"] == len(gap_pcts), published
    assert 0 <= published["gaps_spanning_a_skipped_session"] <= published["gaps"], published

    ordered = sorted(gap_pcts)
    expected = {
        "mean_pct": sum(gap_pcts) / len(gap_pcts),
        "stddev_pct": _stddev_samp(gap_pcts),
        "min_pct": ordered[0],
        "p25_pct": _percentile_disc(ordered, Decimal("0.25")),
        "median_pct": _percentile_disc(ordered, Decimal("0.50")),
        "p75_pct": _percentile_disc(ordered, Decimal("0.75")),
        "max_pct": ordered[-1],
    }
    for field, exact in expected.items():
        assert _publishes_rounded(published[field], exact, _FOUR_PLACES), (field, published, exact)

    # a gap inside the slack of zero may be counted under any sign, and only such a gap may
    near_zero = sum(1 for g in gap_pcts if abs(g) <= _ABSOLUTE_SLACK)
    up = sum(1 for g in gap_pcts if g > _ABSOLUTE_SLACK)
    down = sum(1 for g in gap_pcts if g < -_ABSOLUTE_SLACK)
    assert up <= published["gaps_up"] <= up + near_zero, (published, up, near_zero)
    assert down <= published["gaps_down"] <= down + near_zero, (published, down, near_zero)
    counted = published["gaps_up"] + published["gaps_down"] + published["gaps_flat"]
    assert counted == published["gaps"], published


def test_volatility_buckets_agree_with_a_recomputation_from_the_session_bars(
    client, symbols, daily_rows
):
    symbol = symbols[0]
    sub_window = _sub_window(daily_rows)
    daily, _ = _walk(
        client,
        f"/symbols/{symbol}/daily",
        sub_window,
        settings.AGG_PAGE_MAX,
        _page_budget(_SUB_WINDOW_SESSIONS, settings.AGG_PAGE_MAX),
    )
    first_day, last_day = (date.fromisoformat(sub_window[k]) for k in ("start", "end"))
    span_days = (last_day - first_day).days
    bars, _ = _walk(
        client,
        f"/symbols/{symbol}/bars",
        sub_window,
        settings.BARS_PAGE_MAX,
        _page_budget((span_days + 1) * _MINUTES_PER_DAY, settings.BARS_PAGE_MAX),
    )
    by_session_day: dict = {}
    for row in bars:
        by_session_day.setdefault(_ts_key(row).astimezone(_NEW_YORK).date(), []).append(row)

    returns: dict = {}
    for day_row in daily:
        day = date.fromisoformat(day_row["day"])
        opens_at = datetime.combine(day, _SESSION_OPEN, tzinfo=_NEW_YORK).astimezone(timezone.utc)
        # a session is contiguous from its open, so its bars are the first /daily-counted bars at or
        # after 09:30: the count places a half day's close without the calendar the API never serves
        from_open = sorted(
            (row for row in by_session_day.get(day, []) if _ts_key(row) >= opens_at), key=_ts_key
        )
        session = from_open[: day_row["bars"]]
        label = f"{symbol} session {day}"
        assert len(session) == day_row["bars"], label
        assert _dec(session[0]["open"]) == _dec(day_row["open"]), label
        assert _dec(session[-1]["close"]) == _dec(day_row["close"]), label
        assert max(_dec(r["high"]) for r in session) == _dec(day_row["high"]), label
        assert min(_dec(r["low"]) for r in session) == _dec(day_row["low"]), label
        assert sum(r["volume"] for r in session) == day_row["volume"], label

        for prev, row in zip(session, session[1:]):
            prev_close = _dec(prev["close"])
            if prev_close == 0:
                continue
            # half-hour buckets counted from the session's own open, never from a UTC hour
            minutes_from_open = (_ts_key(row) - opens_at) // timedelta(minutes=1)
            bucket = minutes_from_open // 30 * 30
            span = Decimal((_ts_key(row) - _ts_key(prev)) // timedelta(seconds=1)) / 60
            move = (_dec(row["close"]) - prev_close) * 100 / prev_close
            returns.setdefault(bucket, []).append((move, span))
    assert returns, "no session in the sub-window holds two bars"

    body = _page(
        client.get("/analytics/volatility", params={**sub_window, "symbol": symbol}),
        "/analytics/volatility",
    )
    published = body["data"]
    assert [row["bucket_minute"] for row in published] == sorted(returns), published

    factor_low, factor_high = Decimal(0), None
    for row in published:
        assert set(row) == {
            "bucket_minute", "returns", "avg_minutes_per_return", "stddev_pct", "annualized_pct",
        }, row
        moves = [move for move, _ in returns[row["bucket_minute"]]]
        spans = [span for _, span in returns[row["bucket_minute"]]]
        assert row["returns"] == len(moves), row
        mean_span = sum(spans) / len(spans)
        assert _publishes_rounded(row["avg_minutes_per_return"], mean_span, _TWO_PLACES), row
        stddev = _stddev_samp(moves)
        assert _publishes_rounded(row["stddev_pct"], stddev, _SIX_PLACES), (row, stddev)
        if stddev is None:
            assert row["annualized_pct"] is None, row
            continue
        if stddev == 0:
            assert _dec(row["annualized_pct"]) == 0, row
            continue
        annualized = _dec(row["annualized_pct"])
        half_step = _FOUR_PLACES / 2 + _slack(annualized)
        factor_low = max(factor_low, (annualized - half_step) / (stddev + _slack(stddev)))
        high = (annualized + half_step) / (stddev - _slack(stddev))
        factor_high = high if factor_high is None else min(factor_high, high)
    # whatever the annualisation factor is, one factor must scale every bucket's own deviation
    assert factor_high is None or factor_low <= factor_high, (factor_low, factor_high)
