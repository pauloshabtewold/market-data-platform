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

# bounded well under an ALB's 5 s default health-check timeout, with room for the probe itself.
# /health's own check runs on the event loop and on its own connection, off the shared pool and off
# the 40-thread limiter every sync def route shares, so a burst of slow requests elsewhere cannot
# delay the check. The 500 it answers with is built by the installed ApiError handler, which holds
# this bound under such a burst only while that handler is itself async
HEALTH_TIMEOUT_SECONDS = 2.0

log = logging.getLogger(__name__)


# The shape of a healthy 200, declared through the route's `responses` and never as response_model,
# for the same reason the page models are. A comment and not a docstring: a docstring on a published
# model becomes that schema's `description`, which every generated client carries as its own class
# documentation and every reader of the page sees -- wrapped across source lines, as one run-on line.
# What /health answers is described client-facing in the route's own 200 entry instead.
class HealthResponse(BaseModel):
    status: str
    version: str


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
    try:
        yield
    finally:
        try:
            # a check still in flight would otherwise hold shutdown for the rest of its own deadline
            await app.state.checks.close()
        finally:
            # in a finally of its own: the pool holds server connections, and anything escaping the
            # line above would otherwise leave them open for the process's lifetime
            pool.close()


async def _socket_ready(add_watcher, remove_watcher, fd: int) -> None:
    ready = asyncio.get_running_loop().create_future()
    add_watcher(fd, lambda: ready.done() or ready.set_result(None))
    try:
        await ready
    finally:
        # removed before the socket can close: asyncio keeps a watcher on a closed descriptor, and
        # the next socket to reuse that number then never has its own watcher registered
        remove_watcher(fd)


async def _select_one(pgconn: pq.abc.PGconn) -> None:
    loop = asyncio.get_running_loop()
    # sent and read at the libpq level: on cancellation psycopg's own execute sends a cancel
    # request and then waits for the query again with no deadline, which holds the response for
    # as long as a server that connected and then froze stays frozen
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
            # the status beside libpq's own message, the way the pool's check reports one: psycopg
            # returns a non-empty placeholder for a result that carries no error, so a message alone
            # names nothing when the answer is well formed and simply not one row -- a COMMAND_OK,
            # an empty query and a two-row answer all read "no error details available"
            answers.append((pq.ExecStatus(result.status).name, result.get_error_message()))
    # a connection that is refused its query answers with an error result rather than raising
    if answers != [b"1"]:
        raise psycopg.OperationalError(f"SELECT 1 answered {answers!r}")


async def _check_database(dsn: str) -> None:
    # /health's own short-lived connection, never the shared pool: a request that is holding every
    # pooled connection must not make this read as a dead database. connect_timeout is a libpq
    # connection parameter (seconds) and bounds the connect phase
    conn = await psycopg.AsyncConnection.connect(dsn, connect_timeout=HEALTH_TIMEOUT_SECONDS)
    try:
        await _select_one(conn.pgconn)
    finally:
        # closes the socket at once, whether the check answered or its deadline cancelled it
        await conn.close()


async def _bounded_check(dsn: str) -> None:
    try:
        # cancelling _check_database removes its socket watcher and closes its socket, so this
        # returns at the deadline rather than when the server does
        await asyncio.wait_for(_check_database(dsn), timeout=HEALTH_TIMEOUT_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    # raised outside the except block: the timeout's own exception chain holds the frames of a
    # connect cancelled mid-startup, and those hold its socket open for as long as it is kept
    raise TimeoutError(f"the database did not answer within {HEALTH_TIMEOUT_SECONDS} s")


def _failed_check_logger(dsn: str):
    def log_failure(task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is None:
            return
        exc = task.exception()
        # once per check, however many probes shared it: the class and libpq's message tell an
        # authentication failure, a full server, a DNS failure and a timeout apart. libpq quotes a
        # connection string it cannot parse back in that message, password included, so what is
        # logged is masked against the string this app was built with
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
        # awaited so the cancellation lands and the check's own socket is closed before the loop
        # this runs on is torn down. gathered rather than awaited directly, so the task's own
        # CancelledError is absorbed while one aimed at the caller still ends the caller
        await asyncio.gather(task, return_exceptions=True)


def create_app(dsn: str | None = None) -> FastAPI:
    # checked here and not in Settings, so db.migrate and the ingest start on a configuration only the
    # API cannot serve
    problems = hot_window_configuration_problems(settings)
    if problems:
        raise RuntimeError("refusing to build the app:\n- " + "\n- ".join(problems))
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

    checks = _SharedCheck()
    # on app.state so the lifespan can end a check still in flight at shutdown
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
            # shielded, so a probe that gives up does not cancel the check other probes share
            await asyncio.wait_for(
                asyncio.shield(checks.start(app.state.dsn)), timeout=HEALTH_TIMEOUT_SECONDS
            )
        except Exception as exc:
            # covers both a connection/query failure and asyncio.TimeoutError from wait_for itself
            raise ApiError(500, "internal", INTERNAL_MESSAGE, None) from exc
        return {"status": "ok", "version": build_version()}

    return app


app = create_app()
