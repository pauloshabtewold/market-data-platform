import time

BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0


class TokenBucket:
    def __init__(self, rpm: int, now=time.monotonic, sleep=time.sleep) -> None:
        self._capacity = rpm
        self._rate = rpm / 60
        self._now = now
        self._sleep = sleep
        self._last = now()
        # starting empty makes the rate invariant true from construction rather than only in the limit
        self._tokens = 0.0

    def acquire(self) -> None:
        # one correction and no loop, which is exact while now and sleep share a clock and deliberately non-blocking when a test injects a sleep that does not advance one
        self._refill()
        if self._tokens < 1:
            self._sleep((1 - self._tokens) / self._rate)
            self._refill()
        self._tokens -= 1

    def _refill(self) -> None:
        now = self._now()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now


def backoff_delay(attempt: int, rand: float) -> float:
    # the exponent is clamped because the 429 path is unbounded and 2 ** 1024 overflows the multiply before min can discard it
    ceiling = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** min(attempt - 1, 20))
    # half the ceiling plus jitter is never zero, so a 429 is never retried hot against the limit it reports
    return ceiling / 2 + rand * ceiling / 2
