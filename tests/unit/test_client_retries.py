import logging
import re

import httpx
import pytest

from config import settings
from ingest.client import BARS_HOST, BARS_PATH, AlpacaClient, FatalVendorError, UnitFetchError
from ingest.throttle import TokenBucket, backoff_delay
from tests.fake_clock import FakeClock


def _scripted_transport(events):
    """Replays one event per physical request in order: a status code, or an exception the transport raises."""
    remaining = list(events)

    def send(request: httpx.Request) -> httpx.Response:
        assert remaining, "the client made more physical requests than this test scripted"
        event = remaining.pop(0)
        if isinstance(event, Exception):
            raise event
        return httpx.Response(event, content=b'{"bars": {}}' if event < 300 else b'{"message":"no."}')

    return httpx.MockTransport(send)


def test_a_429_sequence_longer_than_the_attempt_budget_eventually_succeeds_and_counts_every_attempt():
    retries = settings.HTTP_MAX_ATTEMPTS + 6
    client = AlpacaClient(
        http=httpx.Client(transport=_scripted_transport([429] * retries + [200])),
        sleep=lambda delay: None,
    )

    result = client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    assert result == {"bars": {}}
    # 429 is exempt from the attempt budget, so every physical attempt is counted rather than just the first
    assert client.request_counts["bars"] == retries + 1


def test_a_permanent_503_stops_at_the_total_attempt_budget_and_raises_unit_fetch_error():
    client = AlpacaClient(
        http=httpx.Client(transport=_scripted_transport([503] * settings.HTTP_MAX_ATTEMPTS)),
        sleep=lambda delay: None,
    )

    with pytest.raises(UnitFetchError):
        client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    # the budget counts total requests, not retries after a first try
    assert client.request_counts["bars"] == settings.HTTP_MAX_ATTEMPTS


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_fatal_status_raises_on_the_first_attempt_and_spends_exactly_one_request(status):
    client = AlpacaClient(http=httpx.Client(transport=_scripted_transport([status])), sleep=lambda delay: None)

    with pytest.raises(FatalVendorError):
        client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    assert client.request_counts["bars"] == 1


def test_a_status_outside_both_the_retryable_and_fatal_sets_fails_the_unit_on_the_first_attempt():
    client = AlpacaClient(http=httpx.Client(transport=_scripted_transport([404])), sleep=lambda delay: None)

    with pytest.raises(UnitFetchError):
        client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    # retrying cannot help a status outside both sets, so the unit fails now rather than spending the budget on it
    assert client.request_counts["bars"] == 1


def test_a_transport_error_is_retried_on_the_bounded_budget_and_ends_in_unit_fetch_error():
    errors = [httpx.ConnectTimeout("connection timed out")] * settings.HTTP_MAX_ATTEMPTS
    client = AlpacaClient(http=httpx.Client(transport=_scripted_transport(errors)), sleep=lambda delay: None)

    with pytest.raises(UnitFetchError):
        client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    assert client.request_counts["bars"] == settings.HTTP_MAX_ATTEMPTS


def test_a_503_that_clears_on_the_third_attempt_returns_the_parsed_body_and_counts_three_requests():
    client = AlpacaClient(
        http=httpx.Client(transport=_scripted_transport([503, 503, 200])),
        sleep=lambda delay: None,
    )

    result = client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    assert result == {"bars": {}}
    assert client.request_counts["bars"] == 3


def test_exactly_one_warning_is_logged_per_call_however_many_retries_it_makes(caplog):
    client = AlpacaClient(
        http=httpx.Client(transport=_scripted_transport([503, 503, 503, 200])),
        sleep=lambda delay: None,
    )

    with caplog.at_level(logging.WARNING, logger="ingest.client"):
        client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    # one warning per call is what keeps an unattended run's log readable when a unit retries many times
    assert "status 503" in warnings[0]
    assert "attempt 1" in warnings[0]
    assert re.search(r"waiting \d+\.\ds", warnings[0])


@pytest.mark.parametrize("error", [FatalVendorError, UnitFetchError])
def test_the_retyped_errors_are_runtime_errors_and_not_http_status_errors(error):
    # this is what keeps the two shipped pytest.raises(RuntimeError, match=...) pagination tests meaningful now that fetch_bars' raises were retyped
    assert issubclass(error, RuntimeError)
    assert not issubclass(error, httpx.HTTPStatusError)


def test_the_retry_loop_acquires_a_token_from_the_bucket_on_every_physical_attempt():
    clock = FakeClock()
    bucket = TokenBucket(settings.RATE_LIMIT_RPM, now=lambda: clock.now, sleep=clock.sleep)
    client = AlpacaClient(
        http=httpx.Client(transport=_scripted_transport([503, 503, 503, 200])),
        bucket=bucket,
        sleep=lambda delay: None,
    )

    client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    # an __init__ that builds a bucket and a get_json that never calls acquire would leave this at zero
    assert round(clock.now, 4) == round(4 * 60 / settings.RATE_LIMIT_RPM, 4)


def test_alpaca_client_defaults_to_a_30_second_http_timeout():
    # an unbounded default would hang an unattended run forever against a connection that never resolves
    client = AlpacaClient()

    assert client._http.timeout == httpx.Timeout(30.0)


def test_the_retry_loop_sleeps_the_computed_backoff_delay_rather_than_a_placeholder(monkeypatch):
    # every test above injects sleep=lambda delay: None, which would also pass if the loop slept nothing at all
    monkeypatch.setattr("ingest.client.random.random", lambda: 0.0)
    # a bucket of its own, sleeping on a FakeClock: otherwise the default bucket shares this recorder and its own refill wait mixes into the list below
    clock = FakeClock()
    bucket = TokenBucket(settings.RATE_LIMIT_RPM, now=lambda: clock.now, sleep=clock.sleep)
    recorded = []
    client = AlpacaClient(
        http=httpx.Client(transport=_scripted_transport([503, 503, 503, 200])),
        bucket=bucket,
        sleep=recorded.append,
    )

    client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    assert recorded == [backoff_delay(1, 0.0), backoff_delay(2, 0.0), backoff_delay(3, 0.0)]
    assert all(isinstance(delay, float) and delay > 0 for delay in recorded)


def test_the_auth_headers_are_sent_on_every_request():
    captured = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, content=b'{"bars": {}}')

    client = AlpacaClient(http=httpx.Client(transport=httpx.MockTransport(capture)), sleep=lambda delay: None)

    client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    # a request built with an empty headers mapping would 401 at the real vendor rather than fail anything in this suite
    assert captured["headers"]["APCA-API-KEY-ID"] == settings.ALPACA_KEY_ID
    assert captured["headers"]["APCA-API-SECRET-KEY"] == settings.ALPACA_SECRET_KEY


def test_the_bars_request_method_is_get():
    captured = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        return httpx.Response(200, content=b'{"bars": {}}')

    client = AlpacaClient(http=httpx.Client(transport=httpx.MockTransport(capture)), sleep=lambda delay: None)

    client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")

    # MockTransport accepts any verb, so nothing else here would notice the method silently changing
    assert captured["method"] == "GET"
