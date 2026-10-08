import logging
import re
import selectors
import time
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import unquote

import psycopg
from fastapi import Request
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import settings

log = logging.getLogger(__name__)

# every position libpq takes one: after a URI's userinfo, and as a keyword value anywhere in a
# connection string or URI query, spaces and quotes either way
_URI_PASSWORD = re.compile(r"://[^/?#@\s]*:([^@/?#\s]+)@")
_KEYWORD_PASSWORD = re.compile(r"(?:^|[\s?&])(?:ssl)?password\s*=\s*('(?:[^'\\]|\\.)*'|[^\s&]+)")
_TOKEN_EDGE = re.compile(r"[0-9A-Za-z_]")
# shorter than this a password reads as a port number or an address octet
_STANDALONE_UNDER = 4


def _secrets_in(dsn: str) -> list[str]:
    secrets = set()
    try:
        parsed = conninfo_to_dict(dsn)
    except Exception:
        # libpq refuses some strings and quotes the whole one back, so the patterns read raw text
        parsed = {}
    for key, value in parsed.items():
        if key.endswith("password") and isinstance(value, str):
            secrets.add(value)
    for match in _URI_PASSWORD.finditer(dsn):
        # libpq connects with the decoded password and quotes the undecoded one
        secrets.update({match.group(1), unquote(match.group(1))})
    for match in _KEYWORD_PASSWORD.finditer(dsn):
        value = match.group(1)
        if len(value) > 1 and value.startswith("'") and value.endswith("'"):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        secrets.add(value)
    # longest first, so a password containing another value is masked whole
    return sorted({secret for secret in secrets if secret}, key=len, reverse=True)


