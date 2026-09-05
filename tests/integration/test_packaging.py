import shutil
import subprocess
import sys
import tomllib
import zipfile

import pytest

from db.session import connect

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
    # copied from what git tracks rather than from a denylist of directory names: a denylist has to
    # be extended for every new working directory, and it silently ships a credential file or a
    # build artifact the day someone forgets. This also builds exactly what a fresh clone builds.
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=repo_root, check=True, capture_output=True, text=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        # the one thing the denylist form did not need. An sdist, a vendored copy or a build context
        # with no .git reaches this, and check=True swallows git's own message into a captured pipe
        detail = (getattr(exc, "stderr", "") or "").strip() or exc
        raise RuntimeError(
            f"this fixture builds its copy from `git ls-files`, so it needs a git checkout: {detail}"
        ) from exc
    tracked = [name for name in listing.split("\0") if name]
    assert tracked, "git ls-files returned nothing, so this fixture would build an empty tree"
    # git excludes the credential file because it is untracked, which is a weaker guarantee than the
    # denylist's -- the denylist named it and would have held even against a `git add -f`. Every
    # .env* but the committed example, because .gitignore has had to be extended for a variant once
    # already and the next one is a file nobody remembers to add.
    leaked = [
        name for name in tracked
        if name.rsplit("/", 1)[-1].startswith(".env") and name != ".env.example"
    ]
    assert not leaked, f"a credential file is tracked and must not reach a build copy: {leaked}"
    for name in tracked:
        origin = repo_root / name
        # a submodule is a gitlink and a symlink to a directory resolves to one; copy2 raises
        # IsADirectoryError on either, with the path nowhere in the message
        assert origin.is_file(), f"{name} is not a regular file; this fixture copies files"
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, destination)
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--no-index", "-q", "-w", str(work), str(source)],
        check=True, capture_output=True, text=True,
    )
    wheel = next(work.glob("*.whl"))
    return wheel, tracked, set(zipfile.ZipFile(wheel).namelist())


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
    _, tracked, shipped = built
    # enumerated from the repository rather than from the copy: a .sql file the copy loop never
    # wrote is absent from both sides of the comparison, and the test passes without noticing it
    found = sorted(name for name in tracked if name.endswith(".sql"))
    assert found, "no .sql files found, so this test would pass vacuously"

    # an editable install copies nothing, so nothing else in this suite can see a data file the packaging metadata leaves behind
    assert [path for path in found if path not in shipped] == []


def test_the_built_distribution_carries_the_python_modules_and_not_the_tests(built, repo_root):
    _, tracked, shipped = built
    # derived from the tree and then compared with pyproject.toml, in that order. Deriving it FROM
    # pyproject would move both sides together: dropping "api" from `packages` would drop the api
    # modules from the expectation as well, which is the exact failure this test exists for. A
    # top-level directory carrying a tracked __init__.py is an importable package; tests/ has none.
    packages = {name.split("/")[0] for name in tracked if name.endswith("/__init__.py")}
    modules = {name for name in tracked if name.endswith(".py") and "/" not in name}
    modules |= {name for name in tracked if name.endswith(".py") and name.split("/")[0] in packages}
    # the metadata has to name every one of them. It named the four packages that existed at
    # Feature 1 while five importable api/ modules entered the distribution unasserted, and the
    # hardcoded spot check this replaced would have gone the same way at the next package
    declared = tomllib.loads((repo_root / "pyproject.toml").read_text())["tool"]["setuptools"]
    assert set(declared["packages"]) == packages
    assert {f"{name}.py" for name in declared["py-modules"]} == {n for n in modules if "/" not in n}
    assert modules <= shipped
    assert not [name for name in shipped if name.startswith("tests/")]


def test_the_installed_distribution_migrates_a_database_from_zero(built, installed, fresh_dsn, tmp_path):
    _, tracked, _ = built
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
    expected = len([name for name in tracked if name.startswith("db/migrations/")])
    assert f"applied {expected} migrations" in result.stdout

    # the half no archive listing can reach: a distribution shipping some of the migrations still reports success
    with connect(dsn) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"bars", "symbols", "market_days", "ingest_progress"} <= tables
