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

MEASURED = (
    "BARS_PER_TICKER_DAY",
    "DEEP_PAGE_DEPTH",
    "HEAP_INDEX_BYTE_RATIO",
    "HEAP_INDEX_COVERING_RATIO",
)

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


def test_measured_and_measured_by_name_the_same_keys():
    # a duplicated list is what let this tuple drift a key behind config.py's _MEASURED_BY once
    assert set(MEASURED) == set(config._MEASURED_BY)


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


def test_defaults_match_the_documented_values(clean_env):
    settings = Settings(_env_file=None, **REQUIRED)
    assert settings.ALPACA_FEED == "iex"
    assert settings.ALPACA_ADJUSTMENT == "split,spin-off"
    assert settings.ALPACA_LIMIT == 10000
    assert settings.RATE_LIMIT_RPM == 200
    assert settings.HTTP_MAX_ATTEMPTS == 5
    assert settings.AGG_MAX_WINDOW_DAYS == 90


def test_require_raises_runtime_error_for_a_key_no_one_mapped(monkeypatch):
    # a later feature adds the Settings field and forgets the _MEASURED_BY entry: still RuntimeError, never KeyError
    monkeypatch.setattr(config, "_MEASURED_BY", {})
    monkeypatch.setattr(config.settings, "DEEP_PAGE_DEPTH", None)
    with pytest.raises(RuntimeError, match="DEEP_PAGE_DEPTH"):
        config.require("DEEP_PAGE_DEPTH")


def test_every_optional_setting_is_reachable_through_require(monkeypatch):
    optional = [
        name for name, field in Settings.model_fields.items() if field.default is None
    ]
    for name in optional:
        monkeypatch.setattr(config.settings, name, None)
        with pytest.raises(RuntimeError, match=name):
            config.require(name)


def test_a_hot_window_too_short_for_the_widest_endpoint_window_is_refused():
    # three consecutive months can be as few as 89 days (Feb+Mar+Apr), which cannot hold a
    # 90-day request, so the hot-window index would miss rows the endpoint is entitled to ask for
    with pytest.raises(ValidationError, match="89 days"):
        Settings(_env_file=None, HOT_WINDOW_MONTHS=3, **REQUIRED)


def test_the_hot_window_floor_follows_the_window_it_has_to_contain():
    # 120 is the exact minimum four months can guarantee -- pinned at both edges so a floor that
    # were the literal 4 rather than this arithmetic could not also produce the 89-day rejection
    Settings(_env_file=None, HOT_WINDOW_MONTHS=4, AGG_MAX_WINDOW_DAYS=120, **REQUIRED)
    with pytest.raises(ValidationError, match="89 days"):
        Settings(_env_file=None, HOT_WINDOW_MONTHS=3, AGG_MAX_WINDOW_DAYS=120, **REQUIRED)
    with pytest.raises(ValidationError, match="120 days"):
        Settings(_env_file=None, HOT_WINDOW_MONTHS=4, AGG_MAX_WINDOW_DAYS=121, **REQUIRED)
