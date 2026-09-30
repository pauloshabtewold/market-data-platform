import asyncio
import logging
import selectors
import threading
import time

import anyio.to_thread
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

import api.deps
import api.main
from api.main import create_app

DEAD_DSN = "postgresql://nobody:nobody@127.0.0.1:1/none"


class _IdlePool:
    # /health never reaches for the pool, so the lifespan's open and close are all this has to take
    def open(self, wait=False):
        pass

    def close(self):
        pass


class _Arrivals:
    """Counts /health requests as they reach the app, before any route code runs."""

    def __init__(self, app):
        self.app = app
        self.count = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/health":
            self.count += 1
        await self.app(scope, receive, send)


def _get_in_threads(client, path, count, give_up_after):
    responses = [None] * count

    def get(index):
        responses[index] = client.get(path)

    threads = [threading.Thread(target=get, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(give_up_after)
    return responses


def test_health_answers_while_every_worker_thread_is_held(monkeypatch):
    # a check that needs nothing: what is under test is the route's own path to the event loop
    async def _answers(dsn):
        return None

    monkeypatch.setattr(api.main, "_check_database", _answers)
    app = create_app(dsn=DEAD_DSN)
    app.state.pool = _IdlePool()
    release = threading.Event()
    with TestClient(app) as client:
        # the limiter every sync def route and every sync dependency waits on, narrowed to one token
        # and that token held: a /health that hops onto a worker thread anywhere waits behind it
        limiter = client.portal.call(anyio.to_thread.current_default_thread_limiter)
        client.portal.call(setattr, limiter, "total_tokens", 1)
        held = client.portal.start_task_soon(anyio.to_thread.run_sync, release.wait)
        try:
            deadline = time.monotonic() + 5
            while client.portal.call(lambda: limiter.borrowed_tokens) != 1:
                assert time.monotonic() < deadline, "the worker thread was never taken"
                time.sleep(0.01)
            start = time.monotonic()
            [response] = _get_in_threads(client, "/health", 1, give_up_after=3)
            elapsed = time.monotonic() - start
            assert response is not None, "/health waited for a worker thread"
            assert response.status_code == 200
            assert elapsed < 1.0
        finally:
            release.set()
            held.result(5)


def test_concurrent_probes_share_one_connection_attempt_and_log_its_failure_once(
    monkeypatch, caplog
):
    probes = 8
    attempts = []
    arrivals = None

    # the connect itself replaced, so the count is of connections the probes would open against a
    # real server, however the check above it is arranged
    async def _connect(dsn, **kwargs):
        attempts.append(dsn)
        while arrivals.count < probes:
            await asyncio.sleep(0.01)
        # long enough for the last arrival to reach the route and join the check in flight
        await asyncio.sleep(0.2)
        raise psycopg.OperationalError("the probe database is unreachable")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _connect)
    # well past the barrier above, so no probe gives up before every one has arrived
    monkeypatch.setattr(api.main, "HEALTH_TIMEOUT_SECONDS", 5.0)
    app = create_app(dsn=DEAD_DSN)
    app.state.pool = _IdlePool()
    arrivals = _Arrivals(app)
    # every level captured, so a failure logged at the wrong level is read rather than filtered out
    caplog.set_level(logging.DEBUG, logger="api.main")
    with TestClient(arrivals) as client:
        responses = _get_in_threads(client, "/health", probes, give_up_after=10)
    assert [response.status_code for response in responses] == [500] * probes
    # one backend for the whole burst: with a connection per probe, a burst of probes takes the
    # server's last connection slots from the pool and from an operator
    assert attempts == [DEAD_DSN]
    failures = [record for record in caplog.records if record.name == "api.main"]
    # the cause, once for the one check and at a level the default configuration keeps
    assert [(record.levelno, record.getMessage()) for record in failures] == [
        (
            logging.WARNING,
            "database check failed: OperationalError: the probe database is unreachable",
        )
    ]


def test_a_probe_after_a_finished_check_runs_a_check_of_its_own(monkeypatch):
    attempts = []

    async def _connect(dsn, **kwargs):
        attempts.append(dsn)
        raise psycopg.OperationalError("the probe database is unreachable")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _connect)
    app = create_app(dsn=DEAD_DSN)
    app.state.pool = _IdlePool()
    # one client and so one event loop: a finished check must not answer for the probes after it
    with TestClient(app) as client:
        statuses = [client.get("/health").status_code for _ in range(3)]
    assert statuses == [500, 500, 500]
    assert attempts == [DEAD_DSN] * 3


