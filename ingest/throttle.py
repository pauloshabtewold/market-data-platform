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
        # starting empty holds the rate invariant from construction, not just in the limit
        self._tokens = 0.0

    def acquire(self) -> None:
        # one correction, no loop: exact if now and sleep share a clock, non-blocking if not
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
    # clamped: the 429 path is unbounded and 2 ** 1024 overflows the multiply before min discards it
    ceiling = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** min(attempt - 1, 20))
    # half the ceiling plus jitter is never zero, so a 429 never retries hot against its limit
    return ceiling / 2 + rand * ceiling / 2
