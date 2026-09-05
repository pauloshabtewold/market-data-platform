import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from psycopg_pool import ConnectionPool

from api.deps import build_pool, build_version, get_pool
from api.errors import INTERNAL_MESSAGE, ApiError, install_error_handlers
from config import settings

# bounded well under an ALB's 5 s default health-check timeout, with room for the probe itself
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


def create_app(dsn: str | None = None) -> FastAPI:
    app = FastAPI(
        lifespan=_lifespan,
        # section 1 assigns the OpenAPI surface to Feature 7; left at its defaults FastAPI mounts
        # /docs, /redoc, /docs/oauth2-redirect and /openapi.json, which publishes the service
        # description of a scaffold that is about to go behind an ALB
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # a 307 to the unslashed path carries no body, so it is the one response that escapes the
        # single error shape; off, an unrouted /health/ is the 404 the handler already builds.
        # It governs this app's own router, which every include_router route joins -- a sub-app
        # added with app.mount() keeps its own router and its own 307.
        redirect_slashes=False,
    )
    install_error_handlers(app)
    # an explicit dsn lets tests and the testcontainer avoid ever touching settings.DATABASE_URL
    app.state.pool = build_pool(dsn or settings.DATABASE_URL)

    @app.get("/health")
    def health(pool: ConnectionPool = Depends(get_pool)):
        try:
            with pool.connection(timeout=HEALTH_TIMEOUT_SECONDS) as conn:
                conn.execute("SELECT 1")
        except Exception as exc:
            # psycopg_pool already logs the underlying cause at WARNING when a connection attempt fails
            raise ApiError(500, "internal", INTERNAL_MESSAGE, None) from exc
        return {"status": "ok", "version": build_version()}

    return app


app = create_app()