def test_a_probe_that_gives_up_leaves_the_shared_check_to_the_probes_still_waiting(monkeypatch):
    attempts = []

    async def _hangs(dsn, **kwargs):
        attempts.append(dsn)
        await asyncio.sleep(10)

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _hangs)
    monkeypatch.setattr(api.main, "HEALTH_TIMEOUT_SECONDS", 0.6)
    app = create_app(dsn=DEAD_DSN)
    app.state.pool = _IdlePool()
    responses = {}
    # the second probe joins the first one's check and is still waiting on it when the first one's
    # own deadline passes: that deadline must end the first probe, not the check the second shares
    with TestClient(app, raise_server_exceptions=False) as client:
        first = threading.Thread(target=lambda: responses.update(first=client.get("/health")))
        first.start()
        time.sleep(0.3)
        responses["second"] = client.get("/health")
        first.join(10)
    internal = {
        "error": {"code": "internal", "message": "the request could not be completed", "detail": None}
    }
    assert [(responses[name].status_code, responses[name].json()) for name in ("first", "second")] == [
        (500, internal),
        (500, internal),
    ]
    assert attempts == [DEAD_DSN]


def test_a_failed_check_logs_its_cause_with_the_configured_password_masked(monkeypatch, caplog):
    # libpq refuses a bare % in a URI password and quotes the token it refused, so the password
    # arrives inside the exception rather than through any formatting of the dsn
    dsn = "postgresql://appuser:pA55w%rd@127.0.0.1:1/marketdata"

    async def _refuses(connection_string, **kwargs):
        raise psycopg.ProgrammingError('invalid percent-encoded token: "pA55w%rd"')

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _refuses)
    app = create_app(dsn=dsn)
    app.state.pool = _IdlePool()
    caplog.set_level(logging.DEBUG, logger="api.main")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 500
    logged = [record.getMessage() for record in caplog.records if record.name == "api.main"]
    assert logged == ['database check failed: ProgrammingError: invalid percent-encoded token: "***"']


def test_a_failed_check_masks_a_keyword_connection_strings_password(monkeypatch, caplog):
    dsn = "host=127.0.0.1 port=1 dbname=marketdata user=appuser password=s3cr3t-pw"

    async def _refuses(connection_string, **kwargs):
        raise psycopg.OperationalError('connection failed: "s3cr3t-pw" was rejected')

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _refuses)
    app = create_app(dsn=dsn)
    app.state.pool = _IdlePool()
    caplog.set_level(logging.DEBUG, logger="api.main")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 500
    logged = [record.getMessage() for record in caplog.records if record.name == "api.main"]
    assert logged == ['database check failed: OperationalError: connection failed: "***" was rejected']


def test_shutdown_does_not_wait_for_a_check_still_in_flight(monkeypatch):
    started = threading.Event()

    async def _hangs(connection_string, **kwargs):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _hangs)
    # long enough that a shutdown waiting for the check's own deadline is unmistakable
    monkeypatch.setattr(api.main, "HEALTH_TIMEOUT_SECONDS", 20.0)
    app = create_app(dsn=DEAD_DSN)
    app.state.pool = _IdlePool()
    client = TestClient(app)
    client.__enter__()

    def probe_until_shutdown():
        # shutdown cancels the check this probe is waiting on, which reaches the caller as an
        # exception rather than a response: what is under test is how long the shutdown takes
        try:
            client.get("/health")
        except BaseException:
            pass

    probe = threading.Thread(target=probe_until_shutdown)
    probe.start()
    assert started.wait(5), "the check never started"
    start = time.monotonic()
    client.__exit__(None, None, None)
    elapsed = time.monotonic() - start
    probe.join(5)
    assert elapsed < 2.0, f"shutdown waited {elapsed:.2f} s for the check in flight"


def test_a_connection_the_pool_cannot_configure_reports_its_cause(monkeypatch, caplog):
    class _RefusesTheSetting:
        def execute(self, statement):
            if "statement_timeout" in statement:
                raise psycopg.errors.InsufficientPrivilege("permission denied to set parameter")

        def commit(self):
            pass

    caplog.set_level(logging.DEBUG, logger="api.deps")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.deps._pin_utc(_RefusesTheSetting(), DEAD_DSN)
    logged = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "api.deps"
    ]
    assert logged == [
        (
            logging.ERROR,
            "pooled connection could not be configured: InsufficientPrivilege: "
            "permission denied to set parameter",
        )
    ]


