from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    # resolved from this file so fixtures and db/migrations are found by path rather than by whatever directory pytest was invoked from.
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"
