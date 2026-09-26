import logging
import os
import selectors
import socket
import struct
import threading
import time

import psycopg
import psycopg.errors
import pytest
from fastapi.testclient import TestClient
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import api.deps as deps
import api.main
import config
from api.main import create_app
from api.pagination import BARS_CURSOR, decode_cursor, encode_cursor
from db.session import connect

_READY_FOR_QUERY = b"Z\x00\x00\x00\x05I"
_SELECT_ONE = b"Q" + struct.pack("!i", 4 + len(b"SELECT 1\x00")) + b"SELECT 1\x00"
_REFUSAL_BODY = b"SERROR\x00C57P03\x00Mthe relay refuses this query\x00\x00"
# an ErrorResponse and the ReadyForQuery after it: a server that is up and refuses the query
_REFUSAL = b"E" + struct.pack("!i", 4 + len(_REFUSAL_BODY)) + _REFUSAL_BODY + _READY_FOR_QUERY


def _database(dsn: str) -> str:
    return dsn.rsplit("/", 1)[1]


class _Link:
    def __init__(self, client, server, mode):
        self.client = client
        self.server = server
        # forward, silent, freeze_after_ready or refuse_after_ready
        self.mode = mode
        self.ready = False
        self.sent = bytearray()
        self.client_closed = threading.Event()

    def close(self):
        for sock in (self.client, self.server):
            try:
                sock.close()
            except OSError:
                pass


