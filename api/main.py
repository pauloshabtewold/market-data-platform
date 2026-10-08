import asyncio
import logging
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from psycopg import pq
from pydantic import BaseModel

from api.deps import build_pool, build_version, mask_secrets
from api.errors import INTERNAL_MESSAGE, RESPONSE_500, ApiError, install_error_handlers
from api.routes import router
from config import hot_window_configuration_problems, settings

# well under an ALB's 5 s default health-check timeout. The check runs on its own connection, off
# the pool and the thread limiter, so a burst elsewhere cannot delay it
HEALTH_TIMEOUT_SECONDS = 2.0

log = logging.getLogger(__name__)


# via the route's `responses`, never response_model. A comment and not a docstring: a docstring on
# a published model becomes that schema's `description` in every generated client
class HealthResponse(BaseModel):
    status: str
    version: str


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(message)s")
    # basicConfig returns early if the root logger has a handler; uvicorn and pytest install one
    logging.getLogger().setLevel(level)
    # httpx logs every request at INFO and arrives through the vendor and test clients, not this app
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging(settings.LOG_LEVEL)
    pool = app.state.pool
    # an unbounded wait would refuse to start while the database is briefly down, defeating /health
    pool.open(wait=False)
    try:
        yield
    finally:
        try:
            # a check in flight would otherwise hold shutdown for the rest of its deadline
            await app.state.checks.close()
        finally:
            # its own finally: an escape above would leave the pool's server connections
            # open for the process's lifetime
            pool.close()


async def _socket_ready(add_watcher, remove_watcher, fd: int) -> None:
    ready = asyncio.get_running_loop().create_future()
    add_watcher(fd, lambda: ready.done() or ready.set_result(None))
    try:
        await ready
    finally:
        # removed before the socket closes: asyncio keeps a watcher on a closed descriptor, so the
        # next socket reusing that number gets none
        remove_watcher(fd)


async def _select_one(pgconn: pq.abc.PGconn) -> None:
    loop = asyncio.get_running_loop()
    # at the libpq level: on cancellation psycopg's own execute sends a cancel and waits again with
    # no deadline, holding the response as long as a frozen server stays frozen
    pgconn.send_query(b"SELECT 1")
    fd = pgconn.socket
    while pgconn.flush():
        await _socket_ready(loop.add_writer, loop.remove_writer, fd)
    pgconn.consume_input()
    while pgconn.is_busy():
        await _socket_ready(loop.add_reader, loop.remove_reader, fd)
        pgconn.consume_input()
    answers = []
    while (result := pgconn.get_result()) is not None:
        if result.status == pq.ExecStatus.TUPLES_OK and result.ntuples == 1:
            answers.append(result.get_value(0, 0))
        else:
            # the status beside libpq's message: a result carrying no error still gets the non-empty
            # placeholder "no error details available", so the message alone names nothing
            answers.append((pq.ExecStatus(result.status).name, result.get_error_message()))
    # a refused query answers with an error result rather than raising
    if answers != [b"1"]:
        raise psycopg.OperationalError(f"SELECT 1 answered {answers!r}")


async def _check_database(dsn: str) -> None:
    # a short-lived connection of its own, never the pool: a request holding every pooled connection
    # must not make this read as a dead database. libpq's connect_timeout bounds connect, in seconds
    conn = await psycopg.AsyncConnection.connect(dsn, connect_timeout=HEALTH_TIMEOUT_SECONDS)
    try:
        await _select_one(conn.pgconn)
    finally:
        # closes the socket at once, answered or cancelled at the deadline
        await conn.close()


async def _bounded_check(dsn: str) -> None:
    try:
        # cancelling _check_database removes its watcher and closes its socket, so the deadline ends
        # this, not the server
        await asyncio.wait_for(_check_database(dsn), timeout=HEALTH_TIMEOUT_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    # outside the except: the timeout's chain holds the frames of a connect cancelled mid-startup,
    # which hold its socket open while kept
    raise TimeoutError(f"the database did not answer within {HEALTH_TIMEOUT_SECONDS} s")


def _failed_check_logger(dsn: str):
    def log_failure(task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is None:
            return
        exc = task.exception()
        # once per check, however many probes shared it. libpq echoes an unparseable connection
        # string, password included, so the line is masked against this app's own dsn
        log.warning(
            "database check failed: %s: %s",
            type(exc).__name__,
            mask_secrets(str(exc), dsn),
        )

    return log_failure


class _SharedCheck:
    """At most one database check in flight per app; probes that arrive while it runs wait on it
    rather than each opening a connection of their own."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self, dsn: str) -> asyncio.Task:
        task = self._task
        if task is None or task.done():
            task = self._task = asyncio.get_running_loop().create_task(_bounded_check(dsn))
            task.add_done_callback(_failed_check_logger(dsn))
        return task

    async def close(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        # awaited so the cancellation lands and the socket closes before this loop is torn down;
        # gathered so the task's own CancelledError is absorbed while one aimed at the caller lands
        await asyncio.gather(task, return_exceptions=True)


def create_app(dsn: str | None = None) -> FastAPI:
    # not in Settings, so db.migrate and the ingest start on a config only the API cannot serve
    problems = hot_window_configuration_problems(settings)
    if problems:
        raise RuntimeError("refusing to build the app:\n- " + "\n- ".join(problems))
    app = FastAPI(
        lifespan=_lifespan,
        # /health's own version: FastAPI defaults to a literal 0.1.0, stale at the first release
        title="Market Data Platform",
        version=build_version(),
        # docs_url is honoured only while openapi_url is set, so that line publishes or withdraws
        # the whole surface
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        # a 307 to the unslashed path has no body, escaping the single error shape. Governs this
        # app's own router only -- an app.mount()ed sub-app keeps its own
        redirect_slashes=False,
    )
    install_error_handlers(app)
    app.include_router(router)
    # an explicit dsn keeps tests and the testcontainer off settings.DATABASE_URL; on app.state
    # beside the pool built from it, so /health opens its own connection from the same dsn
    resolved_dsn = dsn or settings.DATABASE_URL
    app.state.dsn = resolved_dsn
    app.state.pool = build_pool(resolved_dsn)

    checks = _SharedCheck()
    # on app.state so the lifespan can end a check in flight at shutdown
    app.state.checks = checks

    @app.get(
        "/health",
        summary="Service and database health",
        responses={
            200: {"model": HealthResponse, "description": "The service answered and the database did."},
            500: RESPONSE_500,
        },
    )
    async def health():
        try:
            # shielded: a probe that gives up must not cancel the check others share
            await asyncio.wait_for(
                asyncio.shield(checks.start(app.state.dsn)), timeout=HEALTH_TIMEOUT_SECONDS
            )
        except Exception as exc:
            # covers a connection/query failure and wait_for's own asyncio.TimeoutError
            raise ApiError(500, "internal", INTERNAL_MESSAGE, None) from exc
        return {"status": "ok", "version": build_version()}

    return app


app = create_app()
