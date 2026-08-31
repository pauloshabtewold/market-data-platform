from datetime import date

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

    BARS_PER_TICKER_DAY: float | None = None
    DEEP_PAGE_DEPTH: int | None = None
    HEAP_INDEX_BYTE_RATIO: float | None = None
    # query 7's ceiling, and a different index from the one above: 9 and 10 scan the PK, while 7
    # needs vwap and volume too and can only be served by the covering index. two keys rather than
    # one because on identical data they measure far apart, and a single key invites query 7 being
    # judged against the PK's number -- which is flattering and cites an index it cannot use
    HEAP_INDEX_COVERING_RATIO: float | None = None


settings = Settings()

_MEASURED_BY = {
    "BARS_PER_TICKER_DAY": "the sample ingest run",
    "DEEP_PAGE_DEPTH": "the sample ingest run against the loaded calendar",
    "HEAP_INDEX_BYTE_RATIO": "the loaded partition's heap and index sizes",
    "HEAP_INDEX_COVERING_RATIO": "the covering index measured at Feature 4, in the only window it exists",
}

# a key added to Settings without a matching entry above would otherwise raise KeyError instead of this function's contract
_MEASURED_FALLBACK = "a measurement recorded in README.md"


def require(name: str):
    # required at use and not at import: these three are measured by the sample ingest, which requiring them at import would have made unrunnable
    value = getattr(settings, name)
    if value is None:
        raise RuntimeError(
            f"{name} is unset; it is measured from {_MEASURED_BY.get(name, _MEASURED_FALLBACK)} and written"
            " into .env by hand; README.md carries the value and the arithmetic"
        )
    return value
