from datetime import date

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.errors import (
    ERROR_CODES,
    INVALID_RANGE_MESSAGE,
    UNKNOWN_SYMBOL_MESSAGE,
    ApiError,
    error_body,
    install_error_handlers,
)

app = FastAPI()
install_error_handlers(app)


@app.get("/probe")
def probe(limit: int = 10):
    return {"limit": limit}


@app.get("/item/{item_id}")
def item(item_id: int):
    return {"item_id": item_id}


@app.get("/two")
def two(a: int | None = None, b: int | None = None):
    return {"a": a, "b": b}


@app.get("/win")
def win(start: date, end: date):
    return {"start": start, "end": end}


@app.get("/boom")
def boom():
    raise ZeroDivisionError("a database message that must not reach the client")


@app.get("/raiser")
def raiser():
    raise HTTPException(404, "no such symbol")


@app.get("/refuser")
def refuser():
    raise ApiError(404, "unknown_symbol", "no such symbol", {"reason": "unknown_symbol"})


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def permissive_client():
    # the default client re-raises an unhandled endpoint exception out of the call, so there is
    # no response to assert on -- the handler runs either way (fact 9)
    return TestClient(app, raise_server_exceptions=False)


def test_an_unrouted_path_answers_with_the_one_error_shape(client):
    response = client.get("/nonsense")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "invalid_params",
            "message": "no route matches this path and method",
            "detail": {"reason": "unknown_route", "path": "/nonsense"},
        }
    }


def test_a_method_the_route_does_not_serve_answers_the_same_shape_with_405(client):
    response = client.post("/probe")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.json() == {
        "error": {
            "code": "invalid_params",
            "message": "no route matches this path and method",
            "detail": {"reason": "unknown_route", "path": "/probe"},
        }
    }


def test_an_unhandled_exception_answers_internal_with_no_traceback(permissive_client, caplog):
    with caplog.at_level("ERROR", logger="api.errors"):
        response = permissive_client.get("/boom")
    # the body deliberately says nothing, so the log record is the only account of the cause
    assert [r.message for r in caplog.records] == ["unhandled exception"]
    assert caplog.records[0].exc_info[0] is ZeroDivisionError
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal",
            "message": "the request could not be completed",
            "detail": None,
        }
    }
    assert "ZeroDivisionError" not in response.text
    assert "database message" not in response.text


def test_a_malformed_parameter_is_a_four_hundred(client):
    query_response = client.get("/probe", params={"limit": "not-an-int"})
    assert query_response.status_code == 400
    assert query_response.json()["error"]["code"] == "invalid_params"
    # full-string equality, not a substring: building the expected value from the constant would
    # move both sides of the assertion together and the mutation it exists to catch would survive
    assert query_response.json()["error"]["message"] == "one or more parameters are not valid"
    assert query_response.json()["error"]["detail"] == {
        "reason": "invalid_parameter",
        "parameter": "limit",
        "location": "query",
        "errors": [{"parameter": "limit", "location": "query", "type": "int_parsing"}],
    }

    path_response = client.get("/item/not-an-int")
    assert path_response.status_code == 400
    assert path_response.json()["error"]["detail"] == {
        "reason": "invalid_parameter",
        "parameter": "item_id",
        "location": "path",
        "errors": [{"parameter": "item_id", "location": "path", "type": "int_parsing"}],
    }


