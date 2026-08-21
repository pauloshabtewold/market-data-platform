import pytest
from pydantic import ValidationError

import config
from config import Settings

REQUIRED = {
    "ALPACA_KEY_ID": "key",
    "ALPACA_SECRET_KEY": "secret",
    "ALPACA_TRADING_HOST": "https://paper-api.alpaca.markets",
    "INGEST_START": "2020-08-01",
    "INGEST_END": "2026-06-30",
    "DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:5432/marketdata",
}

MEASURED = ("BARS_PER_TICKER_DAY", "DEEP_PAGE_DEPTH", "HEAP_INDEX_BYTE_RATIO")

DEFAULTED = (
    "ALPACA_FEED",
    "ALPACA_ADJUSTMENT",
    "ALPACA_LIMIT",
    "RATE_LIMIT_RPM",
    "HTTP_MAX_ATTEMPTS",
    "AGG_MAX_WINDOW_DAYS",
)


@pytest.fixture
def clean_env(monkeypatch):
    for key in MEASURED + DEFAULTED:
        monkeypatch.delenv(key, raising=False)


def test_measured_keys_are_absent_legitimately(clean_env):
    settings = Settings(_env_file=None, **REQUIRED)
    for key in MEASURED:
        assert getattr(settings, key) is None


def test_require_raises_naming_the_missing_key(monkeypatch):
    monkeypatch.setattr(config.settings, "DEEP_PAGE_DEPTH", None)
    with pytest.raises(RuntimeError) as excinfo:
        config.require("DEEP_PAGE_DEPTH")
    assert "DEEP_PAGE_DEPTH" in str(excinfo.value)


def test_require_returns_the_value_once_measured(monkeypatch):
    monkeypatch.setattr(config.settings, "DEEP_PAGE_DEPTH", 402_000)
    assert config.require("DEEP_PAGE_DEPTH") == 402_000


def test_missing_credential_fails_at_construction(monkeypatch):
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    without_key_id = {k: v for k, v in REQUIRED.items() if k != "ALPACA_KEY_ID"}
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, **without_key_id)
    assert "ALPACA_KEY_ID" in str(excinfo.value)


def test_defaults_are_the_section_8_values(clean_env):
    settings = Settings(_env_file=None, **REQUIRED)
    assert settings.ALPACA_FEED == "iex"
    assert settings.ALPACA_ADJUSTMENT == "split,spin-off"
    assert settings.ALPACA_LIMIT == 10000
    assert settings.RATE_LIMIT_RPM == 200
    assert settings.HTTP_MAX_ATTEMPTS == 5
    assert settings.AGG_MAX_WINDOW_DAYS == 90
