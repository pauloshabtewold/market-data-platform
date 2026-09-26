import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest


def _e2e_selection():
    # loaded by path rather than as `tests.e2e_selection`: pytest puts its own rootdir on sys.path,
    # so a run whose rootdir sits outside this repository has no importable `tests` package.
    # Keyed in sys.modules by that path and not by a bare name: one pytest process can load two
    # copies of this repository -- a typed path into a snapshot beside the live tree is ordinary
    # here -- and under a shared key the second copy gets the first copy's module, whose E2E_DIR
    # names the FIRST tree's tests/e2e. Both guard layers read this function, so a shared key
    # switches both of them off for every tree but the first
    path = Path(__file__).resolve().parent / "e2e_selection.py"
    key = "_e2e_selection_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    module = sys.modules.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
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
