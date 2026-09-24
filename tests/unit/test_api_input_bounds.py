from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic_core import PydanticCustomError

from api.main import create_app
from api.routes import _reject_numeric_overflow

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