def test_an_http_exception_raised_in_an_endpoint_is_internal_rather_than_unknown_route(
    permissive_client, caplog
):
    with caplog.at_level("ERROR", logger="api.errors"):
        response = permissive_client.get("/raiser")
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal",
            "message": "the request could not be completed",
            "detail": None,
        }
    }
    # the traceback, at THIS call site of _internal_response and not only the other one. The body
    # deliberately says nothing, so the log record is the whole account of a 500 -- and the two
    # call sites are separate: the unhandled-exception test above pins exc_info on its own, and
    # passing None here instead of exc leaves this response byte-identical with a green suite
    assert [r.message for r in caplog.records] == ["unhandled exception"]
    assert caplog.records[0].exc_info[0] is HTTPException

    # the sibling an endpoint raises deliberately, and the one shape whose detail comes from the
    # caller rather than from the handler: every other case here carries None, so a handler passing
    # None in place of exc.detail is invisible -- and this is what unknown_symbol and invalid_range
    # publish on every endpoint that raises them
    response = permissive_client.get("/refuser")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "unknown_symbol",
            "message": "no such symbol",
            "detail": {"reason": "unknown_symbol"},
        }
    }


def test_the_code_vocabulary_is_closed():
    # the type is the claim -- "a sixth code cannot enter by accident" is false for a mutable set,
    # which satisfies every other assertion in this test
    assert type(ERROR_CODES) is frozenset
    assert sorted(ERROR_CODES) == [
        "internal",
        "invalid_cursor",
        "invalid_params",
        "invalid_range",
        "unknown_symbol",
    ]
    with pytest.raises(ValueError):
        ApiError(404, "not_found", "m", None)
    # the wire shape's own constructor, which a later feature's handler can reach without ApiError
    with pytest.raises(ValueError):
        error_body("not_found", "m", None)


def test_a_request_missing_both_required_parameters_names_both_in_the_errors_list(client):
    response = client.get("/win")
    assert response.status_code == 400
    detail = response.json()["error"]["detail"]
    # the case the additive list exists for: the three first-error keys can only ever name one of
    # the two, so before this list a request missing both reported only start
    assert detail["errors"] == [
        {"parameter": "start", "location": "query", "type": "missing"},
        {"parameter": "end", "location": "query", "type": "missing"},
    ]


def test_the_first_error_keys_survive_alongside_the_errors_list(client):
    response = client.get("/two", params={"a": "x", "b": "y"})
    assert response.status_code == 400
    detail = response.json()["error"]["detail"]
    # the backward-compatibility kill: the Feature 5 wire contract is these three keys, and
    # replacing them with the list rather than adding it breaks every client already reading them
    assert detail["reason"] == "invalid_parameter"
    assert detail["parameter"] == "a"
    assert detail["location"] == "query"
    assert [e["parameter"] for e in detail["errors"]] == ["a", "b"]


def test_each_kind_of_wrong_request_carries_its_own_pydantic_type_slug(client):
    # one test iterating five cases, never a parametrize: a parametrize collects as five items and
    # moves this file's gated count from 10 to 14
    cases = [
        ("/probe", {"limit": "not-an-int"}, ["int_parsing"]),
        ("/two", {"a": "x", "b": "y"}, ["int_parsing", "int_parsing"]),
        ("/win", {}, ["missing", "missing"]),
        ("/win", {"start": "2026-01-01"}, ["missing"]),
        ("/win", {"start": "nope", "end": "2026-01-01"}, ["date_from_datetime_parsing"]),
    ]
    for path, params, expected in cases:
        response = client.get(path, params=params)
        body = response.json()["error"]
        assert response.status_code == 400, f"{path} {params}"
        assert body["code"] == "invalid_params", f"{path} {params}"
        assert body["detail"]["reason"] == "invalid_parameter", f"{path} {params}"
        # the slugs are pydantic's own vocabulary, written out as literals so a rename fails here
        # by name rather than reaching a client silently
        assert [e["type"] for e in body["detail"]["errors"]] == expected, f"{path} {params}"


def test_the_two_new_message_constants_are_the_strings_the_endpoints_publish():
    # full-string equality against a literal written out here, never read back from api.errors:
    # building the expected from the thing under test moves both sides and the mutation survives
    assert UNKNOWN_SYMBOL_MESSAGE == "no symbol by that name has been ingested"
    assert INVALID_RANGE_MESSAGE == "the requested date range is not one this endpoint serves"
