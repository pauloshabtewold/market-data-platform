"""The published 200 against the wire, in both directions.

The success schemas are hand-written models read by the document generator alone -- nothing in that
generator reads the SQL that fills a page -- so a column renamed in a statement, or a model field
nobody removed with it, would leave the document describing a body the service does not send. Only a
comparison against a real response catches either, and the comparison is the guard the schemas
depend on.

Names are only the first of three things a client reads off a schema. A field's published `type` is
what a generated model validates every row against, and its nullability is what decides whether a
null is a value or a parse failure, so both are compared here against a real body as well.
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

# a window of one session, which is the only request here that answers a null distribution field
_ONE_SESSION = TRADING_DAYS[0].isoformat()
_GAPS_ONE_SESSION = (
    "/analytics/gaps",
    "/analytics/gaps",
    {"symbol": "AAA", "start": _ONE_SESSION, "end": _ONE_SESSION},
)

# What each JSON Schema type admits of a value json.loads produced, and the asymmetry between the
# two numeric types is the whole reason this comparison exists rather than a key-set one. A price
# column is an unqualified numeric, so its scale is whatever the insert produced and one page
# carries the same field as a JSON integer on one row and a JSON float on the next -- `number`
# admits both, which is why every price is declared `float`. `integer` admits an integral float and
# refuses a fractional one, so a price declared `int` is caught by the first fractional row: that
# refusal is what a real generated client performs on every row it parses.
# bool is excluded explicitly because it is a subclass of int in Python and is not a number in JSON.
_ADMITS = {
    "null": lambda value: value is None,
    "boolean": lambda value: isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "integer": lambda value: (isinstance(value, int) and not isinstance(value, bool))
    or (isinstance(value, float) and value.is_integer()),
    "string": lambda value: isinstance(value, str),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}

# Declared `| None` and never answered null by any request above. The fixture cannot produce a NULL
# in any of them -- it seeds every symbols column, its bar shape leaves no price, volume or trade
# count unset, and the analytics statements answer a real number wherever they answer a row at all.
# Frozen rather than left implicit, so both directions fail here: a field entering the list is a new
# nullable nothing exercises, and a field leaving it is either a `| None` removed while the wire can
# still answer null, or one that a request here has started to answer null and should be asserted on.
_NULLABLE_NEVER_ANSWERED_NULL = (
    "BarRow.close",
    "BarRow.high",
    "BarRow.low",
    "BarRow.open",
    "BarRow.trade_count",
    "BarRow.volume",
    "BarRow.vwap",
    "DailyRow.close",
    "DailyRow.high",
    "DailyRow.low",
    "DailyRow.open",
    "DailyRow.volume",
    "MoveRow.close",
    "MoveRow.move_pct",
    "MoveRow.open",
    "SymbolRow.active",
    "SymbolRow.exchange",
    "SymbolRow.first_bar_ts",
    "SymbolRow.name",
    "VolatilityBucket.annualized_pct",
    "VolatilityBucket.avg_minutes_per_return",
    "VolatilityBucket.stddev_pct",
)

# a dsn nothing listens on, passed explicitly: the document is generated from the routes and the
# models, so reading it needs no database and must not reach for the configured one
_NO_DATABASE_DSN = "postgresql://nobody:nobody@127.0.0.1:1/none"

# Vocabulary that describes how this service is wired rather than what it answers. A docstring on a
# published model becomes that schema's `description` and travels to every generated client, so the
# reasoning behind declaring a 200 through `responses` belongs in a comment beside the model.
_WIRING_WORDS = ("response_model", "re-serialis", "re-serializ", "reserialis", "reserializ")


@pytest.fixture
def document_client(migrated_dsn):
    load(migrated_dsn)
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


def _schemas_of(document, path):
    """((page name, page schema), (row name, row schema)) for path's 200, as published."""
    schemas = document["components"]["schemas"]
    ref = document["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    page_name = ref["$ref"].rsplit("/", 1)[1]
    row_ref = schemas[page_name]["properties"]["data"]["items"]["$ref"]
    row_name = row_ref.rsplit("/", 1)[1]
    return (page_name, schemas[page_name]), (row_name, schemas[row_name])


def _schema_of(document, path):
    """(page schema, row schema) as the document publishes them for path's 200."""
    (_, page), (_, row) = _schemas_of(document, path)
    return page, row


def _branches(schema, field):
    spec = schema["properties"][field]
    return spec.get("anyOf", [spec])


def _nullable(schema, field):
    return any(branch.get("type") == "null" for branch in _branches(schema, field))


def _declared_types(schema, field):
    types = [branch.get("type") for branch in _branches(schema, field)]
    # a branch carrying no `type` is a $ref or a bare {}, which this comparison cannot check against
    # a value: raised rather than skipped, so a field it cannot read is never silently admitted
    assert all(declared in _ADMITS for declared in types), (field, types)
    return types


def _admits(schema, field, value):
    return any(_ADMITS[declared](value) for declared in _declared_types(schema, field))


def _descriptions(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield f"{path}.description", value
            else:
                yield from _descriptions(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _descriptions(value, f"{path}[{index}]")


def _wiring_descriptions(document):
    """Where in the document a published description explains this service's own wiring."""
    return sorted(
        where
        for where, text in _descriptions(document)
        if any(word in text.lower() for word in _WIRING_WORDS)
    )


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


def test_every_documented_success_schema_publishes_the_json_type_a_real_body_carries(
    document_client,
):
    # the field NAMES matching says nothing about what a client does with a value: a model declaring
    # a price column `int` or a count `str` publishes a type a generated client refuses the real row
    # on, and no comparison of key sets or of nullability can see it
    document = document_client.get("/openapi.json").json()
    for path, url, params in _REQUESTS:
        response = document_client.get(url, params=params)
        assert response.status_code == 200, (path, response.text)
        body = response.json()
        (_, page), (_, row) = _schemas_of(document, path)
        # the page's own two fields as well as the rows: next_cursor's type is half of the
        # present-and-explicitly-null contract a generated client is built to honour
        assert body["data"], path
        for schema, entry in ((page, body), *((row, entry) for entry in body["data"])):
            for field, value in entry.items():
                assert _admits(schema, field, value), (
                    path,
                    field,
                    value,
                    _declared_types(schema, field),
                )


def test_the_nullable_fields_no_request_here_answers_null_are_named_rather_than_left_implicit(
    document_client,
):
    # comparing a value against a published nullable reads that `| None` only on a body that really
    # answers null, so on its own it admits both a `| None` no column needs and a `| None` dropped
    # from a field whose column is nullable. Which fields go unexercised is recorded instead, and
    # the record is what fails -- in both directions, since it is an equality
    document = document_client.get("/openapi.json").json()
    declared, answered_null = set(), set()
    for path, url, params in (*_REQUESTS, _GAPS_ONE_SESSION):
        response = document_client.get(url, params=params)
        assert response.status_code == 200, (path, response.text)
        body = response.json()
        page, row = _schemas_of(document, path)
        for (name, schema), entries in ((page, [body]), (row, body["data"])):
            assert entries, path
            declared |= {
                f"{name}.{field}" for field in schema["properties"] if _nullable(schema, field)
            }
            for entry in entries:
                answered_null |= {
                    f"{name}.{field}" for field, value in entry.items() if value is None
                }
    assert tuple(sorted(declared - answered_null)) == _NULLABLE_NEVER_ANSWERED_NULL
    # the other half, and the reason the list above is short: every remaining nullable field is one
    # a request here reads null, which is the claim about the wire the document is making
    assert sorted(declared & answered_null) == [
        "BarsPage.next_cursor",
        "DailyPage.next_cursor",
        "GapsPage.next_cursor",
        "GapsSummary.max_pct",
        "GapsSummary.mean_pct",
        "GapsSummary.median_pct",
        "GapsSummary.min_pct",
        "GapsSummary.p25_pct",
        "GapsSummary.p75_pct",
        "GapsSummary.stddev_pct",
        "MovesPage.next_cursor",
        "SymbolsPage.next_cursor",
        "VolatilityPage.next_cursor",
    ]


def test_no_published_description_explains_how_this_service_is_wired():
    # a model's docstring is the one place an internal decision reaches a client by accident: it
    # becomes the schema's `description`, which a generator copies into its own class. Built over a
    # dsn nothing listens on, because this is a property of the document and not of any response
    document = create_app(_NO_DATABASE_DSN).openapi()
    assert _wiring_descriptions(document) == []
    # not vacuous: the same sweep over a document carrying the sentence this check exists for finds it
    assert _wiring_descriptions(
        {"components": {"schemas": {"HealthResponse": {"description": "never as response_model"}}}}
    ) == [".components.schemas.HealthResponse.description"]
    # a docstring wrapped across source lines reaches a reader of the page as one run-on line
    schemas = document["components"]["schemas"]
    assert [n for n, s in schemas.items() if "\n" in s.get("description", "")] == []
    # and what /health answers is still described where a client-facing sentence belongs
    assert document["paths"]["/health"]["get"]["responses"]["200"]["description"]


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