def test_a_configuration_failure_masks_the_configured_password(monkeypatch, caplog):
    monkeypatch.setattr(
        api.deps.settings, "DATABASE_URL", "postgresql://appuser:s3cr3t-pw@127.0.0.1:1/marketdata"
    )

    class _RefusesEverything:
        def execute(self, statement):
            raise psycopg.OperationalError('the server rejected "s3cr3t-pw"')

        def commit(self):
            pass

    caplog.set_level(logging.DEBUG, logger="api.deps")
    with pytest.raises(psycopg.OperationalError):
        api.deps._pin_utc(_RefusesEverything(), api.deps.settings.DATABASE_URL)
    logged = [record.getMessage() for record in caplog.records if record.name == "api.deps"]
    assert logged == [
        'pooled connection could not be configured: OperationalError: the server rejected "***"'
    ]


def test_a_password_carried_as_a_uri_query_parameter_is_masked(monkeypatch, caplog):
    # libpq reads password as a query parameter too, and refuses this one for the same reason it
    # refuses the bare % in a userinfo password: the position of the password is what moves
    dsn = "postgresql://appuser@127.0.0.1:1/marketdata?password=pA55w%rd"

    async def _refuses(connection_string, **kwargs):
        raise psycopg.ProgrammingError('invalid percent-encoded token: "pA55w%rd"')

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _refuses)
    app = create_app(dsn=dsn)
    app.state.pool = _IdlePool()
    caplog.set_level(logging.DEBUG, logger="api.main")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 500
    logged = [record.getMessage() for record in caplog.records if record.name == "api.main"]
    assert logged == ['database check failed: ProgrammingError: invalid percent-encoded token: "***"']


def test_a_keyword_password_is_masked_through_the_spacing_and_quoting_libpq_accepts():
    # every one of these is a connection string libpq parses, so each is a password that can reach
    # an exception message
    for dsn, secret in (
        ("host=h dbname=d password=bare-pw", "bare-pw"),
        ("host=h dbname=d password = spaced-pw", "spaced-pw"),
        ("host=h dbname=d password='s3c r3t'", "s3c r3t"),
        (r"host=h dbname=d password='s3c\'r3t'", "s3c'r3t"),
        ("host=h dbname=d sslpassword=keypass1", "keypass1"),
    ):
        masked = api.deps.mask_secrets(f'the server rejected "{secret}"', dsn)
        assert masked == 'the server rejected "***"', dsn


def test_a_percent_encoded_password_is_masked_in_both_the_encoded_and_the_decoded_form():
    # libpq connects with the decoded password and quotes the undecoded string back in its own
    # refusals, so a message can carry either
    dsn = "postgresql://appuser:p%40ss@127.0.0.1:1/marketdata"
    assert api.deps.mask_secrets('rejected "p@ss"', dsn) == 'rejected "***"'
    assert api.deps.mask_secrets('rejected "p%40ss"', dsn) == 'rejected "***"'
    # in a query parameter the decoded form is the one libpq resolves, and only libpq's own parse
    # of the string reports it
    as_parameter = "postgresql://appuser@127.0.0.1:1/marketdata?password=q%40p"
    assert api.deps.mask_secrets('rejected "q@p"', as_parameter) == 'rejected "***"'
    # the same for the key it reads a private key file with, which only libpq's parse resolves
    key_file = "postgresql://appuser@127.0.0.1:1/marketdata?sslpassword=k%40y"
    assert api.deps.mask_secrets('rejected "k@y"', key_file) == 'rejected "***"'


def test_a_connection_string_libpq_refuses_wholesale_still_has_its_password_masked():
    # libpq quotes the whole string rather than one field when it cannot parse it at all
    dsn = "  postgresql://appuser:ws-pw@127.0.0.1:1/marketdata"
    message = f'missing "=" after "{dsn.strip()}" in connection info string'
    assert api.deps.mask_secrets(message, dsn) == (
        'missing "=" after "postgresql://appuser:***@127.0.0.1:1/marketdata" in connection info string'
    )


