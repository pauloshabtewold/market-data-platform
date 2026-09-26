import hashlib
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

import config
from config import settings


def _e2e_selection():
    # loaded by path rather than as `tests.e2e_selection`: pytest puts its own rootdir on sys.path,
    # so a run whose rootdir sits outside this repository has no importable `tests` package.
    # Keyed in sys.modules by that path and not by a bare name: one pytest process can load two
    # copies of this repository -- a typed path into a snapshot beside the live tree is ordinary
    # here -- and under a shared key the second copy gets the first copy's module, whose E2E_DIR
    # names the FIRST tree's tests/e2e. Both guard layers read this function, so a shared key
    # switches both of them off for every tree but the first
    path = Path(__file__).resolve().parent.parent / "e2e_selection.py"
    key = "_e2e_selection_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    module = sys.modules.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return module


deselect_untyped_e2e_items = _e2e_selection().deselect_untyped_e2e_items


def pytest_collection_modifyitems(config, items):
    # this file loads even when a conftest-scope option such as --confcutdir skips tests/conftest.py,
    # so the refusal of any test here that no typed argument names has to live here too
    deselect_untyped_e2e_items(config, items)


@pytest.fixture(scope="session")
def client(_e2e_configuration_is_valid):
    # after the check, whatever order autouse fixtures happen to run in: an empty E2E_BASE_URL must
    # never become a client
    with httpx.Client(base_url=settings.E2E_BASE_URL, timeout=30) as c:
        yield c


@pytest.fixture(scope="session")
def window():
    return {"start": settings.E2E_START.isoformat(), "end": settings.E2E_END.isoformat()}


@pytest.fixture(scope="session")
def symbols():
    # stripped per element: a .env written with spaces after the commas must not turn into a
    # symbol nothing was ever ingested under
    return [s.strip() for s in settings.E2E_SYMBOLS.split(",")]


@pytest.fixture(scope="session", autouse=True)
def _e2e_configuration_is_valid():
    # runs before any request: a widened INGEST_END with a stale E2E_* window must fail here,
    # loudly and by name, rather than as an obscure 422 from the first real request below
    problems = config.e2e_configuration_problems(settings)
    if problems:
        pytest.fail(
            "the e2e suite's own configuration is not runnable:\n- " + "\n- ".join(problems),
            pytrace=False,
        )


@pytest.fixture(scope="session", autouse=True)
def _service_is_up(client, _e2e_configuration_is_valid):
    # a skip reads as a pass on a suite that only reports green or red, so this fails loudly instead
    start_command = "docker compose up -d --wait db app"
    try:
        response = client.get("/health")
    except httpx.RequestError as exc:
        pytest.fail(
            f"{settings.E2E_BASE_URL} is unreachable ({exc}) -- start it with: {start_command}",
            pytrace=False,
        )
    if response.status_code != 200:
        pytest.fail(
            f"{settings.E2E_BASE_URL}/health answered {response.status_code}, not 200 -- "
            f"start it with: {start_command}",
            pytrace=False,
        )
