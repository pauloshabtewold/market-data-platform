import shutil
import subprocess
import sys
import zipfile

import pytest

from db.session import connect

# .env is in here because this copies the tree out of the repository, and the credential file must not travel with it.
IGNORED = shutil.ignore_patterns(
    ".git", ".venv", "venv", "build", "dist", "*.egg-info", ".pytest_cache", "__pycache__",
    "notes", ".env",
)

# enough to import config in a subprocess that has no .env; only DATABASE_URL is read by db.migrate.
STUB_ENV = {
    "ALPACA_KEY_ID": "unused",
    "ALPACA_SECRET_KEY": "unused",
    "ALPACA_TRADING_HOST": "https://paper-api.alpaca.markets",
    "INGEST_START": "2020-08-01",
    "INGEST_END": "2026-06-30",
}


@pytest.fixture(scope="module")
def built(repo_root, tmp_path_factory):
    # built from a copy so an in-tree setuptools build leaves no artifacts in the working tree
    work = tmp_path_factory.mktemp("dist")
    source = work / "src"
    shutil.copytree(repo_root, source, ignore=IGNORED)
    # enumerated before the build, which writes its own copy of everything it ships into build/lib
    found = sorted(str(path.relative_to(source)) for path in source.rglob("*.sql"))
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--no-index", "-q", "-w", str(work), str(source)],
        check=True, capture_output=True, text=True,
    )
    wheel = next(work.glob("*.whl"))
    return wheel, found, set(zipfile.ZipFile(wheel).namelist())


@pytest.fixture(scope="module")
def installed(built, tmp_path_factory):
    # --target rather than a venv: it is a real non-editable install of the wheel and it costs a second rather than twenty
    wheel, _, _ = built
    target = tmp_path_factory.mktemp("site")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "-q",
         "--target", str(target), str(wheel)],
        check=True, capture_output=True, text=True,
    )
    return target


def test_every_sql_file_in_the_tree_reaches_the_built_distribution(built):
    _, found, shipped = built
    assert found, "no .sql files found, so this test would pass vacuously"

    # an editable install copies nothing, so nothing else in this suite can see a data file the packaging metadata leaves behind
    assert [path for path in found if path not in shipped] == []


def test_the_built_distribution_carries_the_python_modules_and_not_the_tests(built):
    _, _, shipped = built
    assert {"config.py", "db/migrate.py", "db/session.py", "ingest/client.py"} <= shipped
    assert not [name for name in shipped if name.startswith("tests/")]


def test_the_installed_distribution_migrates_a_database_from_zero(built, installed, fresh_dsn, tmp_path):
    _, found, _ = built
    dsn = fresh_dsn()
    program = (
        "import pathlib, sys, db.migrate\n"
        f"assert pathlib.Path(db.migrate.__file__).is_relative_to(pathlib.Path({str(installed)!r})), db.migrate.__file__\n"
        "db.migrate.main()\n"
    )
    # cwd is elsewhere and PYTHONPATH is the installed tree, so the repository's own db/ cannot answer the import
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(installed), "DATABASE_URL": dsn, **STUB_ENV},
    )

    assert result.returncode == 0, result.stderr
    # counted from the tree the wheel was built from rather than from the installed copy, so a fifth migration at Feature 4 does not turn a correct run red while a wheel shipping fewer files than the tree holds still does
    expected = len([path for path in found if path.startswith("db/migrations/")])
    assert f"applied {expected} migrations" in result.stdout

    # the half no archive listing can reach: a distribution shipping some of the migrations still reports success
    with connect(dsn) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"bars", "symbols", "market_days", "ingest_progress"} <= tables
