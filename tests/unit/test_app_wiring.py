import contextlib
import logging
from datetime import date
from importlib.metadata import PackageNotFoundError, version

import psycopg_pool
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute

import api.main
import api.routes
import config
from api.deps import build_pool
from api.errors import ApiError
from api.main import create_app
from api.pagination import _instant_bounds

DEAD_DSN = "postgresql://nobody:nobody@127.0.0.1:1/none"
# a second unreachable dsn, never the one the environment supplies: an argument the app drops in
# favour of settings.DATABASE_URL is unobservable while the two are the same string
UNROUTED_DSN = "postgresql://nobody:nobody@127.0.0.1:2/none"

# every verb a route with no methods of its own answers, and the verbs the probe below asks for
EVERY_METHOD = ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT")
# every attribute a fastapi router keeps routes on; the second arrived with app.frontend() in 0.138.0
ROUTE_LISTS = ("routes", "_low_priority_routes")


class _RecordingConnection:
    def __init__(self, statements):
        self.statements = statements

    def execute(self, sql, *args, **kwargs):
        self.statements.append(str(sql))
        return self


class _RecordingPool:
    def __init__(self):
        self.statements = []
        self.timeouts = []
        self.waits = []
        self.opened = False
        self.closed = False

    @contextlib.contextmanager
    def connection(self, timeout=None):
        self.timeouts.append(timeout)
        yield _RecordingConnection(self.statements)

    # recorded rather than swallowed: a double that accepts the argument it exists to ignore leaves
    # the invariant to a thirty-second timeout in an unrelated test
    def open(self, wait=False):
        self.waits.append(wait)
        self.opened = True

    def close(self):
        self.closed = True


def _served(container, prefix=""):
    """Every (path, method) registration a router carries, descending into the routers included in
    it. One pair per registration, so a router included twice yields its pairs twice."""
    unread = sorted(
        name
        for name, value in vars(container).items()
        if name not in ROUTE_LISTS
        and isinstance(value, (list, tuple))
        and any(isinstance(item, BaseRoute) for item in value)
    )
    # a route list nobody reads is how app.frontend() came to serve files under a clean pass
    assert not unread, f"{type(container).__name__} keeps routes on {unread}; extend this"
    for attribute in ROUTE_LISTS:
        for route in getattr(container, attribute, ()):
            methods = getattr(route, "methods", None)
            if methods is not None:
                # starlette matches an empty methods set on every verb rather than on none
                for method in sorted(methods) or EVERY_METHOD:
                    yield prefix + route.path, method
                continue
            if hasattr(route, "routes") and not hasattr(route, "app"):
                # routes registered as one route and no path of its own -- app.frontend()'s group.
                # A mount carries routes too, and an application to route them by its own rules.
                yield from _served(route, prefix)
                continue
            inner = getattr(route, "original_router", None)
            context = getattr(route, "include_context", None)
            # a container this cannot descend into -- a Mount, a WebSocketRoute, whatever a later
            # fastapi introduces. It fails rather than skipping: a route object nobody enumerates is
            # exactly how four routes were served for a feature whose README said it served one.
            assert inner is not None, f"cannot enumerate a {type(route).__name__}; extend this"
            # the prefix comes off the same object, so a missing one fails here rather than folding
            # into "" and reporting every route behind the include at the wrong path
            assert context is not None, (
                f"cannot read the include prefix off a {type(route).__name__}; extend this"
            )
            yield from _served(inner, prefix + context.prefix)


def _answered(app, paths):
    """Every (path, method) a live app answers on, asked of the server rather than enumerated."""
    # never entered as a context manager, so no lifespan runs and no pool is ever opened
    client = TestClient(app)
    return {
        (path, method)
        for path in paths
        for method in EVERY_METHOD
        if client.request(method, path).status_code not in (404, 405)
    }


@pytest.fixture
def _reset_root_log_level():
    original = logging.getLogger().level
    yield
    logging.getLogger().setLevel(original)