class _Relay:
    """A loopback TCP relay in front of the test database. A link can stop relaying with both of
    its sockets left open -- a peer that is still connected and no longer answers, which no
    database fixture can be made into -- and every link records what its client sent."""

    def __init__(self, dsn, new_links="forward"):
        params = conninfo_to_dict(dsn)
        self._upstream = (params["host"], int(params["port"]))
        self._new_links = new_links
        self._listener = socket.create_server(("127.0.0.1", 0))
        # a timeout rather than a blocking accept, which closing the listener does not wake on Linux
        self._listener.settimeout(0.05)
        params.update(
            host="127.0.0.1",
            port=str(self._listener.getsockname()[1]),
            # plaintext, so what the client sends can be read off the wire
            sslmode="disable",
            gssencmode="disable",
        )
        self.dsn = make_conninfo(**params)
        self.links = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._threads = [threading.Thread(target=self._accept, daemon=True)]
        self._threads[0].start()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def set_open_links(self, mode):
        with self._lock:
            for link in self.links:
                link.mode = mode

    def close(self):
        self._stopped.set()
        for thread in self._threads:
            thread.join(5)
        self._listener.close()
        for link in self.links:
            link.close()

    def _accept(self):
        while not self._stopped.is_set():
            try:
                client, _ = self._listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            client.settimeout(None)
            server = socket.create_connection(self._upstream)
            link = _Link(client, server, self._new_links)
            with self._lock:
                self.links.append(link)
            thread = threading.Thread(target=self._pump, args=(link,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _pump(self, link):
        with selectors.DefaultSelector() as selector:
            selector.register(link.client, selectors.EVENT_READ)
            selector.register(link.server, selectors.EVENT_READ)
            server_watched = True
            try:
                while not self._stopped.is_set():
                    if link.mode == "silent" and server_watched:
                        selector.unregister(link.server)
                        server_watched = False
                    for key, _ in selector.select(0.05):
                        if key.fileobj is link.server:
                            data = link.server.recv(65536)
                            if not data:
                                return
                            link.client.sendall(data)
                            link.ready = link.ready or _READY_FOR_QUERY in data
                            continue
                        data = link.client.recv(65536)
                        if not data:
                            link.client_closed.set()
                            return
                        link.sent += data
                        if link.ready and link.mode == "freeze_after_ready":
                            link.mode = "silent"
                        if link.mode == "silent":
                            continue
                        if link.ready and link.mode == "refuse_after_ready":
                            if data[:1] in (b"Q", b"P"):
                                link.client.sendall(_REFUSAL)
                            continue
                        link.server.sendall(data)
            except OSError:
                return


def _get_or_give_up(client, path, give_up_after, release):
    """(response, seconds), or (None, None) when the request is still waiting after give_up_after
    seconds -- in which case release() is called so the stuck request can end before the test does."""
    outcome = {}

    def get():
        start = time.monotonic()
        outcome["response"] = client.get(path)
        outcome["elapsed"] = time.monotonic() - start

    thread = threading.Thread(target=get)
    thread.start()
    thread.join(give_up_after)
    if thread.is_alive():
        release()
        thread.join(30)
        return None, None
    return outcome.get("response"), outcome.get("elapsed")


def test_health_answers_two_hundred_against_a_migrated_database(migrated_dsn):
    with TestClient(create_app(migrated_dsn)) as client:
        response = client.get("/health")
        # a second check on the same event loop: a socket watcher left behind by the first sits on
        # the descriptor number the second connection is likely to be given, and swallows its own
        again = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert again.status_code == 200


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
    # ("5s") is a presentation detail this assertion does not need to reproduce. A literal rather
    # than a value computed from the constant, which would move with it
    app = create_app(migrated_dsn)
    with TestClient(app):
        with app.state.pool.connection() as conn:
            assert conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
            ).fetchone() == {"setting": "5000"}


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
        # its own generous timeout: the 0.3 s above is what the request under test is held to, and
        # a loaded machine can take longer than that just to open the pool's first connection
        with app.state.pool.connection(timeout=10) as conn:
            conn.execute("SELECT 1")
            holder_ready.set()
            release_holder.wait(10)

    holder = threading.Thread(target=hold_the_only_connection)
    # raise_server_exceptions=False: the default client re-raises PoolTimeout out of the call
    # instead of returning the 500 response the installed handler already built for it
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.pool.wait(timeout=10)
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
            # dead database
            assert client.get("/health").status_code == 200
        finally:
            release_holder.set()
            holder.join(10)


def test_a_pooled_connection_that_stops_answering_is_replaced_within_the_check_deadline(
    migrated_dsn, monkeypatch
):
    monkeypatch.setattr(config.settings, "DB_POOL_MIN", 1)
    monkeypatch.setattr(config.settings, "DB_POOL_MAX", 1)
    with _Relay(migrated_dsn) as relay:
        app = create_app(relay.dsn)
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.pool.wait(timeout=10)
            assert client.get("/symbols").status_code == 200
            # the pool's only connection goes silent with its socket open: a failover, a dropped
            # NAT entry or a hung peer. The checkout wait has already ended by the time it is checked
            relay.set_open_links("silent")
            response, elapsed = _get_or_give_up(client, "/symbols", 10, relay.close)
            assert response is not None, "a request was held by a silent pooled connection"
            assert response.status_code == 200
            # the check's own deadline and a replacement's connect, well inside the checkout wait
            assert elapsed < deps.POOL_CHECK_TIMEOUT_SECONDS + 1.0
            # closed from this side rather than abandoned with its socket open
            assert relay.links[0].client_closed.wait(1)
            assert len(relay.links) == 2


def test_a_pooled_connection_that_refuses_its_check_is_replaced(migrated_dsn, monkeypatch):
    monkeypatch.setattr(config.settings, "DB_POOL_MIN", 1)
    monkeypatch.setattr(config.settings, "DB_POOL_MAX", 1)
    with _Relay(migrated_dsn) as relay:
        app = create_app(relay.dsn)
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.pool.wait(timeout=10)
            # a connection that still answers, with an error: it would refuse the request's own
            # statement just the same
            relay.set_open_links("refuse_after_ready")
            response, _ = _get_or_give_up(client, "/symbols", 10, relay.close)
            assert response is not None
            assert response.status_code == 200
            assert relay.links[0].client_closed.wait(1)
            # and the refusal's own words, on a connection checked outside the pool: inside it the
            # pool catches this and replaces the connection, so nothing ever reads the message that
            # tells an operator WHAT the connection answered
            outside_the_pool = psycopg.connect(relay.dsn, autocommit=True)
            try:
                relay.set_open_links("refuse_after_ready")
                with pytest.raises(
                    psycopg.OperationalError, match="the connection answered its check with"
                ):
                    deps._check_within_deadline(outside_the_pool)
            finally:
                outside_the_pool.close()


def test_a_pooled_connection_the_server_closed_is_replaced(migrated_dsn, monkeypatch):
    monkeypatch.setattr(config.settings, "DB_POOL_MIN", 1)
    monkeypatch.setattr(config.settings, "DB_POOL_MAX", 1)
    app = create_app(migrated_dsn)
    with TestClient(app, raise_server_exceptions=False) as client:
        with app.state.pool.connection() as conn:
            pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        with connect(migrated_dsn) as admin:
            admin.autocommit = True
            admin.execute("SELECT pg_terminate_backend(%s)", [pid])
            deadline = time.monotonic() + 10
            while admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", [pid]
            ).fetchone()[0]:
                assert time.monotonic() < deadline
                time.sleep(0.05)
        response = client.get("/symbols")
    assert response.status_code == 200


