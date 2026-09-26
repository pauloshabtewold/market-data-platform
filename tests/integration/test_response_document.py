"""The published 200 against the wire, in both directions.

The success schemas are hand-written models read by the document generator alone -- nothing in that
generator reads the SQL that fills a page -- so a column renamed in a statement, or a model field
nobody removed with it, would leave the document describing a body the service does not send. Only a
comparison against a real response catches either, and the comparison is the guard the schemas
depend on.
"""

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.market_fixture import TRADING_DAYS, WINDOW_END, WINDOW_START, load

_WINDOW = {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}

# every documented operation that answers rows, with the request that fills it
_REQUESTS = (
    ("/symbols", "/symbols", {}),
    ("/symbols/{symbol}/bars", "/symbols/AAA/bars", _WINDOW),
    ("/symbols/{symbol}/daily", "/symbols/AAA/daily", _WINDOW),
    ("/analytics/volatility", "/analytics/volatility", {"symbol": "AAA", **_WINDOW}),
    ("/analytics/gaps", "/analytics/gaps", {"symbol": "AAA", **_WINDOW}),
    ("/analytics/largest-moves", "/analytics/largest-moves", _WINDOW),
)


@pytest.fixture
def document_client(migrated_dsn):
    load(migrated_dsn)
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


def _schema_of(document, path):
    """(page schema, row schema) as the document publishes them for path's 200."""
    schemas = document["components"]["schemas"]
    ref = document["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    page = schemas[ref["$ref"].rsplit("/", 1)[1]]
    row = schemas[page["properties"]["data"]["items"]["$ref"].rsplit("/", 1)[1]]
    return page, row


def _nullable(schema, field):
    branches = schema["properties"][field].get("anyOf", [schema["properties"][field]])
    return any(branch.get("type") == "null" for branch in branches)


def test_every_documented_success_schema_names_the_fields_a_real_body_carries(document_client):
    document = document_client.get("/openapi.json").json()
    for path, url, params in _REQUESTS:
        response = document_client.get(url, params=params)
        assert response.status_code == 200, (path, response.text)
        body = response.json()
        page, row = _schema_of(document, path)
        assert set(body) == set(page["properties"]), path
        # a row, not a shape derived from the model: the fixture fills every one of these endpoints,
        # so an empty data list here would make the comparison below vacuous
        assert body["data"], path
        for entry in body["data"]:
            assert set(entry) == set(row["properties"]), path
            for field, value in entry.items():
                assert value is not None or _nullable(row, field), (path, field)


def test_the_health_document_names_the_fields_health_answers(document_client):
    document = document_client.get("/openapi.json").json()
    schemas = document["components"]["schemas"]
    ref = document["paths"]["/health"]["get"]["responses"]["200"]["content"]["application/json"]
    model = schemas[ref["schema"]["$ref"].rsplit("/", 1)[1]]
    body = document_client.get("/health").json()
    assert body["status"] == "ok"
    assert set(body) == set(model["properties"])


def test_the_gaps_fields_the_document_marks_nullable_are_the_ones_an_empty_window_answers_null(
    document_client,
):
    # a window of one session has no previous close to gap from, so the counts are zero and every
    # distribution field is null -- the case that makes the nullability in the document a claim
    # about the wire rather than a guess, and the one no test had ever read field by field
    document = document_client.get("/openapi.json").json()
    _, row = _schema_of(document, "/analytics/gaps")
    one_session = TRADING_DAYS[0].isoformat()
    body = document_client.get(
        "/analytics/gaps", params={"symbol": "AAA", "start": one_session, "end": one_session}
    ).json()
    assert len(body["data"]) == 1
    summary = body["data"][0]
    assert summary["gaps"] == 0
    null_fields = sorted(field for field, value in summary.items() if value is None)
    assert null_fields == sorted(field for field in row["properties"] if _nullable(row, field))
    assert null_fields == [
        "max_pct",
        "mean_pct",
        "median_pct",
        "min_pct",
        "p25_pct",
        "p75_pct",
        "stddev_pct",
    ]
