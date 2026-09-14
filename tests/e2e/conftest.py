import httpx
import pytest

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
def _service_is_up(client):
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
