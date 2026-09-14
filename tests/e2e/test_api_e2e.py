from datetime import datetime, timedelta

from config import settings

_UNKNOWN_SYMBOL = "ZZZZ-NOT-A-SYMBOL"
_BAD_CURSOR = "not-a-cursor"

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


def _assert_second_page_follows(client, path: str, params: dict, key) -> None:
    first = _page(client.get(path, params={**params, "limit": 50}), path)
    assert first["next_cursor"] is not None, path
    second = _page(
        client.get(path, params={**params, "limit": 50, "cursor": first["next_cursor"]}), path
    )
    assert second["data"], path
    boundary = key(first["data"][-1])
    seen = {key(row) for row in first["data"]}
    for row in second["data"]:
        assert key(row) > boundary, path
        assert key(row) not in seen, path


def test_health_answers_ok_with_a_real_version(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"] and body["version"] != "unknown"


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


def test_a_tampered_cursor_is_refused_on_every_endpoint_that_declares_one(client, window, symbols):
    for path, params, _cap in _cursor_endpoints(window, symbols[0]):
        response = client.get(path, params={**params, "cursor": _BAD_CURSOR})
        _refusal(response, 400, "invalid_cursor", path)


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
    _assert_second_page_follows(client, "/analytics/largest-moves", window, _ts_symbol_key)


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


def test_symbols_are_strictly_ascending(client):
    body = _page(client.get("/symbols", params={"limit": settings.BARS_PAGE_MAX}), "/symbols")
    _assert_strictly_ascending([row["symbol"] for row in body["data"]], "/symbols")


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


def test_a_largest_moves_page_is_strictly_ascending_and_every_row_clears_the_threshold(
    client, window
):
    # a positive threshold, because at the default of 0 every row clears it and the check could not
    # fail; 0.1 has fewer decimals than the projection keeps, so rounding cannot carry a qualifying
    # row below it
    threshold = 0.1
    params = {**window, "limit": 200, "min_move_pct": threshold}
    body = _page(client.get("/analytics/largest-moves", params=params), "/analytics/largest-moves")
    assert body["data"]
    _assert_strictly_ascending([_ts_symbol_key(row) for row in body["data"]], "largest-moves")
    for row in body["data"]:
        assert set(row) == {"ts", "symbol", "open", "close", "move_pct"}, row
        assert abs(float(row["move_pct"])) >= threshold, row
