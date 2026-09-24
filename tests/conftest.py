import importlib.util
import sys
from pathlib import Path

import pytest


def _e2e_selection():
    # loaded by path rather than as `tests.e2e_selection`: pytest puts its own rootdir on sys.path,
    # so a run whose rootdir sits outside this repository has no importable `tests` package
    module = sys.modules.get("_e2e_selection")
    if module is None:
        path = Path(__file__).resolve().parent / "e2e_selection.py"
        spec = importlib.util.spec_from_file_location("_e2e_selection", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["_e2e_selection"] = module
        spec.loader.exec_module(module)
    return module


is_inside_e2e = _e2e_selection().is_inside_e2e
path_is_typed = _e2e_selection().path_is_typed


@pytest.fixture(scope="session")
def repo_root() -> Path:
    # resolved from this file so fixtures and db/migrations are found by path rather than by whatever directory pytest was invoked from.
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


def pytest_ignore_collect(collection_path: Path, config: pytest.Config):
    # tests/e2e reaches production, so it is opt-in by a path typed on the command line rather than
    # swept in by `pytest tests` or `pytest .`; tests/e2e/conftest.py refuses unnamed items again,
    # item by item, for the runs that skip this file
    if not is_inside_e2e(collection_path):
        return None
    return None if path_is_typed(collection_path, config) else True
