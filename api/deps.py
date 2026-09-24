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

# read in the positions libpq itself accepts one: after the userinfo of a URI, and as a keyword
# value anywhere in a connection string or a URI query, with or without spaces and quotes
_URI_PASSWORD = re.compile(r"://[^/?#@\s]*:([^@/?#\s]+)@")
_KEYWORD_PASSWORD = re.compile(r"(?:^|[\s?&])(?:ssl)?password\s*=\s*('(?:[^'\\]|\\.)*'|[^\s&]+)")
_TOKEN_EDGE = re.compile(r"[0-9A-Za-z_]")
# below this length a password cannot be told from a port number or an address octet
_STANDALONE_UNDER = 4


def _secrets_in(dsn: str) -> list[str]:
    secrets = set()
    try:
        parsed = conninfo_to_dict(dsn)
    except Exception:
        # libpq refuses some strings outright and quotes the whole string back rather than one
        # field, so the patterns below read the raw text as well
        parsed = {}
    for key, value in parsed.items():
        if key.endswith("password") and isinstance(value, str):
            secrets.add(value)
    for match in _URI_PASSWORD.finditer(dsn):
        # both forms: libpq connects with the decoded password and quotes the undecoded one
        secrets.update({match.group(1), unquote(match.group(1))})
    for match in _KEYWORD_PASSWORD.finditer(dsn):
        value = match.group(1)
        if len(value) > 1 and value.startswith("'") and value.endswith("'"):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        secrets.add(value)
    # longest first, so a password that contains another of these values is masked whole
    return sorted({secret for secret in secrets if secret}, key=len, reverse=True)


def _mask(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if len(secret) < _STANDALONE_UNDER:
            # a password this short occurs inside the addresses and identifiers a diagnostic is
            # made of, so it is masked only where it stands on its own
            before, after = r"(?<![^\s\"'=])", r"(?![^\s\"',)])"
        else:
            # bounded to whole tokens, so a dictionary-word password does not blank out the words
            # of the diagnostic around it
            before = r"(?<![0-9A-Za-z_])" if _TOKEN_EDGE.match(secret[0]) else ""
            after = r"(?![0-9A-Za-z_])" if _TOKEN_EDGE.match(secret[-1]) else ""
        text = re.sub(before + re.escape(secret) + after, "***", text)
    return text


def mask_secrets(text, dsn) -> str:
    # masks rather than raises on anything it is handed: this runs on a connection's failure path,
    # where a second exception would replace the cause an operator is reading
    try:
        return _mask(str(text), _secrets_in(str(dsn)))
    except Exception:
        return "***"


# module-level rather than inlined, so a test can monkeypatch either down to something fast:
# how long a request waits for a pool slot before PoolTimeout, and how long a statement may run
# on a pooled connection before Postgres cancels it, so one slow client or one stuck query cannot
# starve every other request of the pool
POOL_CHECKOUT_TIMEOUT_SECONDS = 5.0
STATEMENT_TIMEOUT_SECONDS = 5.0
# how long a pooled connection has to answer the empty query it is checked with at checkout: the
# checkout wait above ends when a connection is handed over, so a connection whose peer has gone
# silent would otherwise hold its request for as long as TCP takes to notice
POOL_CHECK_TIMEOUT_SECONDS = 1.0


def _statement_timeout_ms() -> int:
    milliseconds = round(STATEMENT_TIMEOUT_SECONDS * 1000)
    # Postgres reads 0 as no timeout at all and refuses a negative one on every connection. Raising
    # here moves the symptom rather than removing it -- the pool then hands out no connection at all
    # and the line in _pin_utc reports why
    if milliseconds < 1:
        raise ValueError(
            f"STATEMENT_TIMEOUT_SECONDS={STATEMENT_TIMEOUT_SECONDS!r} is under one millisecond"
        )
    return milliseconds


def _pin_utc(conn, dsn: str):
    try:
        # the pool makes its own connections and never calls db.session.connect, so the zone that file pins per connection has to be pinned again here
        conn.execute("SET TIME ZONE 'UTC'")
        # read by name rather than captured as a default argument, so a monkeypatch of the module
        # attribute reaches every connection this callback configures from here on
        conn.execute(f"SET statement_timeout = {_statement_timeout_ms()}")
        conn.commit()
    except Exception as exc:
        # a connection the pool cannot configure is discarded, and the pool retries with a widening
        # gap until its reconnect_timeout gives up, while /health runs off the pool and keeps
        # answering 200: this line is where an operator reads why every request is failing
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
    # the refusal ConnectionPool.check_connection makes too: it cannot switch autocommit on for a
    # connection that is closed or inside a transaction
    if conn.closed or pgconn.transaction_status != pq.TransactionStatus.IDLE:
        raise psycopg.OperationalError("the connection is not idle and cannot be checked")
    deadline = time.monotonic() + POOL_CHECK_TIMEOUT_SECONDS
    try:
        # sent and read at the libpq level: psycopg's own execute waits on the socket with no
        # deadline, and an empty simple query opens no transaction, so autocommit is left as found
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
        # read to the end before judging, so an error answer leaves no result unread behind it
        while (result := pgconn.get_result()) is not None:
            answers.append((pq.ExecStatus(result.status).name, result.get_error_message()))
        if [status for status, _ in answers] != ["EMPTY_QUERY"]:
            raise psycopg.OperationalError(f"the connection answered its check with {answers!r}")
    except Exception:
        # closed here: a connection that answered with an error is idle again, and psycopg_pool
        # hands an idle connection straight back out, where it discards a closed one and replaces it
        conn.close()
        raise


def _configure_for(dsn: str):
    def configure(conn) -> None:
        # the dsn this pool was built with rather than the configured one, so an app given an
        # explicit dsn masks the password its own connections carry
        _pin_utc(conn, dsn)

    return configure


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
        kwargs={
            # paginate indexes a row by the cursor's field names, so a pooled connection yielding
            # psycopg's default tuples 500s on the first page that has a successor -- and never
            # before, since paginate returns early whenever the rows fit inside the limit.
            # declared here rather than inside the configure callback so it survives a later
            # feature replacing that callback
            "row_factory": dict_row,
            # psycopg's default is 130 s, and a pool worker stuck that long connecting to a host
            # that drops packets cannot replace a discarded connection once the host is back
            "connect_timeout": 5,
            # an idle pooled connection whose peer vanished (a failover, an expired NAT or load
            # balancer entry) is otherwise noticed only by the kernel's two-hour keepalive default
            "keepalives": 1,
            # probing after 10 s idle also keeps an idle connection's NAT entry from expiring
            "keepalives_idle": 10,
            # three unanswered probes 5 s apart: a vanished peer is dropped 25 s into its silence
            "keepalives_interval": 5,
            "keepalives_count": 3,
            # Linux only (libpq ignores it elsewhere): a statement or probe left unacknowledged
            # 25 s drops the connection, where retransmission alone takes about fifteen minutes
            "tcp_user_timeout": 25000,
        },
        configure=_configure_for(dsn),
        check=_check_within_deadline,
        # psycopg_pool's own default is 30.0 s, long enough for a burst of slow requests to hold
        # every later checkout past the patience of whatever client is waiting on it
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