def _mask(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if len(secret) < _STANDALONE_UNDER:
            # this short it occurs inside diagnostic addresses and identifiers: masked only alone
            before, after = r"(?<![^\s\"'=])", r"(?![^\s\"',)])"
        else:
            # whole tokens, so a dictionary-word password does not blank the diagnostic around it
            before = r"(?<![0-9A-Za-z_])" if _TOKEN_EDGE.match(secret[0]) else ""
            after = r"(?![0-9A-Za-z_])" if _TOKEN_EDGE.match(secret[-1]) else ""
        text = re.sub(before + re.escape(secret) + after, "***", text)
    return text


def mask_secrets(text, dsn) -> str:
    # never raises: on a connection's failure path a second exception replaces the real cause
    try:
        return _mask(str(text), _secrets_in(str(dsn)))
    except Exception:
        return "***"


# module-level so a test can monkeypatch either down: the wait for a pool slot, and how long a
# statement runs before Postgres cancels it
POOL_CHECKOUT_TIMEOUT_SECONDS = 5.0
STATEMENT_TIMEOUT_SECONDS = 5.0
# how long a checked-out connection has to answer its empty query: the wait above ends at handover,
# so a silent peer would hold its request as long as TCP takes to notice
POOL_CHECK_TIMEOUT_SECONDS = 1.0


def _statement_timeout_ms() -> int:
    milliseconds = round(STATEMENT_TIMEOUT_SECONDS * 1000)
    # Postgres reads 0 as no timeout and refuses a negative one. Raising here moves the symptom:
    # the pool hands out no connection and the line in _pin_utc reports why
    if milliseconds < 1:
        raise ValueError(
            f"STATEMENT_TIMEOUT_SECONDS={STATEMENT_TIMEOUT_SECONDS!r} is under one millisecond"
        )
    return milliseconds


def _pin_utc(conn, dsn: str):
    try:
        # the pool never calls db.session.connect, so the zone that file pins per connection is
        # pinned again here
        conn.execute("SET TIME ZONE 'UTC'")
        # read by name, not a default argument, so a monkeypatch of the attribute reaches every
        # connection configured after it
        conn.execute(f"SET statement_timeout = {_statement_timeout_ms()}")
        conn.commit()
    except Exception as exc:
        # the pool discards an unconfigurable connection, widening its retry gap until
        # reconnect_timeout gives up while /health, off the pool, answers 200: here is why all fail
        log.error(
            "pooled connection could not be configured: %s: %s",
            type(exc).__name__,
            mask_secrets(str(exc), dsn),
        )
        raise


def _wait_for_socket(selector: selectors.BaseSelector, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not selector.select(remaining):
        raise psycopg.OperationalError(
            f"the connection did not answer its check within {POOL_CHECK_TIMEOUT_SECONDS} s"
        )


def _check_within_deadline(conn: psycopg.Connection) -> None:
    pgconn = conn.pgconn
    # ConnectionPool.check_connection refuses the same: no autocommit on a closed or in-transaction
    # connection
    if conn.closed or pgconn.transaction_status != pq.TransactionStatus.IDLE:
        raise psycopg.OperationalError("the connection is not idle and cannot be checked")
    deadline = time.monotonic() + POOL_CHECK_TIMEOUT_SECONDS
    try:
        # libpq level: psycopg's execute waits on the socket with no deadline; an empty simple query
        # opens no transaction, leaving autocommit as found
        pgconn.send_query(b"")
        with selectors.DefaultSelector() as selector:
            selector.register(pgconn.socket, selectors.EVENT_WRITE)
            while pgconn.flush():
                _wait_for_socket(selector, deadline)
            selector.modify(pgconn.socket, selectors.EVENT_READ)
            pgconn.consume_input()
            while pgconn.is_busy():
                _wait_for_socket(selector, deadline)
                pgconn.consume_input()
        answers = []
        # drained first, so an error answer leaves no result unread behind it
        while (result := pgconn.get_result()) is not None:
            answers.append((pq.ExecStatus(result.status).name, result.get_error_message()))
        if [status for status, _ in answers] != ["EMPTY_QUERY"]:
            raise psycopg.OperationalError(f"the connection answered its check with {answers!r}")
    except Exception:
        # closed here: an errored connection is idle again and psycopg_pool hands idle ones back
        # out, where it discards and replaces a closed one
        conn.close()
        raise


def _configure_for(dsn: str):
    def configure(conn) -> None:
        # this pool's own dsn, not the configured one, so an explicit dsn masks its own password
        _pin_utc(conn, dsn)

    return configure


def build_pool(
    dsn: str, min_size: int | None = None, max_size: int | None = None
) -> ConnectionPool:
    # not a signature default: that is evaluated once at import, past any later settings monkeypatch
    min_size = settings.DB_POOL_MIN if min_size is None else min_size
    max_size = settings.DB_POOL_MAX if max_size is None else max_size
    return ConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={
            # paginate indexes rows by cursor field name, so psycopg's default tuples 500 on the
            # first page with a successor. Here, not in configure, so replacing that cannot drop it
            "row_factory": dict_row,
            # psycopg's default is 130 s, and a worker stuck that long on a host dropping packets
            # cannot replace a discarded connection once it is back
            "connect_timeout": 5,
            # a vanished peer (failover, expired NAT or load balancer entry) is otherwise noticed
            # only by the kernel's two-hour keepalive default
            "keepalives": 1,
            # probing at 10 s idle also keeps the NAT entry from expiring
            "keepalives_idle": 10,
            # three unanswered probes 5 s apart: a vanished peer drops 25 s into its silence
            "keepalives_interval": 5,
            "keepalives_count": 3,
            # Linux only (libpq ignores it elsewhere): 25 s unacknowledged drops the connection,
            # where retransmission alone takes about fifteen minutes
            "tcp_user_timeout": 25000,
        },
        configure=_configure_for(dsn),
        check=_check_within_deadline,
        # psycopg_pool's default is 30.0 s, long enough for a burst of slow requests to hold later
        # checkouts past any waiting client's patience
        timeout=POOL_CHECKOUT_TIMEOUT_SECONDS,
    )


# async so FastAPI resolves it on the event loop: a sync dependency waits for one of the worker
# threads every sync route shares, and every route's parameter validation waits behind it
async def get_pool(request: Request) -> ConnectionPool:
    return request.app.state.pool


def build_version() -> str:
    try:
        return version("market-data-platform")
    except PackageNotFoundError:
        return "unknown"
