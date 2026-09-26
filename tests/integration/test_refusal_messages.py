"""The `message` of every refusal this service can answer, on every endpoint that can answer it.

A refusal's body carries a code, a message and a detail. The endpoint tests assert the code and the
detail; the message was asserted at five raise sites once and nowhere since, so each new raise site
started with `error.message` unpinned and could publish it as null, as the wrong constant or as
shouting, with the whole suite green. `ErrorInfo.message` is declared a non-nullable string in the
published document, so a null there also contradicts the schema a generated client is built from.

The recipes below are checked for coverage against the routes' own declared responses rather than
listed by hand alone: a route that documents a status with no recipe here fails, and so does a route
this file has never heard of.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import api.deps
from api.errors import (
    INTERNAL_MESSAGE,
    INVALID_CURSOR_MESSAGE,
    INVALID_PARAMS_MESSAGE,
    INVALID_RANGE_MESSAGE,
    UNKNOWN_ROUTE_MESSAGE,
    UNKNOWN_SYMBOL_MESSAGE,
)
from api.main import create_app
from api.pagination import BARS_CURSOR, encode_cursor
from tests.market_fixture import WINDOW_END, WINDOW_START, load

# every (status, code) pair the endpoints produce, and the one message each publishes
_MESSAGE_FOR = {
    (400, "invalid_cursor"): INVALID_CURSOR_MESSAGE,
    (400, "invalid_params"): INVALID_PARAMS_MESSAGE,
    (404, "invalid_params"): UNKNOWN_ROUTE_MESSAGE,
    (404, "unknown_symbol"): UNKNOWN_SYMBOL_MESSAGE,
    (405, "invalid_params"): UNKNOWN_ROUTE_MESSAGE,
    (422, "invalid_range"): INVALID_RANGE_MESSAGE,
    (500, "internal"): INTERNAL_MESSAGE,
}

_WINDOW = {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}
_INVERTED = {"start": WINDOW_END.isoformat(), "end": WINDOW_START.isoformat()}
_BAD_CURSOR = {"cursor": "not-base64-at-all"}
_OVER_THE_CAP = {"limit": 0}

# (documented path, request path, query, status, code). Every entry is a refusal a live endpoint
# answers; the 500s are driven against a second app whose pool cannot connect
_RECIPES = (
    ("/symbols", "/symbols", _OVER_THE_CAP, 400, "invalid_params"),
    ("/symbols", "/symbols", _BAD_CURSOR, 400, "invalid_cursor"),
    ("/symbols/{symbol}/bars", "/symbols/AAA/bars", {**_WINDOW, **_OVER_THE_CAP}, 400, "invalid_params"),
    ("/symbols/{symbol}/bars", "/symbols/AAA/bars", {**_WINDOW, **_BAD_CURSOR}, 400, "invalid_cursor"),
    ("/symbols/{symbol}/bars", "/symbols/NOPE/bars", _WINDOW, 404, "unknown_symbol"),
    # percent-encoded because a raw control character is refused by the client before it is a
    # request; the server decodes it, so the handler sees a NUL in the path parameter
    ("/symbols/{symbol}/bars", "/symbols/AB%00CD/bars", _WINDOW, 404, "unknown_symbol"),
    ("/symbols/{symbol}/bars", "/symbols/AAA/bars", _INVERTED, 422, "invalid_range"),
    ("/symbols/{symbol}/daily", "/symbols/AAA/daily", {**_WINDOW, **_OVER_THE_CAP}, 400, "invalid_params"),
    ("/symbols/{symbol}/daily", "/symbols/AAA/daily", {**_WINDOW, **_BAD_CURSOR}, 400, "invalid_cursor"),
    ("/symbols/{symbol}/daily", "/symbols/NOPE/daily", _WINDOW, 404, "unknown_symbol"),
    ("/symbols/{symbol}/daily", "/symbols/AAA/daily", _INVERTED, 422, "invalid_range"),
    ("/analytics/volatility", "/analytics/volatility", {"symbol": "AAA", "start": "2026/03/01", "end": "2026-03-31"}, 400, "invalid_params"),
    ("/analytics/volatility", "/analytics/volatility", {"symbol": "NOPE", **_WINDOW}, 404, "unknown_symbol"),
    ("/analytics/volatility", "/analytics/volatility", {"symbol": "AAA", **_INVERTED}, 422, "invalid_range"),
    ("/analytics/gaps", "/analytics/gaps", {"symbol": "AAA", "start": "2026/03/01", "end": "2026-03-31"}, 400, "invalid_params"),
    ("/analytics/gaps", "/analytics/gaps", {"symbol": "NOPE", **_WINDOW}, 404, "unknown_symbol"),
    ("/analytics/gaps", "/analytics/gaps", {"symbol": "AAA", **_INVERTED}, 422, "invalid_range"),
    ("/analytics/largest-moves", "/analytics/largest-moves", {**_WINDOW, **_OVER_THE_CAP}, 400, "invalid_params"),
    ("/analytics/largest-moves", "/analytics/largest-moves", {**_WINDOW, **_BAD_CURSOR}, 400, "invalid_cursor"),
    ("/analytics/largest-moves", "/analytics/largest-moves", {**_WINDOW, "min_move_pct": "1e131073"}, 400, "invalid_params"),
    ("/analytics/largest-moves", "/analytics/largest-moves", _INVERTED, 422, "invalid_range"),
)
# the same request on every endpoint, against an app whose database cannot be reached
_LEGAL_REQUEST = {
    "/symbols": ("/symbols", {}),
    "/symbols/{symbol}/bars": ("/symbols/AAA/bars", _WINDOW),
    "/symbols/{symbol}/daily": ("/symbols/AAA/daily", _WINDOW),
    "/analytics/volatility": ("/analytics/volatility", {"symbol": "AAA", **_WINDOW}),
    "/analytics/gaps": ("/analytics/gaps", {"symbol": "AAA", **_WINDOW}),
    "/analytics/largest-moves": ("/analytics/largest-moves", _WINDOW),
}
_DEAD_DSN = "postgresql://nobody:nobody@127.0.0.1:1/none"


@pytest.fixture
def refusal_client(migrated_dsn):
    load(migrated_dsn)
    with TestClient(create_app(migrated_dsn), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def unreachable_client(monkeypatch):
    # the checkout timeout, not the connect: a pool that cannot reach its server keeps trying until
    # the checkout deadline, and the shipped 5 s would make this fixture the slowest thing in the
    # suite for a verdict that is the same at a tenth of a second
    monkeypatch.setattr(api.deps, "POOL_CHECKOUT_TIMEOUT_SECONDS", 0.1)
    with TestClient(create_app(_DEAD_DSN), raise_server_exceptions=False) as client:
        yield client


def _body(response, status, code):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code, response.text
    # the message, which is what this file exists for: the constant its code names, spelled the way
    # the module spells it, and never null
    assert error["message"] == _MESSAGE_FOR[(status, code)], response.text
    assert set(error) == {"code", "message", "detail"}, response.text
    return error


def test_every_documented_refusal_has_a_recipe_here(refusal_client):
    # the coverage half: the recipes are checked against what the routes declare, so a route that
    # documents a status no recipe drives fails here rather than going unexercised, and a route this
    # file has never heard of fails too
    document = refusal_client.get("/openapi.json").json()
    documented = {
        (path, int(status))
        for path, item in document["paths"].items()
        for status in item["get"]["responses"]
        if status.isdigit() and status != "200" and path != "/health"
    }
    covered = {(path, status) for path, _, _, status, _ in _RECIPES}
    covered |= {(path, 500) for path in _LEGAL_REQUEST}
    assert documented == covered


def test_every_refusal_publishes_the_message_its_code_names(refusal_client):
    for path, url, params, status, code in _RECIPES:
        error = _body(refusal_client.get(url, params=params), status, code)
        assert error["detail"], (path, url, params)


def test_a_cursor_from_another_endpoint_publishes_the_cursor_message(refusal_client):
    # a cursor that decodes and then fails its field check, rather than one that is not base64:
    # decode_cursor refuses at several steps and each step is its own raise site
    cursor = encode_cursor(
        BARS_CURSOR, {"ts": datetime(2026, 3, 10, 13, 30, tzinfo=timezone.utc)}
    )
    _body(refusal_client.get("/symbols", params={"cursor": cursor}), 400, "invalid_cursor")


def test_an_unrouted_path_and_a_wrong_method_publish_the_route_message(refusal_client):
    _body(refusal_client.get("/nonsense"), 404, "invalid_params")
    _body(refusal_client.post("/symbols"), 405, "invalid_params")


def test_a_request_that_cannot_reach_the_database_publishes_the_internal_message(
    unreachable_client,
):
    for url, params in _LEGAL_REQUEST.values():
        error = _body(unreachable_client.get(url, params=params), 500, "internal")
        # null detail on a 500, as the published schema says: nothing about the failure reaches
        # the client
        assert error["detail"] is None, url
