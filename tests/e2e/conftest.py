import httpx
import pytest

import config
from config import settings


@pytest.fixture(scope="session")
def client():
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