def test_a_short_password_does_not_blank_out_the_words_of_the_message_around_it():
    # what a connection diagnostic is made of is addresses, ports and identifiers, and a password
    # short enough to occur inside them is masked only where it stands on its own
    refusal = (
        'connection to server at "127.0.0.1", port 5432 failed: FATAL:  password authentication '
        'failed for user "appuser"'
    )
    for password in ("1", "0", ".", ":", "a", "pg"):
        dsn = f"postgresql://appuser:{password}@127.0.0.1:5432/marketdata"
        assert api.deps.mask_secrets(refusal, dsn) == refusal, password
        assert api.deps.mask_secrets(f'rejected "{password}"', dsn) == 'rejected "***"', password
    inside_a_word = "postgresql://appuser:market@127.0.0.1:1/marketdata"
    entry = 'FATAL: database "marketdata" does not exist'
    assert api.deps.mask_secrets(entry, inside_a_word) == entry


def test_a_password_that_contains_another_configured_secret_is_masked_whole():
    # the contained secret is NOT a prefix of the containing one, and that is the whole point: for a
    # prefix pair, descending alphabetical order and longest-first order are the same order, so a pair
    # like ("secretpw", "secretpw-and-more") is satisfied by either and pins neither. Here the short
    # secret sorts alphabetically AFTER the long one, so masking it first rewrites the text and the
    # long one no longer matches -- the operator would read aa-***-bb, which is the long password's
    # length and both of its outer fragments, where the rule exists to produce ***
    dsn = "host=h dbname=d password=zzzz sslpassword=aa-zzzz-bb"
    assert api.deps._secrets_in(dsn) == ["aa-zzzz-bb", "zzzz"]
    assert sorted(api.deps._secrets_in(dsn), reverse=True) == ["zzzz", "aa-zzzz-bb"]
    assert api.deps.mask_secrets('rejected "aa-zzzz-bb"', dsn) == 'rejected "***"'
    assert api.deps.mask_secrets('rejected "zzzz"', dsn) == 'rejected "***"'
    # and the prefix pair the same rule was written from, kept because it is the shape a DSN
    # carrying one password twice actually produces
    prefix_pair = "host=h dbname=d password=secretpw sslpassword=secretpw-and-more"
    masked = api.deps.mask_secrets('rejected "secretpw-and-more" and "secretpw"', prefix_pair)
    assert masked == 'rejected "***" and "***"'


def test_a_secret_is_masked_at_the_two_characters_the_guards_branch_on():
    # _mask reads the secret's FIRST and LAST character to decide whether to bound the match to a
    # whole token, and switches to a different pair of guards below _STANDALONE_UNDER. Every other
    # password in this file is a word -- five characters or more, alphanumeric at both ends -- so
    # the else-arm of each guard and the length boundary itself were taken by nothing, and nine
    # mutations of those three lines survived a whole generated campaign twice. Three of them stop
    # masking a real password, which is the direction that leaks.

    # exactly at the boundary: four characters still take the token branch, where the standalone
    # branch would not mask a secret followed by a hyphen
    at_the_boundary = "host=h dbname=d password=1234"
    assert api.deps.mask_secrets("connection to 1234-5 failed", at_the_boundary) == (
        "connection to ***-5 failed"
    )

    # an edge that is not a token character at all: there is no word boundary to anchor, so the
    # guard is empty and the escaped secret has to match on its own
    for dsn, refusal, masked in (
        ("host=h dbname=d password='!secret'", 'rejected "!secret"', 'rejected "***"'),
        ("host=h dbname=d password='secret!'", 'rejected "secret!"', 'rejected "***"'),
    ):
        assert api.deps.mask_secrets(refusal, dsn) == masked, dsn

    # and the other direction, which is what the token bounds exist for: a secret that occurs inside
    # a longer identifier is left alone, whatever the case of the characters around it. The existing
    # "marketdata" case cannot see this -- its continuation is lowercase, so a mutated character
    # class still excludes it; only a capital on one side and a lowercase on the other pins both.
    inside_a_word = "host=h dbname=d password=market"
    for entry in (
        'FATAL: cluster DBmarket is down',
        'FATAL: cluster xmarket is down',
        'FATAL: database "marketDATA" does not exist',
    ):
        assert api.deps.mask_secrets(entry, inside_a_word) == entry, entry

    # the same for WHICH character each guard reads: with a non-alphanumeric one position in from
    # either end, reading secret[1] instead of secret[0] -- or secret[-2] instead of secret[-1] --
    # picks the wrong guard and starts masking inside a word
    for dsn, entry in (
        ("host=h dbname=d password='a-bcdef'", "FATAL: role xa-bcdef is unknown"),
        ("host=h dbname=d password='a-bcdef'", "FATAL: role a-bcdefX is unknown"),
        ("host=h dbname=d password='abcde-f'", "FATAL: role abcde-fX is unknown"),
    ):
        assert api.deps.mask_secrets(entry, dsn) == entry, (dsn, entry)


