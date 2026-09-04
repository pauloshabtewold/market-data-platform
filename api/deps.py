from importlib.metadata import PackageNotFoundError, version

from fastapi import Request
from psycopg_pool import ConnectionPool

from config import settings


def _pin_utc(conn):
    # the pool makes its own connections and never calls db.session.connect, so the zone that file pins per connection has to be pinned again here
    conn.execute("SET TIME ZONE 'UTC'")
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
        configure=_pin_utc,
        check=ConnectionPool.check_connection,
    )


def get_pool(request: Request) -> ConnectionPool:
    return request.app.state.pool


def build_version() -> str:
    try:
        return version("market-data-platform")
    except PackageNotFoundError:
        return "unknown"
