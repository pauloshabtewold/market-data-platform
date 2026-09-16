from pathlib import Path

import pytest

_E2E_DIR = Path(__file__).resolve().parent / "e2e"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    # resolved from this file so fixtures and db/migrations are found by path rather than by whatever directory pytest was invoked from.
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


def _names_a_path_inside_e2e(config: pytest.Config) -> bool:
    # config.args holds the same list of paths whether it came from the command line, from
    # testpaths, or from the bare-invocation fallback to the invocation directory -- args_source
    # is what tells those apart, and only ARGS is something anyone actually typed. A bare `pytest`
    # run with cwd = tests/e2e resolves args to that cwd under ArgsSource.INVOCATION_DIR, with no
    # path named at all, and that must not read as naming tests/e2e
    if config.args_source is not pytest.Config.ArgsSource.ARGS:
        return False
    base = Path(config.invocation_params.dir)
    for raw in config.args:
        candidate = Path(raw.split("::", 1)[0])
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == _E2E_DIR or _E2E_DIR in resolved.parents:
            return True
    return False


def pytest_ignore_collect(collection_path: Path, config: pytest.Config):
    # tests/e2e reaches production, so it is opt-in by path rather than swept in by `pytest tests`
    # or `pytest .` the way testpaths alone lets it through: this must name tests/e2e (or a node
    # inside it) as an argument someone actually typed, not merely as where args got resolved from
    resolved = collection_path.resolve()
    if resolved != _E2E_DIR and _E2E_DIR not in resolved.parents:
        return None
    return None if _names_a_path_inside_e2e(config) else True
