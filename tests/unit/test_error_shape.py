import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.errors import ERROR_CODES, ApiError, error_body, install_error_handlers

app = FastAPI()
install_error_handlers(app)


@app.get("/probe")
def probe(limit: int = 10):
    return {"limit": limit}


@app.get("/item/{item_id}")
def item(item_id: int):
    return {"item_id": item_id}


@app.get("/boom")
def boom():
    raise ZeroDivisionError("a database message that must not reach the client")


@app.get("/raiser")
def raiser():
    raise HTTPException(404, "no such symbol")


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
    }

    path_response = client.get("/item/not-an-int")
    assert path_response.status_code == 400
    assert path_response.json()["error"]["detail"] == {
        "reason": "invalid_parameter",
        "parameter": "item_id",
        "location": "path",
    }


def test_an_http_exception_raised_in_an_endpoint_is_internal_rather_than_unknown_route(
    permissive_client,
):
    response = permissive_client.get("/raiser")
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal",
            "message": "the request could not be completed",
            "detail": None,
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
