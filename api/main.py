import asyncio
import logging
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI

from api.deps import build_pool, build_version
from api.errors import INTERNAL_MESSAGE, RESPONSE_500, ApiError, install_error_handlers
from api.routes import router
from config import settings

# bounded well under an ALB's 5 s default health-check timeout, with room for the probe itself.
# genuinely a bound now: /health runs on its own connection (below), off the shared pool and off
# the 40-thread limiter every other (sync def) route shares, so a burst of slow requests elsewhere
# cannot make this wait behind them
HEALTH_TIMEOUT_SECONDS = 2.0


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(message)s")
    # basicConfig returns early once the root logger already has a handler, and uvicorn and pytest both install one before this ever runs
    logging.getLogger().setLevel(level)
    # httpx arrives through the vendor client and the test client rather than through this app, and
    # logs every request at INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging(settings.LOG_LEVEL)
    pool = app.state.pool
    # unbounded wait would refuse to start the process while the database is briefly down, defeating the point of a health check
    pool.open(wait=False)
    yield
    pool.close()


async def _check_database(dsn: str) -> None:
    # /health's own short-lived connection, never the shared pool: a request that is holding every
    # pooled connection or every thread in the sync limiter must not make this read as a dead
    # database. connect_timeout is a libpq connection parameter (seconds); it bounds the connect
    # phase only, and the asyncio.wait_for around this call in the route bounds the query too
    async with await psycopg.AsyncConnection.connect(
        dsn, connect_timeout=HEALTH_TIMEOUT_SECONDS
    ) as conn:
        await conn.execute("SELECT 1")


def create_app(dsn: str | None = None) -> FastAPI:
    app = FastAPI(
        lifespan=_lifespan,
        # the same version /health reports: FastAPI's own default is a literal 0.1.0 that would stop
        # matching the package at its first release
        title="Market Data Platform",
        version=build_version(),
        # generated from the routes themselves, so the page cannot describe an endpoint that is not
        # served. docs_url is honoured only while openapi_url is set, which makes openapi_url the
        # line that publishes or withdraws the whole surface; ReDoc stays off because the surface is
        # one generated page, not two renderings of the same document
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        # a 307 to the unslashed path carries no body, so it is the one response that escapes the
        # single error shape; off, an unrouted /health/ is the 404 the handler already builds.
        # It governs this app's own router, which every include_router route joins -- a sub-app
        # added with app.mount() keeps its own router and its own 307.
        redirect_slashes=False,
    )
    install_error_handlers(app)
    app.include_router(router)
    # an explicit dsn lets tests and the testcontainer avoid ever touching settings.DATABASE_URL.
    # kept on app.state itself, alongside the pool built from it, so /health can open its own
    # connection from the same dsn without ever reaching for settings.DATABASE_URL either
    resolved_dsn = dsn or settings.DATABASE_URL
    app.state.dsn = resolved_dsn
    app.state.pool = build_pool(resolved_dsn)

    @app.get("/health", summary="Service and database health", responses={500: RESPONSE_500})
    async def health():
        try:
            await asyncio.wait_for(_check_database(app.state.dsn), timeout=HEALTH_TIMEOUT_SECONDS)
        except Exception as exc:
            # covers both a connection/query failure and asyncio.TimeoutError from wait_for itself
            raise ApiError(500, "internal", INTERNAL_MESSAGE, None) from exc
        return {"status": "ok", "version": build_version()}

    return app


app = create_app()
