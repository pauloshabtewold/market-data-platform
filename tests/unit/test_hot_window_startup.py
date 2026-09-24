import os
import subprocess
import sys

import pytest

import api.main
from api.main import create_app
from config import Settings

DEAD_DSN = "postgresql://postgres:postgres@127.0.0.1:1/nonexistent"

REQUIRED = {
    "ALPACA_KEY_ID": "dead",
    "ALPACA_SECRET_KEY": "dead",
    "ALPACA_TRADING_HOST": "http://127.0.0.1:1",
    "INGEST_START": "2020-08-01",
    "INGEST_END": "2026-06-30",
    "DATABASE_URL": DEAD_DSN,
}

# INGEST_END=2026-05-01 at HOT_WINDOW_MONTHS=4: the hot window starts 2026-02-01, 89 days back, one
# short of AGG_MAX_WINDOW_DAYS=90
SHORT = {"INGEST_END": "2026-05-01", "HOT_WINDOW_MONTHS": "4", "AGG_MAX_WINDOW_DAYS": "90"}
SHORT_PROBLEM = (
    "HOT_WINDOW_MONTHS=4 anchored at INGEST_END=2026-05-01 spans 89 days back to 2026-02-01, short of"
    " AGG_MAX_WINDOW_DAYS=90; the hot-window index would not cover the widest window an endpoint can"
    " ask for"
)


def _settings(**overrides):
    return Settings(_env_file=None, **{**REQUIRED, **overrides})


def test_create_app_refuses_a_hot_window_that_cannot_serve_the_widest_window(monkeypatch):
    monkeypatch.setattr(api.main, "settings", _settings(**SHORT))
    with pytest.raises(RuntimeError) as excinfo:
        create_app(dsn=DEAD_DSN)
    assert str(excinfo.value) == "refusing to build the app:\n- " + SHORT_PROBLEM


def test_create_app_reads_the_settings_it_serves_with(monkeypatch):
    # one day short refused, the exact span built: HOT_WINDOW_MONTHS=3 at INGEST_END=2026-06-30
    # reaches back to 2026-04-01, 90 days
    monkeypatch.setattr(api.main, "settings", _settings(HOT_WINDOW_MONTHS=3, AGG_MAX_WINDOW_DAYS=90))
    assert create_app(dsn=DEAD_DSN) is not None
    monkeypatch.setattr(api.main, "settings", _settings(HOT_WINDOW_MONTHS=3, AGG_MAX_WINDOW_DAYS=91))
    with pytest.raises(RuntimeError, match="HOT_WINDOW_MONTHS=3 anchored at INGEST_END=2026-06-30 spans 90"):
        create_app(dsn=DEAD_DSN)
    monkeypatch.setattr(api.main, "settings", _settings(HOT_WINDOW_MONTHS=0))
    with pytest.raises(RuntimeError, match="HOT_WINDOW_MONTHS=0 is below 1"):
        create_app(dsn=DEAD_DSN)


def test_a_hot_window_only_the_api_refuses_lets_migrate_and_the_ingest_start(repo_root, tmp_path):
    # a subprocess, because config builds its settings at import: this is the configuration an
    # operator has when INGEST_END moves, and only the API serves from the hot-window index. cwd is
    # empty so no .env can reach the child
    env = {key: value for key, value in os.environ.items() if key not in Settings.model_fields}
    env.update(REQUIRED)
    env.update(SHORT)
    env.update({"PYTHONPATH": str(repo_root), "PYTHONDONTWRITEBYTECODE": "1"})
    script = (
        "import config, db.migrate, ingest.__main__, ingest.pipeline\n"
        "print(config.__file__)\n"
        "print(config.settings.INGEST_END)\n"
        "try:\n"
        "    import api.main\n"
        "except RuntimeError as exc:\n"
        "    print(repr(str(exc)))\n"
        "else:\n"
        "    print('api.main imported')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(repo_root / "config.py"),
        "2026-05-01",
        repr("refusing to build the app:\n- " + SHORT_PROBLEM),
    ]