def test_the_keyword_fallback_finds_a_password_in_a_string_libpq_itself_refuses():
    # _secrets_in reads the DSN twice: through libpq, and through its own two regexes. The regexes
    # exist ONLY for the strings libpq refuses -- and every test of their quoting and unescaping used
    # a string libpq ACCEPTS, where conninfo_to_dict supplies the password anyway and the regex path
    # cannot be observed at all. Four mutations of it survived two campaigns; on a refused string each
    # one drops the real password out of the set, and a password that is not in the set is not masked
    # out of the line an operator reads.
    #
    # The refusal is asserted rather than assumed: if a later libpq accepts one of these, this test
    # says the premise moved instead of quietly testing the path that was already covered.
    for dsn, password in (
        # a quoted value with an escaped quote inside it, in a string carrying an unknown keyword
        (r"password='s3c\'r3t' host=h dbname=d nosuchkeyword=1", "s3c'r3t"),
        # an unterminated quote, which only the regex can read
        ("host=h dbname=d password='", "'"),
    ):
        with pytest.raises(psycopg.ProgrammingError):
            conninfo_to_dict(dsn)
        assert password in api.deps._secrets_in(dsn), dsn
        assert api.deps.mask_secrets(f'the server rejected "{password}"', dsn) == (
            'the server rejected "***"'
        ), dsn

    # and an empty quoted password is no secret: it unwraps to "" and is dropped, so nothing in a
    # diagnostic is masked on account of it
    assert api.deps._secrets_in("host=h dbname=d password=''") == []


def test_masking_answers_rather_than_raises_on_anything_it_is_handed():
    # it runs on a connection's failure path, where a second exception would replace the cause
    assert api.deps.mask_secrets("nothing to mask", None) == "nothing to mask"
    assert api.deps.mask_secrets(None, "postgresql://u:p@h:1/d") == "None"
    assert api.deps.mask_secrets("nothing to mask", "") == "nothing to mask"

    class _RaisesWhenRead:
        def __str__(self):
            raise RuntimeError("this exception has no printable form")

    # masked rather than reported: what cannot be rendered cannot be shown to be secret-free
    assert api.deps.mask_secrets(_RaisesWhenRead(), "postgresql://u:pw@h:1/d") == "***"
    assert api.deps.mask_secrets("nothing to mask", _RaisesWhenRead()) == "***"


def test_the_pool_masks_against_the_dsn_it_was_built_with(monkeypatch, caplog):
    monkeypatch.setattr(
        api.deps.settings, "DATABASE_URL", "postgresql://appuser:OTHER-PW@127.0.0.1:1/marketdata"
    )
    dsn = "postgresql://appuser:POOL-PW@127.0.0.1:1/marketdata"

    class _RefusesEverything:
        def execute(self, statement):
            raise psycopg.OperationalError(f'connection failed; connection string was "{dsn}"')

        def commit(self):
            pass

    pool = api.deps.build_pool(dsn, min_size=0, max_size=1)
    caplog.set_level(logging.DEBUG, logger="api.deps")
    try:
        # the callback the pool itself would run on every connection it opens
        with pytest.raises(psycopg.OperationalError):
            pool._configure(_RefusesEverything())
    finally:
        pool.close()
    logged = [record.getMessage() for record in caplog.records if record.name == "api.deps"]
    assert logged == [
        "pooled connection could not be configured: OperationalError: connection failed; "
        'connection string was "postgresql://appuser:***@127.0.0.1:1/marketdata"'
    ]


def test_a_statement_timeout_under_a_millisecond_is_reported_by_the_line_that_reports_a_refusal(
    monkeypatch, caplog
):
    monkeypatch.setattr(api.deps, "STATEMENT_TIMEOUT_SECONDS", 0.0004)

    class _AcceptsTheZone:
        def execute(self, statement):
            pass

        def commit(self):
            pass

    caplog.set_level(logging.DEBUG, logger="api.deps")
    with pytest.raises(ValueError):
        api.deps._pin_utc(_AcceptsTheZone(), DEAD_DSN)
    logged = [record.getMessage() for record in caplog.records if record.name == "api.deps"]
    assert logged == [
        "pooled connection could not be configured: ValueError: "
        "STATEMENT_TIMEOUT_SECONDS=0.0004 is under one millisecond"
    ]


