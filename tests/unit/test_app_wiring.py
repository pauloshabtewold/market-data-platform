import contextlib
import logging
from importlib.metadata import version

import psycopg_pool
import pytest
from fastapi.testclient import TestClient

import api.main
import config
from api.deps import build_pool
from api.main import create_app

DEAD_DSN = "postgresql://nobody:nobody@127.0.0.1:1/none"


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
        self.opened = False
        self.closed = False

    @contextlib.contextmanager
    def connection(self, timeout=None):
        self.timeouts.append(timeout)
        yield _RecordingConnection(self.statements)

    def open(self, wait=False):
        self.opened = True

    def close(self):
        self.closed = True


def _served(router, prefix=""):
    """Every (path, method) a router answers, descending into the routers included in it."""
    for route in router.routes:
        methods = getattr(route, "methods", None)
        if methods is not None:
            for method in methods:
                yield prefix + route.path, method
            continue
        inner = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        # a container this cannot descend into -- a Mount, a WebSocketRoute, whatever a later
        # fastapi introduces. It fails rather than skipping: a route object nobody enumerates is
        # exactly how four routes were served for a feature whose README said it served one.
        assert inner is not None, f"cannot enumerate a {type(route).__name__}; extend this"
        yield from _served(inner, prefix + (getattr(context, "prefix", "") or ""))


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
    with TestClient(create_app(dsn=DEAD_DSN)) as client:
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

    # and the mechanism rather than today's value. The assertion above reads the same live metadata
    # the function does, so both sides move together and a build_version hardcoded to the current
    # version satisfies it -- which is the failure DL-010 names for message strings, here for a
    # version string. Patching the lookup fixes one side, and the argument is checked with it.
    monkeypatch.setattr("api.deps.version", lambda name: f"probe-{name}")
    with TestClient(app) as client:
        assert client.get("/health").json()["version"] == "probe-market-data-platform"


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
    # basicConfig returns early once the root logger has any handler, and pytest installs one for
    # the call phase, so with it in place configure_logging's format= is applied to nothing and no
    # assertion can reach the third of its three lines. Cleared here rather than in the fixture,
    # because the plugin's handler arrives after fixture setup.
    installed = root.handlers[:]
    root.handlers[:] = []
    try:
        with TestClient(app):
            assert root.getEffectiveLevel() == logging.ERROR
            # httpx arrives through the vendor client at INFO and is not this app's traffic, so it
            # is pinned above the configured level rather than following it
            assert logging.getLogger("httpx").level == logging.WARNING
            # asserted through the formatter's output rather than its private format string, so it
            # pins what an operator actually reads
            record = logging.LogRecord("x", logging.ERROR, "p", 1, "the message", None, None)
            assert root.handlers[0].format(record) == "the message"
    finally:
        root.handlers[:] = installed


def test_the_app_serves_exactly_the_routes_this_feature_claims():
    # enumerated rather than probed by name: every other test in this suite requests a path it
    # chose, so four routes FastAPI mounts by default were served for a feature whose README,
    # commit message and plan all say /health is the only one.
    #
    # The router tree, not app.routes and not the OpenAPI document. app.routes stops being the
    # surface at the first include_router -- since 0.141 fastapi appends one opaque wrapper rather
    # than copying the routes in -- and the document omits anything carrying include_in_schema=False
    # as well as /docs and /openapi.json themselves, so either alone reads as a clean pass while
    # routes are served. Verified across both prefix forms, nesting, and include_in_schema=False.
    # Out of scope, deliberately: redirect_slashes is a property of this app's own router and does
    # not reach a mounted sub-application.
    app = create_app(dsn=DEAD_DSN)
    # GET alone: FastAPI's APIRoute does not add the implicit HEAD that Starlette's Route does
    assert set(_served(app.router)) == {("/health", "GET")}


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