def test_the_check_leaves_a_connection_idle_and_refuses_one_inside_a_transaction(migrated_dsn):
    app = create_app(migrated_dsn)
    with TestClient(app):
        with app.state.pool.connection() as conn:
            # checked on the way out of the pool, and still outside any transaction, in the
            # autocommit mode it arrived in
            assert conn.pgconn.transaction_status == pq.TransactionStatus.IDLE
            assert conn.autocommit is False
            conn.execute("SELECT 1")
            with pytest.raises(psycopg.OperationalError, match="not idle"):
                deps._check_within_deadline(conn)


def test_a_pooled_connection_carries_the_configured_keepalives(migrated_dsn):
    app = create_app(migrated_dsn)
    with TestClient(app):
        with app.state.pool.connection() as conn:
            probe = socket.socket(fileno=os.dup(conn.pgconn.socket))
            try:
                assert probe.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
                # TCP_KEEPIDLE on Linux, TCP_KEEPALIVE on macOS: the same idle time under two names
                idle = getattr(socket, "TCP_KEEPIDLE", None) or socket.TCP_KEEPALIVE
                assert probe.getsockopt(socket.IPPROTO_TCP, idle) == 10
                assert probe.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 5
                assert probe.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
                if hasattr(socket, "TCP_USER_TIMEOUT"):
                    assert probe.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 25000
            finally:
                probe.close()


def test_health_sends_its_query_to_the_database_and_closes_the_connection(migrated_dsn):
    with _Relay(migrated_dsn) as relay:
        app = create_app(migrated_dsn)
        # /health alone through the relay; the pool keeps its direct connection
        app.state.dsn = relay.dsn
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
        [link] = relay.links
        assert link.client_closed.wait(1)
        # a database that accepts a connection and would refuse every query reads as healthy to a
        # probe that only connects
        assert _SELECT_ONE in link.sent


def test_health_answers_internal_when_the_database_refuses_the_query(migrated_dsn, caplog):
    caplog.set_level(logging.DEBUG, logger="api.main")
    with _Relay(migrated_dsn, new_links="refuse_after_ready") as relay:
        app = create_app(migrated_dsn)
        app.state.dsn = relay.dsn
        with TestClient(app) as client:
            assert client.get("/health").status_code == 500
    # the probe's own diagnostic, which is the only record of what the server answered: the 500 the
    # client gets carries the one internal message and nothing about the cause
    [record] = [record for record in caplog.records if record.name == "api.main"]
    assert record.getMessage().startswith(
        "database check failed: OperationalError: SELECT 1 answered "
    )
    assert "the relay refuses this query" in record.getMessage()