def test_the_checks_own_refusals_name_the_bound_and_the_answer_they_refused():
    # the two diagnostics a pooled connection's check raises, which nothing reads off the wire: the
    # pool catches them, discards the connection and replaces it, so the only place they are ever
    # read is an operator's log line -- and each was replaceable by None with the suite green
    with pytest.raises(
        psycopg.OperationalError,
        match=f"the connection did not answer its check within {api.deps.POOL_CHECK_TIMEOUT_SECONDS} s",
    ):
        # a selector with nothing registered and a deadline already past: the shape of a peer that
        # accepted the query and then stopped answering
        api.deps._wait_for_socket(selectors.DefaultSelector(), time.monotonic() - 1)


def test_shutdown_returns_only_once_the_check_it_cancelled_has_finished(monkeypatch):
    # the check holds a socket, and a task still unwinding when the loop is torn down leaves it open
    async def _hangs(connection_string, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _hangs)

    async def scenario():
        checks = api.main._SharedCheck()
        task = checks.start(DEAD_DSN)
        await asyncio.sleep(0)
        await checks.close()
        return task.done()

    assert asyncio.run(scenario()) is True


def test_shutdown_passes_on_a_cancellation_aimed_at_the_lifespan_itself(monkeypatch):
    async def _hangs(connection_string, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _hangs)

    async def scenario():
        checks = api.main._SharedCheck()
        checks.start(DEAD_DSN)
        await asyncio.sleep(0)
        reached_the_end = False

        async def shutting_down():
            nonlocal reached_the_end
            await checks.close()
            reached_the_end = True

        closing = asyncio.get_running_loop().create_task(shutting_down())
        await asyncio.sleep(0)
        closing.cancel()
        try:
            await closing
        except asyncio.CancelledError:
            return reached_the_end, "cancelled"
        return reached_the_end, "completed"

    assert asyncio.run(scenario()) == (False, "cancelled")


def test_the_pool_is_closed_even_when_ending_the_check_fails(monkeypatch):
    closed = []

    class _RecordingPool(_IdlePool):
        def close(self):
            closed.append(True)

    class _RefusesToClose:
        async def close(self):
            raise KeyboardInterrupt("the process is going down now")

    app = create_app(dsn=DEAD_DSN)
    app.state.pool = _RecordingPool()
    app.state.checks = _RefusesToClose()

    # the lifespan driven directly: a server's own task group would re-raise this inside a group,
    # which says nothing about the order the two closes ran in
    async def startup_then_shutdown():
        async with api.main._lifespan(app):
            pass

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(startup_then_shutdown())
    assert closed == [True]


def test_a_connection_string_libpq_refuses_has_every_form_of_password_masked():
    # a string libpq will not parse is the case the patterns exist for: nothing resolves the fields,
    # so each spelling of a password has to be read out of the raw text
    refused = "host=h dbname=d sslpassword='k p' trailing-garbage"
    assert api.deps.mask_secrets('the key password "k p" was rejected', refused) == (
        'the key password "***" was rejected'
    )
    spaced = "host=h dbname=d password = spaced-pw trailing-garbage"
    assert api.deps.mask_secrets('rejected "spaced-pw"', spaced) == 'rejected "***"'
    padded = "  postgresql://appuser:p%40ss@127.0.0.1:1/marketdata"
    assert api.deps.mask_secrets('rejected "p@ss"', padded) == 'rejected "***"'


def test_a_failed_check_masks_the_cause_where_the_check_reports_it(caplog):
    # the reporting line itself, with no app and so no handler filter in the way: masking at the
    # handler covers what other packages log, and this covers what this one logs
    dsn = "postgresql://appuser:s3cr3t-pw@127.0.0.1:1/marketdata"

    async def scenario():
        async def fails():
            raise psycopg.OperationalError(f'connection failed; connection string was "{dsn}"')

        task = asyncio.get_running_loop().create_task(fails())
        try:
            await task
        except psycopg.OperationalError:
            pass
        api.main._failed_check_logger(dsn)(task)

    caplog.set_level(logging.DEBUG, logger="api.main")
    asyncio.run(scenario())
    logged = [record.getMessage() for record in caplog.records if record.name == "api.main"]
    assert logged == [
        "database check failed: OperationalError: connection failed; connection string was "
        '"postgresql://appuser:***@127.0.0.1:1/marketdata"'
    ]


