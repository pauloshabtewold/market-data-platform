import shutil
import subprocess
import sys
import zipfile

import pytest

IGNORED = shutil.ignore_patterns(
    ".git", ".venv", "venv", "build", "dist", "*.egg-info", ".pytest_cache", "__pycache__", "notes"
)


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
    return found, set(zipfile.ZipFile(next(work.glob("*.whl"))).namelist())


def test_every_sql_file_in_the_tree_reaches_the_built_distribution(built):
    found, shipped = built
    assert found, "no .sql files found, so this test would pass vacuously"

    # an editable install copies nothing, so nothing else in this suite can see a data file the packaging metadata leaves behind
    assert [path for path in found if path not in shipped] == []


def test_the_built_distribution_carries_the_python_modules_and_not_the_tests(built):
    _, shipped = built
    assert {"config.py", "db/migrate.py", "db/session.py", "ingest/client.py"} <= shipped
    assert not [name for name in shipped if name.startswith("tests/")]
