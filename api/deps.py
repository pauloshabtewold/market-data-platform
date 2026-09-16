from importlib.metadata import PackageNotFoundError, version

from fastapi import Request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import settings

# module-level rather than inlined, so a test can monkeypatch either down to something fast:
# how long a request waits for a pool slot before PoolTimeout, and how long a statement may run
# on a pooled connection before Postgres cancels it, so one slow client or one stuck query cannot
# starve every other request of the pool
POOL_CHECKOUT_TIMEOUT_SECONDS = 5.0
STATEMENT_TIMEOUT_SECONDS = 5.0


def _pin_utc(conn):
    # the pool makes its own connections and never calls db.session.connect, so the zone that file pins per connection has to be pinned again here
    conn.execute("SET TIME ZONE 'UTC'")
    # read by name rather than captured as a default argument, so a monkeypatch of the module
    # attribute reaches every connection this callback configures from here on
    conn.execute(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_SECONDS * 1000)}")
    conn.commit()


def build_pool(
    dsn: str, min_size: int | None = None, max_size: int | None = None
) -> ConnectionPool:
    # resolved here rather than defaulted in the signature -- a default argument is evaluated once at import, so monkeypatching settings afterwards would not change it
    min_size = settings.DB_POOL_MIN if min_size is None else min_size
    max_size = settings.DB_POOL_MAX if max_size is None else max_size
    return ConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        open=False,
        # paginate indexes a row by the cursor's field names, so a pooled connection yielding
        # psycopg's default tuples 500s on the first page that has a successor -- and never
        # before, since paginate returns early whenever the rows fit inside the limit.
        # declared here rather than inside the configure callback so it survives a later feature
        # replacing that callback
        kwargs={"row_factory": dict_row},
        configure=_pin_utc,
        check=ConnectionPool.check_connection,
        # psycopg_pool's own default of 30.0 s is what let a burst of slow requests make every
        # later checkout, /health's included, wait long enough to fail its own 2 s bound
        timeout=POOL_CHECKOUT_TIMEOUT_SECONDS,
    )


def get_pool(request: Request) -> ConnectionPool:
    return request.app.state.pool


def build_version() -> str:
    try:
        return version("market-data-platform")
    except PackageNotFoundError:
        return "unknown"