def test_health_answers_at_its_deadline_when_the_database_freezes_after_connecting(
    migrated_dsn, caplog
):
    caplog.set_level(logging.DEBUG, logger="api.main")
    with _Relay(migrated_dsn, new_links="freeze_after_ready") as relay:
        app = create_app(migrated_dsn)
        app.state.dsn = relay.dsn
        with TestClient(app, raise_server_exceptions=False) as client:
            response, elapsed = _get_or_give_up(client, "/health", 10, relay.close)
            assert response is not None, "/health waited on a frozen server past its own deadline"
            assert response.status_code == 500
            assert elapsed < api.main.HEALTH_TIMEOUT_SECONDS + 0.5
            [link] = relay.links
            # the socket closed at the deadline, and no second connection opened to cancel a query
            # the server never received
            assert link.client_closed.wait(1)
            assert len(relay.links) == 1
    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "api.main"
    ] == [
        (
            logging.WARNING,
            "database check failed: TimeoutError: the database did not answer within 2.0 s",
        )
    ]


def test_a_health_check_that_times_out_during_startup_closes_its_socket(
    migrated_dsn, monkeypatch, caplog
):
    monkeypatch.setattr(api.main, "HEALTH_TIMEOUT_SECONDS", 0.5)
    caplog.set_level(logging.DEBUG, logger="api.main")
    # accepts the connection and never answers the startup packet, so the check is cancelled inside
    # psycopg's connect, where no code of this app holds the socket to close it
    with _Relay(migrated_dsn, new_links="silent") as relay:
        app = create_app(migrated_dsn)
        app.state.dsn = relay.dsn
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/health").status_code == 500
            [link] = relay.links
            # released with the probe rather than whenever the garbage collector next runs: a
            # socket held open per probe is a server backend held open per probe
            assert link.client_closed.wait(1)
    assert [record.getMessage() for record in caplog.records if record.name == "api.main"] == [
        "database check failed: TimeoutError: the database did not answer within 0.5 s"
    ]


def test_a_failed_health_check_logs_its_cause_without_the_password(migrated_dsn, caplog):
    params = conninfo_to_dict(migrated_dsn)
    params["password"] = "not-the-password-4f1c"
    wrong = make_conninfo(**params)
    caplog.set_level(logging.DEBUG, logger="api.main")
    app = create_app(migrated_dsn)
    app.state.dsn = wrong
    with TestClient(app) as client:
        assert client.get("/health").status_code == 500
    [record] = [record for record in caplog.records if record.name == "api.main"]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert message.startswith("database check failed: OperationalError: ")
    assert "password authentication failed" in message
    assert "not-the-password-4f1c" not in message
    assert wrong not in message

    # the failure above is the one easiest to drive against a real server and the one failure whose
    # message carries no password: libpq answers a wrong password with `FATAL: password
    # authentication failed for user "..."`, so every assertion above holds whether or not the
    # masking ran. The two below are real, unmocked libpq failures that DO quote the secret back --
    # a connection string libpq cannot parse is echoed whole, password included -- so they are what
    # makes this test fail when the masking is deleted, against a real libpq rather than a patched
    # connect with hand-written exception text
    for dsn, secret, quoted in (
        (
            " postgresql://appuser:LEAD-SPACE-PW-9a2b@127.0.0.1:1/marketdata",
            "LEAD-SPACE-PW-9a2b",
            'missing "=" after',
        ),
        (
            "postgresql://appuser:PCT-PW-7c1d%zz@127.0.0.1:1/marketdata",
            "PCT-PW-7c1d%zz",
            "invalid percent-encoded token",
        ),
    ):
        caplog.clear()
        unparseable = create_app(migrated_dsn)
        unparseable.state.dsn = dsn
        with TestClient(unparseable) as client:
            assert client.get("/health").status_code == 500, dsn
        [record] = [record for record in caplog.records if record.name == "api.main"]
        message = record.getMessage()
        # the cause still reaches the operator, so the masking is not blanking the diagnostic
        assert quoted in message, message
        assert "***" in message, message
        assert secret not in message, message
