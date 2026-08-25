from ingest.throttle import BACKOFF_CAP, TokenBucket, backoff_delay
from tests.fake_clock import FakeClock


def test_ten_acquires_against_an_empty_bucket_cost_ten_intervals():
    clock = FakeClock()
    bucket = TokenBucket(200, now=lambda: clock.now, sleep=clock.sleep)

    for _ in range(10):
        bucket.acquire()

    assert round(clock.now, 4) == 3.0


def test_cumulative_acquires_never_exceed_elapsed_time_times_the_rate():
    clock = FakeClock()
    bucket = TokenBucket(200, now=lambda: clock.now, sleep=clock.sleep)

    for n in range(1, 401):
        bucket.acquire()
        assert n - clock.now * 200 / 60 <= 1e-9


def test_a_bucket_idle_for_a_full_second_between_acquires_never_sleeps():
    clock = FakeClock()
    bucket = TokenBucket(200, now=lambda: clock.now, sleep=clock.sleep)

    for _ in range(5):
        clock.now += 1.0
        bucket.acquire()

    assert clock.slept == []


def test_backoff_delay_matches_the_literal_schedule_at_rand_zero_and_rand_one():
    assert [backoff_delay(n, 0.0) for n in (1, 2, 3, 4, 5)] == [0.5, 1.0, 2.0, 4.0, 8.0]
    assert [backoff_delay(n, 1.0) for n in (1, 2, 3, 4, 5)] == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_delay_saturates_at_the_cap_and_is_never_zero_at_rand_zero():
    assert backoff_delay(12, 1.0) == BACKOFF_CAP
    assert backoff_delay(1, 0.0) > 0


def test_the_bucket_never_asks_to_sleep_a_negative_duration():
    clock = FakeClock()
    bucket = TokenBucket(200, now=lambda: clock.now, sleep=clock.sleep)

    for step in (0.0, 0.05, 0.5, 0.31, 0.0, 1.0, 0.29, 0.7):
        clock.now += step
        bucket.acquire()

    # the real time.sleep raises ValueError on a negative duration, so a deficit computed against the wrong threshold takes the run down rather than pacing it
    assert all(delay >= 0 for delay in clock.slept), clock.slept
