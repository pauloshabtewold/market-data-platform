import os
import subprocess
import sys
from pathlib import Path

# enough to construct config.Settings with no real .env visible via the inherited environment;
# the dead ports mean a collection that somehow ran past --collect-only would still reach nothing
DEAD_ENV = {
    "ALPACA_KEY_ID": "dead",
    "ALPACA_SECRET_KEY": "dead",
    "ALPACA_TRADING_HOST": "http://127.0.0.1:1",
    "INGEST_START": "2020-08-01",
    "INGEST_END": "2026-06-30",
    "DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:1/nonexistent",
    "E2E_BASE_URL": "http://127.0.0.1:1",
}


def _collect(repo_root: Path, arg: str | None, *, cwd: Path | None = None) -> str:
    # a real subprocess, not an in-process Pytester run: pytest_ignore_collect reads
    # config.args_source, which only a fresh invocation populates the way a bare `pytest`, a
    # `pytest tests` or a `pytest tests/e2e` -- run from whatever cwd -- actually would
    cmd = [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider"]
    if arg is not None:
        cmd.append(arg)
    result = subprocess.run(
        cmd,
        cwd=cwd or repo_root,
        # overlaid rather than replacing os.environ, so a harness running this suite keeps its own
        # variables in the child
        env={**os.environ, **DEAD_ENV, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def _e2e_node_count(output: str) -> int:
    return sum(1 for line in output.splitlines() if line.startswith("tests/e2e/"))


def test_naming_tests_collects_no_end_to_end_test(repo_root):
    output = _collect(repo_root, "tests")
    assert _e2e_node_count(output) == 0, output


def test_naming_tests_e2e_collects_every_end_to_end_test(repo_root):
    output = _collect(repo_root, "tests/e2e")
    assert _e2e_node_count(output) >= 17, output


def test_a_bare_run_from_inside_tests_e2e_collects_no_end_to_end_test(repo_root):
    # no argument at all, with cwd = tests/e2e: config.args resolves to that cwd under
    # ArgsSource.INVOCATION_DIR, purely because that happens to be the caller's directory -- nobody
    # typed a path, and that must not read as naming tests/e2e
    output = _collect(repo_root, None, cwd=repo_root / "tests" / "e2e")
    assert _e2e_node_count(output) == 0, output


def test_a_dot_argument_from_inside_tests_e2e_collects_every_end_to_end_test(repo_root):
    # "." IS something the invoker typed (ArgsSource.ARGS), and it resolves to tests/e2e from that
    # cwd -- the guard is about where the path came from, not what it looks like on its own
    output = _collect(repo_root, ".", cwd=repo_root / "tests" / "e2e")
    assert _e2e_node_count(output) >= 17, output
