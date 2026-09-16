from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from config import settings

_UNKNOWN_SYMBOL = "ZZZZ-NOT-A-SYMBOL"
_BAD_CURSOR = "not-a-cursor"
_FOUR_PLACES = Decimal("0.0001")
# a universe-wide day at min_move_pct=0 is every regular-session bar of every symbol; AGG_PAGE_MAX
# per request bounds how many pages that takes, and this bounds the walk itself against a service
# that never terminates a cursor
_FULL_DAY_PAGE_BUDGET = 200

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
    body = response.json()
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
    # via str(), never Decimal(float(...)) -- these values already arrived as JSON numbers with a
    # handful of decimal digits, and str() is what recovers exactly those digits rather than a
    # binary float's full expansion
    return Decimal(str(value))


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


def _oversized_window() -> dict:
    # anchored on E2E_END and never on INGEST_START: the deployed service sets its own INGEST_START
    # to the copied hot window, where a span starting at this machine's INGEST_START is refused
    # outside_ingested_range before the length rule is reached. It still needs E2E_END to sit at
    # least AGG_MAX_WINDOW_DAYS + 1 days after the deployed hot window's first day; a
    # configuration leaving no room for an over-the-cap window fails here by name, and no anchor
    # avoids that
    end = settings.E2E_END
    start = end - timedelta(days=settings.AGG_MAX_WINDOW_DAYS + 1)
    return {"start": start.isoformat(), "end": end.isoformat()}


def _assert_second_page_follows(client, path: str, params: dict, key, order_key=None) -> None:
    # order_key defaults to key: for a single-field cursor (ts or day) the two coincide and the
    # field has no ties, so ">=" and uniqueness together are as strong as strict ordering. For the
    # universe cursor, order_key is passed as ts alone -- the tie-break field is symbol, and the
    # database's collation is not Python's string order (BF.B sorts after BFAM in en_US.utf8, before
    # it in codepoint order), so only ts may be compared as an order and symbol only as identity
    order_key = order_key or key
    first = _page(client.get(path, params={**params, "limit": 50}), path)
    assert first["next_cursor"] is not None, path
    second = _page(
        client.get(path, params={**params, "limit": 50, "cursor": first["next_cursor"]}), path
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
    client, path: str, params: dict, small_limit: int, big_limit: int
) -> None:
    walked = _collect_prefix(client, path, params, small_limit, big_limit)
    big = _page(client.get(path, params={**params, "limit": big_limit}), path)
    # a walk that stops short of what one bigger page actually holds is itself the defect -- a
    # cursor the service treats as final while more rows exist, whether or not the rows it did
    # return happen to match
    assert len(walked) >= min(big_limit, len(big["data"])), (path, len(walked), len(big["data"]))
    n = min(len(walked), len(big["data"]))
    assert n > 0, path
    assert walked[:n] == big["data"][:n], path


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
    # only the range tier running before any checkout gives this 422
    inverted = {"start": window["end"], "end": window["start"]}
    response = client.get(f"/symbols/{_UNKNOWN_SYMBOL}/bars", params=inverted)
    error = _refusal(response, 422, "invalid_range", "/symbols/{symbol}/bars")
    assert error["detail"]["reason"] == "start_after_end"


def test_bars_refuses_a_window_before_the_ingested_range_ahead_of_any_symbol_lookup(client):
    # unknown symbol again, for the same reason as the inverted-window case above: this must be a
    # 422 from the range tier, never a 404 from a lookup the range tier should have pre-empted
    start = settings.INGEST_START - timedelta(days=10)
    end = settings.INGEST_START - timedelta(days=5)
    response = client.get(
        f"/symbols/{_UNKNOWN_SYMBOL}/bars",
        params={"start": start.isoformat(), "end": end.isoformat()},
    )
    error = _refusal(response, 422, "invalid_range", "/symbols/{symbol}/bars before INGEST_START")
    assert error["detail"]["reason"] == "outside_ingested_range"


def test_a_window_over_the_cap_is_refused_on_daily_and_the_analytics_endpoints(client, symbols):
    params = _oversized_window()
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