def test_the_pool_is_built_without_connecting():
    pool = build_pool(DEAD_DSN)
    with pytest.raises(psycopg_pool.PoolClosed):
        with pool.connection(timeout=1.0):
            pass


def test_the_pool_is_built_with_the_configured_sizes(monkeypatch):
    monkeypatch.setattr(config.settings, "DB_POOL_MIN", 2)
    monkeypatch.setattr(config.settings, "DB_POOL_MAX", 7)
    configured = build_pool(DEAD_DSN)
    assert (configured.min_size, configured.max_size) == (2, 7)
    explicit = build_pool(DEAD_DSN, min_size=4, max_size=4)
    assert (explicit.min_size, explicit.max_size) == (4, 4)


def test_the_pool_is_built_with_a_liveness_check():
    pool = build_pool(DEAD_DSN)
    assert pool._check is psycopg_pool.ConnectionPool.check_connection


def test_an_unreachable_database_answers_internal():
    app = create_app(dsn=UNROUTED_DSN)
    # read before the lifespan opens anything: an app that dropped its argument would open a pool
    # against whatever the environment supplies, which is the database this argument keeps out
    assert app.state.pool.conninfo == UNROUTED_DSN
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal",
            "message": "the request could not be completed",
            "detail": None,
        }
    }


def test_health_runs_its_probe_query_on_the_pooled_connection(monkeypatch):
    app = create_app(dsn=DEAD_DSN)
    pool = _RecordingPool()
    app.state.pool = pool
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert pool.statements == ["SELECT 1"]
    # the installed metadata, not just a present key: a build_version naming the wrong distribution
    # reports "unknown" forever and every "version" in body assertion stays green
    assert response.json() == {"status": "ok", "version": version("market-data-platform")}
    # the lifespan's two halves, which nothing else in this suite observes
    assert (pool.opened, pool.closed) == (True, True)
    # an unbounded wait refuses to start the process while the database is briefly down, which is
    # the whole point of the health check it would be blocking
    assert pool.waits == [False]

    # the mechanism rather than today's value. The assertion above reads the same live metadata the
    # function does, so both sides move together and a build_version hardcoded to the current
    # version satisfies it -- which is the failure DL-010 names for message strings, here for a
    # version string. Patching the lookup fixes one side, and the argument is checked with it.
    # On its own app and pool, so the cumulative assertions above stay above one entry and the next
    # assertion anyone appends here cannot silently read two.
    monkeypatch.setattr("api.deps.version", lambda name: f"probe-{name}")
    probed = create_app(dsn=DEAD_DSN)
    probed.state.pool = _RecordingPool()
    with TestClient(probed) as client:
        assert client.get("/health").json()["version"] == "probe-market-data-platform"

    # and the branch the function exists to have, written out rather than imported: installed under
    # another name -- an sdist, a rename, a vendored copy -- /health publishes this string forever
    def _absent(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr("api.deps.version", _absent)
    unnamed = create_app(dsn=DEAD_DSN)
    unnamed.state.pool = _RecordingPool()
    with TestClient(unnamed) as client:
        assert client.get("/health").json()["version"] == "unknown"


def test_the_health_timeout_is_passed_to_the_pool(monkeypatch):
    # the default is read before it is patched: a test that overrides it every time is blind to a
    # changed default, and psycopg_pool's own 30 s makes /health slower than the checker consuming it
    assert api.main.HEALTH_TIMEOUT_SECONDS == 2.0
    monkeypatch.setattr("api.main.HEALTH_TIMEOUT_SECONDS", 9.5)
    app = create_app(dsn=DEAD_DSN)
    pool = _RecordingPool()
    app.state.pool = pool
    with TestClient(app) as client:
        client.get("/health")
    assert pool.timeouts == [9.5]


def test_the_lifespan_applies_the_configured_log_level(monkeypatch, _reset_root_log_level):
    # built before the patch on purpose: a create_app that configured logging in its own body would
    # read the unpatched level and still be green if the patch came first
    app = create_app(dsn=DEAD_DSN)
    monkeypatch.setattr(config.settings, "LOG_LEVEL", "ERROR")
    app.state.pool = _RecordingPool()
    root = logging.getLogger()
    installed = root.handlers[:]
    try:
        # with a handler already installed -- uvicorn's, in production -- basicConfig returns early
        # and the explicit setLevel is the only line that moves the level
        root.handlers[:] = [logging.NullHandler()]
        root.setLevel(logging.DEBUG)
        with TestClient(app):
            assert root.getEffectiveLevel() == logging.ERROR
            # httpx arrives through the vendor client at INFO and is not this app's traffic, so it
            # is pinned above the configured level rather than following it
            assert logging.getLogger("httpx").level == logging.WARNING
        # and with none installed, which is the only state in which basicConfig gets as far as its
        # format=. Cleared here rather than in the fixture, because pytest's own handler arrives
        # after fixture setup.
        root.handlers[:] = []
        root.setLevel(logging.DEBUG)
        with TestClient(app):
            # asserted through the formatter's output rather than its private format string, so it
            # pins what an operator actually reads
            record = logging.LogRecord("x", logging.ERROR, "p", 1, "the message", None, None)
            assert root.handlers[0].format(record) == "the message"
    finally:
        root.handlers[:] = installed


def test_the_app_serves_exactly_the_routes_this_feature_claims(tmp_path, monkeypatch):
    # enumerated rather than probed by name: every other test in this suite requests a path it
    # chose, so four routes FastAPI mounts by default were served for a feature whose README,
    # commit message and plan all say /health is the only one.
    #
    # The router tree, not app.routes and not the OpenAPI document. app.routes stops being the
    # surface at the first include_router -- from 0.137.0 fastapi appends one opaque wrapper rather
    # than copying the routes in -- and the document omits anything carrying
    # include_in_schema=False, which is how FastAPI registers /openapi.json, /docs and
    # /docs/oauth2-redirect themselves, so either alone reads as a clean pass while routes are
    # served. 0.138.0 added a second route list, which app.frontend() is the only thing
    # to reach and which appears in neither. pyproject floors fastapi at 0.110 with no ceiling, so
    # every one of those shapes installs.
    #
    # It over-reports in one shape, deliberately: a third-party BaseRoute carrying .methods behind
    # an include_router is dropped from fastapi's own dispatch and answers 404 while this lists it.
    # A guard that must never read clean over a served route can be wrong in that direction only.
    app = create_app(dsn=DEAD_DSN)
    # GET alone on the application routes, because FastAPI's APIRoute does not add the implicit HEAD
    # that Starlette's Route does -- and GET with HEAD on the three documentation routes, because
    # FastAPI registers those as Starlette Routes. ReDoc is absent on purpose
    assert set(_served(app.router)) == {
        ("/analytics/gaps", "GET"),
        ("/analytics/largest-moves", "GET"),
        ("/analytics/volatility", "GET"),
        ("/docs", "GET"),
        ("/docs", "HEAD"),
        ("/docs/oauth2-redirect", "GET"),
        ("/docs/oauth2-redirect", "HEAD"),
        ("/health", "GET"),
        ("/openapi.json", "GET"),
        ("/openapi.json", "HEAD"),
        ("/symbols", "GET"),
        ("/symbols/{symbol}/bars", "GET"),
        ("/symbols/{symbol}/daily", "GET"),
    }

    # and the walk against a scratch app, because the shipped app has no include prefix, no second
    # route list and no empty methods set, so a walk broken on any of those agrees with the correct
    # one here
    def _ok(request):
        return PlainTextResponse("ok")

    leaves = APIRouter()

    @leaves.get("/leaf")
    def leaf():
        return {}

    @leaves.get("/hidden", include_in_schema=False)
    def hidden():
        return {}

    middle = APIRouter(prefix="/mid")
    middle.include_router(leaves, prefix="/in")

    probe = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @probe.get("/plain")
    def plain():
        return {}

    probe.include_router(leaves)
    probe.include_router(middle, prefix="/out")
    # methods=[] is falsy, and starlette reads a falsy methods set as match-all
    probe.add_route("/ghost", _ok, methods=[])
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<h1>ui</h1>")
    probe.frontend("/ui", directory=str(ui))

    expected = {
        ("/plain", "GET"),
        ("/leaf", "GET"),
        ("/hidden", "GET"),
        ("/out/mid/in/leaf", "GET"),
        ("/out/mid/in/hidden", "GET"),
        ("/ui", "GET"),
        ("/ui", "HEAD"),
    } | {("/ghost", method) for method in EVERY_METHOD}
    assert set(_served(probe.router)) == expected
    # against the server rather than against a second enumeration, which would only agree with the
    # first one's mistakes
    assert _answered(probe, {path for path, _ in expected}) == expected

    # one pair per registration: the caller's set() is what collapses a router included twice, so
    # the docstring can only promise what the generator yields
    twice = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    twice.include_router(leaves)
    twice.include_router(leaves)
    assert sorted(_served(twice.router)) == [
        ("/hidden", "GET"),
        ("/hidden", "GET"),
        ("/leaf", "GET"),
        ("/leaf", "GET"),
    ]

    # a mount is an application routing by its own rules, and StaticFiles has no router at all: it
    # answers 200 on whatever directory it is given, so being skipped here is the failure
    files = tmp_path / "static"
    files.mkdir()
    (files / "f.txt").write_text("x")
    for mounted in (StaticFiles(directory=str(files)), FastAPI()):
        blind = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        blind.mount("/mounted", mounted)
        with pytest.raises(AssertionError, match="cannot enumerate a Mount"):
            set(_served(blind.router))

    # a route list this walk has never heard of, which is what app.frontend()'s was before 0.138.0
    surprise = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    surprise.router.__dict__["_later_routes"] = list(leaves.routes)
    with pytest.raises(AssertionError, match="_later_routes"):
        set(_served(surprise.router))

    contextless = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    contextless.include_router(leaves, prefix="/pfx")
    monkeypatch.delattr(contextless.router.routes[-1], "include_context")
    with pytest.raises(AssertionError, match="cannot read the include prefix"):
        set(_served(contextless.router))


def test_a_trailing_slash_is_refused_in_the_one_error_shape_rather_than_redirected():
    # a 307 carries no body, so it is the one response that escapes the single error shape
    app = create_app(dsn=DEAD_DSN)
    app.state.pool = _RecordingPool()
    with TestClient(app) as client:
        for response in (client.get("/health/"), client.post("/health/")):
            assert response.status_code == 404
            assert response.headers["content-type"] == "application/json"
            assert response.json() == {
                "error": {
                    "code": "invalid_params",
                    "message": "no route matches this path and method",
                    "detail": {"reason": "unknown_route", "path": "/health/"},
                }
            }


def test_the_sentinel_used_for_a_first_page_is_below_every_ingestible_bar():
    # page 1 binds a sentinel to the same exclusive cursor predicate page 2 uses, so one SQL text
    # serves both. A sentinel above the window's own lower bound silently drops page 1's earliest
    # bars, and no row count anywhere would notice
    lo, _ = _instant_bounds(config.settings.INGEST_START, config.settings.INGEST_END)
    assert api.routes._BEFORE_ANY_BAR < lo
    # and the symbol key's: symbol is the primary key and therefore NOT NULL, so every real row
    # sorts strictly above the empty string
    assert api.routes._BEFORE_ANY_SYMBOL == ""


def test_each_endpoint_carries_its_own_page_caps_and_defaults_to_its_own(monkeypatch):
    bars_pair = (config.settings.BARS_PAGE_DEFAULT, config.settings.BARS_PAGE_MAX)
    agg_pair = (config.settings.AGG_PAGE_DEFAULT, config.settings.AGG_PAGE_MAX)
    # asserted first, and it is what makes the equalities below discriminate rather than
    # tautologise: if the two pairs held the same values, handing one pair to every endpoint would pass
    assert bars_pair != agg_pair

    # spec line 508 classes /symbols and /bars raw-row and /daily aggregating, and puts the three
    # analytics endpoints under the aggregating pair with /daily
    assert api.routes._SYMBOLS_CAPS == bars_pair
    assert api.routes._BARS_CAPS == bars_pair
    assert api.routes._DAILY_CAPS == agg_pair
    assert api.routes._VOLATILITY_CAPS == agg_pair
    assert api.routes._GAPS_CAPS == agg_pair
    assert api.routes._MOVES_CAPS == agg_pair

    # an omitted limit resolves to the DEFAULT and not to the cap -- unobservable from any
    # response this feature can produce, since 100 and 1000 are both above every fixture
    _, resolved = api.routes.resolve_request(raw_limit=None, page_default=3, page_max=50)
    assert resolved == 3

    # WHICH pair each endpoint READS, and which HALF of it, neither of which the equalities
    # above can see: _SYMBOLS_CAPS and _BARS_CAPS hold identical values, so a transposition
    # between them -- or /bars reading the aggregating pair -- changes no response the suite asks
    # for. Every /bars and /daily limit in the integration files is 10 or 50, and every fixture is
    # smaller than either default, so nothing this feature can produce distinguishes a default of
    # 100 from one of 1000 either. Each constant gets a distinct sentinel and each endpoint must
    # name its own back.
    #
    # Each pair is deliberately INVERTED -- default above max -- which config's validator forbids
    # of the real settings and nothing checks of these module constants. That is what lets one
    # call pin both halves: an omitted limit resolves to the default and then fails the same range
    # check, so the refusal names the default in `limit` and the cap in `max`. Pinning `max` alone
    # leaves the default free, and /bars taking the aggregating default while keeping its own cap
    # then serves a 100-row page where 1000 is specified, with the whole suite green.
    #
    # All twelve numbers are distinct and the three analytics pairs continue the same series: a
    # number shared between two pairs leaves a transposition between them caught by one half
    # instead of both, which re-creates for that pair the gap inverting the pairs exists to close.
    sentinels = {
        "_SYMBOLS_CAPS": (11, 10),
        "_BARS_CAPS": (21, 20),
        "_DAILY_CAPS": (31, 30),
        "_VOLATILITY_CAPS": (41, 40),
        "_GAPS_CAPS": (51, 50),
        "_MOVES_CAPS": (61, 60),
    }
    for name, pair in sentinels.items():
        monkeypatch.setattr(api.routes, name, pair)
    window = {"start": date(2026, 3, 9), "end": date(2026, 3, 12)}
    # pool=None is safe and is the point: resolve_request runs the 400 tier and raises before
    # pool.connection() is entered, so a handler that reached for a connection first would fail
    # here with AttributeError rather than the ApiError this asserts
    for endpoint, kwargs, pair in (
        (api.routes.list_symbols, {}, sentinels["_SYMBOLS_CAPS"]),
        (api.routes.list_bars, {"symbol": "AAA"} | window, sentinels["_BARS_CAPS"]),
        (api.routes.list_daily, {"symbol": "AAA"} | window, sentinels["_DAILY_CAPS"]),
        (api.routes.analytics_largest_moves, dict(window), sentinels["_MOVES_CAPS"]),
    ):
        default, cap = pair
        with pytest.raises(ApiError) as excinfo:
            endpoint(limit=9999, pool=None, **kwargs)
        assert excinfo.value.detail["max"] == cap, endpoint.__name__
        # and the DEFAULT half, which the call above cannot see because it supplies a raw limit
        with pytest.raises(ApiError) as excinfo:
            endpoint(limit=None, pool=None, **kwargs)
        assert excinfo.value.detail == {
            "reason": "limit_out_of_range",
            "limit": default,
            "max": cap,
        }, endpoint.__name__

    # /analytics/volatility and /analytics/gaps declare no limit parameter at all, so the loop
    # above cannot drive them: a caps pair a caller cannot address cannot be driven over its cap.
    # They reach the inverted default through an unsupplied raw_limit instead, which is the whole
    # reason the pairs are inverted -- one call pins both halves
    for endpoint, pair in (
        (api.routes.analytics_volatility, sentinels["_VOLATILITY_CAPS"]),
        (api.routes.analytics_gaps, sentinels["_GAPS_CAPS"]),
    ):
        default, cap = pair
        with pytest.raises(ApiError) as excinfo:
            endpoint(pool=None, symbol="AAA", **window)
        assert excinfo.value.detail == {
            "reason": "limit_out_of_range",
            "limit": default,
            "max": cap,
        }, endpoint.__name__
        # and that the signature really carries no limit, which is what keeps a parameter that
        # does nothing out of the generated OpenAPI document
        with pytest.raises(TypeError):
            endpoint(limit=9999, pool=None, symbol="AAA", **window)


def test_the_range_tier_tolerates_a_window_with_only_one_bound():
    # no endpoint passes exactly one bound, so nothing reachable over HTTP discriminates this:
    # written as `or`, the first call reaches `start > end` against None and raises TypeError,
    # which a route would answer as a 500
    assert api.routes.resolve_request(
        page_default=100, page_max=1000, start=date(2026, 4, 1), end=None
    ) == (None, 100)
    assert api.routes.resolve_request(
        page_default=100, page_max=1000, start=None, end=date(2026, 6, 30)
    ) == (None, 100)


def test_the_generated_documentation_answers_and_redoc_does_not(monkeypatch):
    # a response rather than a second enumeration: the route-surface literal proves registration,
    # and only a request shows that the document and the page answer
    #
    # a sentinel version, because FastAPI's own default is also 0.1.0 -- the version the package
    # reports today -- so comparing against the real one passes with version= deleted
    monkeypatch.setattr(api.main, "build_version", lambda: "9.9.9-sentinel")
    client = TestClient(create_app(dsn=DEAD_DSN))
    document = client.get("/openapi.json")
    assert document.status_code == 200
    body = document.json()
    assert body["info"] == {"title": "Market Data Platform", "version": "9.9.9-sentinel"}
    paths = body["paths"]
    # written out as literals, never tested for presence: FastAPI fills an absent summary from the
    # function name ("list_daily" publishes as "List Daily"), so a presence check passes with every
    # summary deleted and the page back to reading as a list of function names
    assert {path: item["get"]["summary"] for path, item in paths.items()} == {
        "/analytics/gaps": "Overnight gap distribution for one symbol",
        "/analytics/largest-moves": "Minute moves at or above a threshold, universe-wide",
        "/analytics/volatility": "Realized volatility by half-hour bucket",
        "/health": "Service and database health",
        "/symbols": "List ingested symbols",
        "/symbols/{symbol}/bars": "Minute bars for one symbol",
        "/symbols/{symbol}/daily": "Daily bars for one symbol",
    }
    # the constant and not a truthiness check: FastAPI falls back to the endpoint's docstring, so a
    # docstring added later would keep a presence check green with the description deleted
    assert paths["/symbols/{symbol}/bars"]["get"]["description"] == api.routes._BARS_DESCRIPTION
    # a constrained Decimal renders as anyOf(number, string) with the bound on the number branch, so
    # a top-level schema.get("minimum") reads None with and without ge=0 and could not fail
    parameters = {p["name"]: p for p in paths["/analytics/largest-moves"]["get"]["parameters"]}
    branches = parameters["min_move_pct"]["schema"]["anyOf"]
    assert [b.get("minimum") for b in branches if b.get("type") == "number"] == [0.0]

    page = client.get("/docs")
    assert page.status_code == 200
    assert page.headers["content-type"].split(";")[0] == "text/html"

    # ReDoc is off, so its default path is unrouted and answers the 404 every unknown path does
    redoc = client.get("/redoc")
    assert redoc.status_code == 404
    assert redoc.json()["error"]["code"] == "invalid_params"
