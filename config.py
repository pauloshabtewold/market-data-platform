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

    BARS_PER_TICKER_DAY: float | None = None
    DEEP_PAGE_DEPTH: int | None = None
    HEAP_INDEX_BYTE_RATIO: float | None = None


settings = Settings()

_MEASURED_BY = {
    "BARS_PER_TICKER_DAY": "Feature 1 gate item 5",
    "DEEP_PAGE_DEPTH": "Feature 1 gate item 5",
    "HEAP_INDEX_BYTE_RATIO": "Feature 1 gate item 6",
}


def require(name: str):
    # required at use and not at import: these three are measured by the sample ingest, which requiring them at import would have made unrunnable
    value = getattr(settings, name)
    if value is None:
        raise RuntimeError(
            f"{name} is unset; it is measured by {_MEASURED_BY[name]} and written into .env by hand"
        )
    return value
