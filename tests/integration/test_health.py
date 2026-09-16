import threading
import time

import psycopg.errors
import pytest
from fastapi.testclient import TestClient

import api.deps as deps
import config
from api.main import create_app
from api.pagination import BARS_CURSOR, decode_cursor, encode_cursor
from db.session import connect


def _database(dsn: str) -> str:
    return dsn.rsplit("/", 1)[1]


def test_health_answers_two_hundred_against_a_migrated_database(migrated_dsn):
    with TestClient(create_app(migrated_dsn)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_a_pooled_connection_reports_utc(migrated_dsn):
    with connect(migrated_dsn) as conn:
        conn.autocommit = True
        conn.execute(
            f'ALTER DATABASE "{_database(migrated_dsn)}" SET TimeZone TO \'America/New_York\''
        )

    app = create_app(migrated_dsn)
    with TestClient(app):
        with app.state.pool.connection() as conn:
            # dicts and not tuples: the pool declares a row factory, so these two comparisons
            # are also the loudest signal that it reached psycopg.connect
            assert conn.execute("SHOW TimeZone").fetchone() == {"TimeZone": "UTC"}
            assert conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'TimeZone' AND source = 'session'"
            ).fetchone() == {"setting": "UTC"}


def test_a_pooled_connection_yields_dict_rows_so_a_next_cursor_can_be_built(migrated_dsn):
    # the fast kill: paginate indexes rows[limit - 1] by the cursor's field names, and it returns
    # early whenever the rows fit inside the limit -- so an endpoint written against tuple rows
    # passes every integration test with a fixture smaller than its limit and 500s on the first
    # request that has a page 2. This one needs no page boundary at all.
    app = create_app(migrated_dsn)
    with TestClient(app):
        with app.state.pool.connection() as conn:
            # the coupling itself, and not a second SHOW TimeZone: asserting the row shape is
            # what the test above already does, so repeating it here adds no kill. What nothing
            # else reaches without a page boundary is encode_cursor being handed a POOLED row --
            # it indexes by shape.fields, so a tuple row raises TypeError on the index and a
            # column aliased anything but `ts` raises KeyError
            row = conn.execute(
                "SELECT now() AT TIME ZONE 'UTC' AT TIME ZONE 'UTC' AS ts, 1 AS ignored"
            ).fetchone()
            assert set(row) == {"ts", "ignored"}
            assert decode_cursor(BARS_CURSOR, encode_cursor(BARS_CURSOR, row))["ts"] == row["ts"]


def test_a_pooled_connection_carries_the_configured_statement_timeout(migrated_dsn):
    # the raw setting in pg_settings, in the GUC's own base unit (ms) -- SHOW's own formatted text
    # ("5s") is a presentation detail this assertion does not need to reproduce
    app = create_app(migrated_dsn)
    with TestClient(app):
        with app.state.pool.connection() as conn:
            assert conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
            ).fetchone() == {"setting": str(int(deps.STATEMENT_TIMEOUT_SECONDS * 1000))}


def test_a_statement_past_the_timeout_is_cancelled(migrated_dsn, monkeypatch):
    # small and monkeypatched rather than the 5 s default -- a fresh pool built after the patch
    # picks it up because _pin_utc reads the module attribute by name, not a bound default
    monkeypatch.setattr(deps, "STATEMENT_TIMEOUT_SECONDS", 0.2)
    app = create_app(migrated_dsn)
    with TestClient(app):
        with app.state.pool.connection() as conn:
            with pytest.raises(psycopg.errors.QueryCanceled):
                conn.execute("SELECT pg_sleep(2)")


def test_health_answers_even_while_the_only_pooled_connection_is_held(migrated_dsn, monkeypatch):
    # DB_POOL_MAX read by build_pool at call time, so patching settings before create_app narrows
    # the pool this app builds to exactly one connection
    monkeypatch.setattr(config.settings, "DB_POOL_MIN", 1)
    monkeypatch.setattr(config.settings, "DB_POOL_MAX", 1)
    monkeypatch.setattr(deps, "POOL_CHECKOUT_TIMEOUT_SECONDS", 0.3)
    app = create_app(migrated_dsn)
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold_the_only_connection():
        with app.state.pool.connection() as conn:
            conn.execute("SELECT 1")
            holder_ready.set()
            release_holder.wait(10)

    holder = threading.Thread(target=hold_the_only_connection)
    # raise_server_exceptions=False: the default client re-raises PoolTimeout out of the call
    # instead of returning the 500 response the installed handler already built for it
    with TestClient(app, raise_server_exceptions=False) as client:
        holder.start()
        assert holder_ready.wait(10)
        try:
            # the pool's only connection is held: a data request that needs one is refused rather
            # than hanging until the client gives up
            start = time.monotonic()
            response = client.get("/symbols")
            elapsed = time.monotonic() - start
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "internal"
            assert elapsed < 2.0
            # /health never reaches for the pool, so an exhausted pool cannot make it read as a
            # dead database -- this is the D-440 fix
            assert client.get("/health").status_code == 200
        finally:
            release_holder.set()
            holder.join(10)