def test_bars_has_no_window_length_cap_where_the_analytics_endpoints_do(client, symbols):
    # spec line 630: /bars takes the whole ingested range. limit=1 keeps this cheap regardless of
    # how wide the window is -- the point is acceptance, not the row count
    body = _page(
        client.get(f"/symbols/{symbols[0]}/bars", params={**_oversized_window(), "limit": 1}),
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


def test_a_cursor_from_one_window_is_refused_on_a_disjoint_window(client, symbols):
    symbol = symbols[0]
    window_a = {
        "start": settings.E2E_START.isoformat(),
        "end": (settings.E2E_START + timedelta(days=9)).isoformat(),
    }
    disjoint_start = settings.E2E_START + timedelta(days=40)
    window_b = {
        "start": disjoint_start.isoformat(),
        "end": (disjoint_start + timedelta(days=9)).isoformat(),
    }
    first = _page(
        client.get(f"/symbols/{symbol}/bars", params={**window_a, "limit": 1}), "/bars window A"
    )
    assert first["next_cursor"] is not None

    error = _refusal(
        client.get(f"/symbols/{symbol}/bars", params={**window_b, "cursor": first["next_cursor"]}),
        400,
        "invalid_cursor",
        "/bars window B",
    )
    assert error["detail"]["reason"] == "cursor_outside_window"


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
    _assert_second_page_follows(client, f"/symbols/{symbols[0]}/bars", window, _ts_key)


def test_a_largest_moves_second_page_follows_the_cursor_without_repeating_a_row(client, window):
    # order_key=_ts_key, not the (ts, symbol) tuple: the tie-break is the database's collation, and
    # Python string order is not that collation (see the portability tests below)
    _assert_second_page_follows(
        client, "/analytics/largest-moves", window, _ts_symbol_key, order_key=_ts_key
    )


def test_next_cursor_is_present_and_explicitly_null_on_a_last_page(client, window, symbols):
    cases = [
        ("/symbols", {"limit": settings.BARS_PAGE_MAX}),
        (f"/symbols/{symbols[0]}/daily", {**window, "limit": settings.AGG_PAGE_MAX}),
    ]
    for path, params in cases:
        # _page has already asserted the key is present, so this reads present-and-null
        assert _page(client.get(path, params=params), path)["next_cursor"] is None, path


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


def test_largest_moves_default_threshold_matches_an_explicit_zero_and_zero_is_accepted(client):
    day = settings.E2E_START.isoformat()
    day_window = {"start": day, "end": day, "limit": 50}
    default = _page(client.get("/analytics/largest-moves", params=day_window), "default threshold")
    explicit_zero = _page(
        client.get("/analytics/largest-moves", params={**day_window, "min_move_pct": 0}),
        "explicit min_move_pct=0",
    )
    assert default["data"], "default threshold"
    assert default["data"] == explicit_zero["data"]


def test_symbols_has_no_duplicate_symbols(client):
    # not Python string ascending: the database collation ignores punctuation (BF.B sorts after
    # BFAM), which codepoint order does not -- see the portability tests below for the property
    # that replaces it
    body = _page(client.get("/symbols", params={"limit": settings.BARS_PAGE_MAX}), "/symbols")
    names = [row["symbol"] for row in body["data"]]
    assert names
    assert len(set(names)) == len(names)


def test_symbols_active_filter_partitions_the_unfiltered_set(client):
    cap = settings.BARS_PAGE_MAX
    unfiltered = _page(client.get("/symbols", params={"limit": cap}), "/symbols")
    active = _page(client.get("/symbols", params={"limit": cap, "active": True}), "/symbols active")
    inactive = _page(
        client.get("/symbols", params={"limit": cap, "active": False}), "/symbols inactive"
    )

    assert all(row["active"] is True for row in active["data"]), "active=true"
    assert all(row["active"] is False for row in inactive["data"]), "active=false"

    all_symbols = {row["symbol"] for row in unfiltered["data"]}
    partitioned = {row["symbol"] for row in active["data"]} | {
        row["symbol"] for row in inactive["data"]
    }
    assert partitioned == all_symbols


def test_a_bars_page_is_strictly_ascending_for_every_configured_symbol(client, window, symbols):
    for symbol in symbols:
        response = client.get(f"/symbols/{symbol}/bars", params={**window, "limit": 200})
        body = _page(response, symbol)
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
    threshold = 0.1
    params = {**window, "limit": 200, "min_move_pct": threshold}
    body = _page(client.get("/analytics/largest-moves", params=params), "/analytics/largest-moves")
    assert body["data"]
    # ts non-decreasing and no duplicate (ts, symbol) key -- not "symbol ascending", which is a
    # Python string comparison the database collation does not agree with (see below)
    timestamps = [_ts_key(row) for row in body["data"]]
    assert all(a <= b for a, b in zip(timestamps, timestamps[1:])), "largest-moves"
    keys = [_ts_symbol_key(row) for row in body["data"]]
    assert len(set(keys)) == len(keys), "largest-moves"
    for row in body["data"]:
        assert set(row) == {"ts", "symbol", "open", "close", "move_pct"}, row
        assert abs(float(row["move_pct"])) >= threshold, row


def test_symbols_pages_walked_at_a_small_limit_concatenate_to_one_larger_page(client):
    _assert_walk_matches_one_larger_page(client, "/symbols", {}, small_limit=2, big_limit=5)


def test_bars_pages_walked_at_a_small_limit_concatenate_to_one_larger_page(client, window, symbols):
    _assert_walk_matches_one_larger_page(
        client, f"/symbols/{symbols[0]}/bars", window, small_limit=7, big_limit=30
    )


def test_daily_pages_walked_at_a_small_limit_concatenate_to_one_larger_page(
    client, window, symbols
):
    _assert_walk_matches_one_larger_page(
        client, f"/symbols/{symbols[0]}/daily", window, small_limit=3, big_limit=10
    )


def test_largest_moves_pages_walked_at_a_small_limit_concatenate_to_one_larger_page(client):
    # a single day, never the configured 90-day window: universe-wide rows are dense enough that a
    # whole-window walk would cost thousands of requests for no more assurance than one day gives
    day = settings.E2E_START.isoformat()
    _assert_walk_matches_one_larger_page(
        client, "/analytics/largest-moves", {"start": day, "end": day}, small_limit=2, big_limit=8
    )


def test_the_configured_windows_first_and_last_session_are_not_dropped_by_any_endpoint(
    client, window, symbols
):
    symbol = symbols[0]
    daily = _page(client.get(f"/symbols/{symbol}/daily", params=window), "/daily")
    assert daily["data"], "/daily"
    reported_first, reported_last = daily["data"][0]["day"], daily["data"][-1]["day"]

    for day, label in ((reported_first, "first"), (reported_last, "last")):
        day_window = {"start": day, "end": day}
        bars = _page(
            client.get(
                f"/symbols/{symbol}/bars", params={**day_window, "limit": settings.BARS_PAGE_MAX}
            ),
            f"/bars on daily's own {label} day",
        )
        assert bars["data"], f"/bars has no rows on daily's own {label} day {day}"
        assert all(_ts_key(row).date().isoformat() == day for row in bars["data"]), label

        moves = _page(
            client.get("/analytics/largest-moves", params={**day_window, "limit": 50}),
            f"/analytics/largest-moves on daily's own {label} day",
        )
        assert moves["data"], (
            f"/analytics/largest-moves has no rows on daily's own {label} day {day}"
        )
        assert all(_ts_key(row).date().isoformat() == day for row in moves["data"]), label

    # ground truth from a codepath the rollup never touches: if /bars independently confirms the
    # window's own literal edges are real trading sessions, the rollup must report exactly those
    # and not a neighbour it silently substituted
    edge_start = _page(
        client.get(
            f"/symbols/{symbol}/bars", params={**window, "end": window["start"], "limit": 1}
        ),
        "/bars at the window's literal start",
    )
    if edge_start["data"]:
        assert reported_first == window["start"], "daily dropped the window's first session"

    edge_end = _page(
        client.get(
            f"/symbols/{symbol}/bars", params={**window, "start": window["end"], "limit": 1}
        ),
        "/bars at the window's literal end",
    )
    if edge_end["data"]:
        assert reported_last == window["end"], "daily dropped the window's last session"


def test_largest_moves_values_agree_with_bars_and_daily_for_one_symbol(client, symbols):
    symbol = symbols[0]
    full_window = {"start": settings.E2E_START.isoformat(), "end": settings.E2E_END.isoformat()}
    daily = _page(client.get(f"/symbols/{symbol}/daily", params=full_window), "/daily")
    assert daily["data"], "/daily"
    daily_row = daily["data"][-1]
    day = daily_row["day"]

    day_window = {"start": day, "end": day}
    bars = _page(
        client.get(
            f"/symbols/{symbol}/bars", params={**day_window, "limit": settings.BARS_PAGE_MAX}
        ),
        "/bars for the day",
    )
    assert bars["data"], "/bars for the day"
    bars_by_ts = {row["ts"]: row for row in bars["data"]}
    for row in bars["data"]:
        o, h, lo, c = (_dec(row[k]) for k in ("open", "high", "low", "close"))
        assert h >= max(o, c), row["ts"]
        assert lo <= min(o, c), row["ts"]

    # universe-wide and one whole day, per the tradeoff above -- min_move_pct=0 makes every
    # regular-session bar of every symbol a row, so this is the full day's worth of pages
    moves_for_symbol = {}
    cursor = None
    for _ in range(_FULL_DAY_PAGE_BUDGET):
        params = {**day_window, "min_move_pct": 0, "limit": settings.AGG_PAGE_MAX}
        if cursor is not None:
            params["cursor"] = cursor
        page = _page(
            client.get("/analytics/largest-moves", params=params), "/analytics/largest-moves"
        )
        for row in page["data"]:
            if row["symbol"] == symbol:
                moves_for_symbol[row["ts"]] = row
        cursor = page["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("largest-moves did not reach its last page for one day within the page budget")

    assert moves_for_symbol, "no largest-moves rows for the configured symbol on its own last day"

    for ts, move_row in moves_for_symbol.items():
        assert ts in bars_by_ts, f"largest-moves named a ts /bars does not have: {ts}"
        bar_row = bars_by_ts[ts]
        assert _dec(move_row["open"]) == _dec(bar_row["open"]).quantize(
            _FOUR_PLACES, rounding=ROUND_HALF_UP
        ), ts
        assert _dec(move_row["close"]) == _dec(bar_row["close"]).quantize(
            _FOUR_PLACES, rounding=ROUND_HALF_UP
        ), ts
        expected_move = (
            (_dec(bar_row["close"]) - _dec(bar_row["open"])) * 100 / _dec(bar_row["open"])
        ).quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)
        assert _dec(move_row["move_pct"]) == expected_move, ts

    # the regular-session subset of /bars that day is exactly the set largest-moves names -- the
    # rollup's own OHLCV must agree with recomputing it from that subset alone
    regular_ts = sorted(moves_for_symbol)
    regular_bars = [bars_by_ts[ts] for ts in regular_ts]
    assert _dec(daily_row["open"]) == _dec(regular_bars[0]["open"])
    assert _dec(daily_row["close"]) == _dec(regular_bars[-1]["close"])
    assert _dec(daily_row["high"]) == max(_dec(r["high"]) for r in regular_bars)
    assert _dec(daily_row["low"]) == min(_dec(r["low"]) for r in regular_bars)
    assert daily_row["volume"] == sum(r["volume"] for r in regular_bars)
    assert daily_row["bars"] == len(regular_bars)


def test_gaps_and_volatility_totals_agree_with_the_daily_rollup_over_a_sub_window(client, symbols):
    symbol = symbols[0]
    sub_start = settings.E2E_START
    sub_end = sub_start + timedelta(days=9)
    sub_window = {"start": sub_start.isoformat(), "end": sub_end.isoformat()}

    daily = _page(client.get(f"/symbols/{symbol}/daily", params=sub_window), "/daily sub-window")
    n_days = len(daily["data"])
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
    expected_returns = sum(row["bars"] - 1 for row in daily["data"])
    assert total_returns == expected_returns
