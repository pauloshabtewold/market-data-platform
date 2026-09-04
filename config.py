import logging
from datetime import date

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ALPACA_KEY_ID: str
    ALPACA_SECRET_KEY: str
    ALPACA_TRADING_HOST: str
    INGEST_START: date
    INGEST_END: date
    DATABASE_URL: str

    ALPACA_FEED: str = "iex"
    ALPACA_ADJUSTMENT: str = "split,spin-off"
    ALPACA_LIMIT: int = 10000
    RATE_LIMIT_RPM: int = 200
    HTTP_MAX_ATTEMPTS: int = 5
    AGG_MAX_WINDOW_DAYS: int = 90
    # the hot-window partial index's predicate AND the month list Feature 10 copies to RDS -- one
    # value doing both jobs, so the local index and the deployed copy cannot drift apart. floored
    # at 4 because it must contain AGG_MAX_WINDOW_DAYS, the widest window an endpoint can ask for
    HOT_WINDOW_MONTHS: int = 4

    BARS_PAGE_DEFAULT: int = 1000
    BARS_PAGE_MAX: int = 10000
    AGG_PAGE_DEFAULT: int = 100
    AGG_PAGE_MAX: int = 1000
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 10
    LOG_LEVEL: str = "INFO"

    BARS_PER_TICKER_DAY: float | None = None
    DEEP_PAGE_DEPTH: int | None = None
    HEAP_INDEX_BYTE_RATIO: float | None = None
    # query 7's ceiling, and a different index from the one above: 9 and 10 scan the PK, while 7
    # needs vwap and volume too and can only be served by the covering index. two keys rather than
    # one because on identical data they measure far apart, and a single key invites query 7 being
    # judged against the PK's number -- which is flattering and cites an index it cannot use
    HEAP_INDEX_COVERING_RATIO: float | None = None

    @model_validator(mode="after")
    def _hot_window_contains_the_widest_endpoint_window(self):
        # the floor is derived rather than written as 4, so raising AGG_MAX_WINDOW_DAYS at a later
        # feature fails here instead of silently leaving the hot index too short to serve it
        lengths = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
        shortest = min(
            sum(lengths[(start + i) % 12] for i in range(self.HOT_WINDOW_MONTHS))
            for start in range(12)
        )
        if shortest < self.AGG_MAX_WINDOW_DAYS:
            raise ValueError(
                f"HOT_WINDOW_MONTHS={self.HOT_WINDOW_MONTHS} spans as few as {shortest} days,"
                f" which cannot contain AGG_MAX_WINDOW_DAYS={self.AGG_MAX_WINDOW_DAYS};"
                " the hot-window index would not cover the widest window an endpoint can ask for"
            )
        return self

    @model_validator(mode="after")
    def _page_pool_and_log_level_bounds_hold(self):
        if self.BARS_PAGE_DEFAULT > self.BARS_PAGE_MAX:
            raise ValueError(
                f"BARS_PAGE_DEFAULT={self.BARS_PAGE_DEFAULT} exceeds"
                f" BARS_PAGE_MAX={self.BARS_PAGE_MAX}"
            )
        if self.AGG_PAGE_DEFAULT > self.AGG_PAGE_MAX:
            raise ValueError(
                f"AGG_PAGE_DEFAULT={self.AGG_PAGE_DEFAULT} exceeds"
                f" AGG_PAGE_MAX={self.AGG_PAGE_MAX}"
            )
        if self.DB_POOL_MIN > self.DB_POOL_MAX:
            raise ValueError(
                f"DB_POOL_MIN={self.DB_POOL_MIN} exceeds DB_POOL_MAX={self.DB_POOL_MAX}"
            )
        if self.DB_POOL_MAX < 1:
            raise ValueError(
                f"DB_POOL_MAX={self.DB_POOL_MAX} is below the minimum pool size of 1"
            )
        # checked here because basicConfig accepts a bad level silently once a handler already exists
        if self.LOG_LEVEL not in logging.getLevelNamesMapping():
            raise ValueError(
                f"LOG_LEVEL={self.LOG_LEVEL!r} is not one of"
                f" {sorted(logging.getLevelNamesMapping())}"
            )
        return self


settings = Settings()

_MEASURED_BY = {
    "BARS_PER_TICKER_DAY": "the sample ingest run",
    "DEEP_PAGE_DEPTH": "the sample ingest run against the loaded calendar",
    "HEAP_INDEX_BYTE_RATIO": "the loaded partition's heap and index sizes",
    "HEAP_INDEX_COVERING_RATIO": "the loaded partition's heap against the covering index (symbol, ts) INCLUDE (vwap, volume)",
}

# a key added to Settings without a matching entry above would otherwise raise KeyError instead of this function's contract
_MEASURED_FALLBACK = "a measurement recorded in docs/"


def require(name: str):
    # required at use and not at import: these are measured by runs that config.py refusing to import would have made unrunnable -- the sample ingest for the first three, Feature 4's index sweep for the covering ratio
    value = getattr(settings, name)
    if value is None:
        raise RuntimeError(
            f"{name} is unset; it is measured from {_MEASURED_BY.get(name, _MEASURED_FALLBACK)} and written"
            " into .env by hand; docs/METHODOLOGY.md carries every measured value with its arithmetic"
        )
    return value
