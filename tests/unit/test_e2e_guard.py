import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

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

# loaded into each child through PYTEST_PLUGINS rather than read back from its printed output, whose
# node-id prefix moves with --rootdir and whose layout moves with -v
_RECORDER = '''
import json
import os
import sys
from pathlib import Path

_deselected = []


def _key(item):
    return str(Path(item.path).resolve()) + "::" + "::".join(item.nodeid.split("::")[1:])


def pytest_deselected(items):
    _deselected.extend(_key(item) for item in items)


def pytest_collection_finish(session):
    config_module = sys.modules.get("config")
    Path(os.environ["E2E_GUARD_RECORD"]).write_text(json.dumps({
        "items": [_key(item) for item in session.items],
        "deselected": _deselected,
        "config_file": getattr(config_module, "__file__", None),
    }))
'''


class Collection:
    def __init__(self, returncode: int, record: dict | None, output: str):
        self.returncode = returncode
        self.items = set(record["items"]) if record else set()
        self.deselected = set(record["deselected"]) if record else set()
        self.config_file = record["config_file"] if record else None
        self.output = output


def _collect(repo_root: Path, workdir: Path, name: str, args, cwd: Path, extra_env=None) -> Collection:
    # a real subprocess, not an in-process Pytester run: the guard reads the arguments pytest is
    # invoked with and the directory it is invoked from, which only a fresh invocation populates
    record = workdir / f"{name}.json"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    # overlaid rather than replacing os.environ, so a harness running this suite keeps its own
    # variables in the child
    env.update(DEAD_ENV)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(p for p in (str(workdir), env.get("PYTHONPATH")) if p),
            "PYTEST_PLUGINS": "e2e_guard_recorder",
            "E2E_GUARD_RECORD": str(record),
        }
    )
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    payload = json.loads(record.read_text()) if record.exists() else None
    return Collection(result.returncode, payload, (result.stdout + result.stderr)[-4000:])


@pytest.fixture(scope="module")
def forms(repo_root, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("e2e_guard")
    (workdir / "e2e_guard_recorder.py").write_text(_RECORDER)
    tests_dir = repo_root / "tests"
    e2e_dir = tests_dir / "e2e"

    def run_all(specs):
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                name: pool.submit(_collect, repo_root, workdir, name, args, cwd, extra)
                for name, (args, cwd, extra) in specs.items()
            }
            return {name: future.result() for name, future in futures.items()}

    # a SECOND copy of this tree's guard, built beside the first: one pytest process loading both
    # is the case every form below misses, because they all drive one tree
    second = workdir / "second"
    (second / "tests" / "e2e").mkdir(parents=True, exist_ok=True)
    for name in ("conftest.py", "e2e_selection.py"):
        (second / "tests" / name).write_text((tests_dir / name).read_text())
    (second / "tests" / "e2e" / "conftest.py").write_text((e2e_dir / "conftest.py").read_text())
    # a stand-in rather than a copy of the real file: what is under test is which items the guard
    # hides in the second tree, not what those items do
    (second / "tests" / "e2e" / "test_second_tree_e2e.py").write_text(
        "def test_placeholder():\n    pass\n"
    )
    # and one outside its tests/e2e, so a collection that never reached the copy at all is
    # distinguishable from one the guard turned away
    (second / "tests" / "test_second_tree_unit.py").write_text(
        "def test_placeholder():\n    pass\n"
    )
    # tests/e2e reached through a path that only normalises to it, so the difference between
    # Path.resolve() and Path.absolute() is observable: absolute() normalises no .. segment
    dotted_file = "tests/unit/../e2e/test_api_e2e.py"

    # the expected sets, from collections the guard cannot touch: --noconftest loads neither
    # conftest, and naming tests/unit and tests/integration never walks into tests/e2e
    collected = run_all(
        {
            "second_tree_beside_one_live_file": (
                ["tests/unit/test_pagination.py", str(second / "tests")],
                repo_root,
                None,
            ),
            "dotted_path_into_e2e": ([dotted_file], repo_root, None),
            "e2e_unguarded": (["--noconftest", "tests/e2e"], repo_root, None),
            "every_other": (["tests/unit", "tests/integration"], repo_root, None),
            "tests": (["tests"], repo_root, None),
            # from tests/, so the form does not also collect whatever else a working copy holds
            "dot_from_tests": (["."], tests_dir, None),
            "bare_from_root": ([], repo_root, None),
            "bare_inside_e2e": ([], e2e_dir, None),
            "dot_inside_e2e": (["."], e2e_dir, None),
            "tests_e2e": (["tests/e2e"], repo_root, None),
            "confcutdir_unit": (["--confcutdir=tests/unit", "tests"], repo_root, None),
            "confcutdir_integration": (["--confcutdir=tests/integration", "tests"], repo_root, None),
            "confcutdir_e2e": (["--confcutdir=tests/e2e", "tests"], repo_root, None),
            "confcutdir_e2e_as_a_value": (["--confcutdir", "tests/e2e", "tests"], repo_root, None),
            # a typed path inside tests/e2e that is not a test file, with the root guard skipped
            "confcutdir_unit_tests_and_e2e_conftest": (
                ["--confcutdir=tests/unit", "tests", "tests/e2e/conftest.py"],
                repo_root,
                None,
            ),
            "addopts_bare": ([], repo_root, {"PYTEST_ADDOPTS": "tests/e2e"}),
            "addopts_beside_unit": (["tests/unit"], repo_root, {"PYTEST_ADDOPTS": "tests/e2e"}),
        }
    )
    e2e_all = collected["e2e_unguarded"].items
    assert collected["e2e_unguarded"].returncode == 0, collected["e2e_unguarded"].output
    assert e2e_all, collected["e2e_unguarded"].output
    node_key = sorted(e2e_all)[0]
    node_path, _, node_names = node_key.partition("::")
    node_id = f"{Path(node_path).relative_to(repo_root).as_posix()}::{node_names}"
    collected.update(
        run_all(
            {
                "node_alone": ([node_id], repo_root, None),
                "tests_and_node": (["tests", node_id], repo_root, None),
                "confcutdir_unit_tests_and_node": (
                    ["--confcutdir=tests/unit", "tests", node_id],
                    repo_root,
                    None,
                ),
                "deselect_option_naming_node": (["--deselect", node_id, "tests"], repo_root, None),
            }
        )
    )
    for name, collection in collected.items():
        # every child has to have imported this tree's config, or its verdict is about another tree
        assert collection.config_file in (None, str(repo_root / "config.py")), (
            name,
            collection.config_file,
        )
    collected["e2e_all"] = e2e_all
    collected["node_key"] = node_key
    return collected


def _inside(key: str, directory: Path) -> bool:
    return Path(key.partition("::")[0]).is_relative_to(directory)


def test_the_expected_sets_come_from_collections_that_found_tests(forms, repo_root):
    e2e_dir = repo_root / "tests" / "e2e"
    every_other = forms["every_other"]
    assert every_other.returncode == 0, every_other.output
    assert every_other.items and not any(_inside(key, e2e_dir) for key in every_other.items)
    assert all(_inside(key, e2e_dir) for key in forms["e2e_all"])


def test_naming_tests_collects_every_other_test_and_never_walks_into_tests_e2e(forms):
    collection = forms["tests"]
    assert collection.returncode == 0, collection.output
    assert collection.items == forms["every_other"].items, collection.output
    # nothing deselected: the directory is never collected, so tests/e2e/conftest.py never loads
    assert collection.deselected == set(), collection.output


def test_a_dot_typed_from_tests_collects_no_end_to_end_test(forms):
    collection = forms["dot_from_tests"]
    assert collection.returncode == 0, collection.output
    assert collection.items == forms["every_other"].items, collection.output
    assert collection.deselected == set(), collection.output


def test_a_bare_run_from_the_root_collects_no_end_to_end_test(forms):
    collection = forms["bare_from_root"]
    assert collection.returncode == 0, collection.output
    assert collection.items == forms["every_other"].items, collection.output


def test_a_bare_run_from_inside_tests_e2e_collects_nothing_and_exits_cleanly(forms):
    # no argument at all, with cwd = tests/e2e: nobody typed a path, and that must not read as naming
    # tests/e2e. Exit 5 is pytest's "no tests collected" -- a guard that crashed would exit 2 or 3
    collection = forms["bare_inside_e2e"]
    assert collection.returncode == 5, collection.output
    assert collection.items == set(), collection.output


def test_a_dot_argument_from_inside_tests_e2e_collects_every_end_to_end_test(forms):
    # "." IS something the invoker typed, and it resolves to tests/e2e from that cwd -- the guard is
    # about where the path came from, not what it looks like on its own
    collection = forms["dot_inside_e2e"]
    assert collection.returncode == 0, collection.output
    assert collection.items == forms["e2e_all"], collection.output


def test_a_second_copy_of_this_tree_is_guarded_too(forms, repo_root, tmp_path_factory):
    # the guard's module is cached in sys.modules; under a bare key the first tree loaded in a
    # process wins and every later copy's tests/e2e is invisible to BOTH layers, so the copy's
    # end-to-end tests collect although only an ancestor of them was typed. Copies of this tree are
    # routine here -- a snapshot of a commit, a per-breakage tree -- and a path into one typed
    # beside a live test file is all it takes
    collection = forms["second_tree_beside_one_live_file"]
    assert collection.returncode == 0, collection.output
    # the copy's own non-e2e test is collected, so the copy was walked: this is the copy's guard
    # turning its tests/e2e away and not a collection that never reached the copy
    assert any("test_second_tree_unit.py" in key for key in collection.items), collection.output
    assert any("test_pagination.py" in key for key in collection.items), collection.output
    assert [key for key in collection.items if "test_second_tree_e2e.py" in key] == [], (
        collection.output
    )
    assert [key for key in collection.deselected if "test_second_tree_e2e.py" in key] == [], (
        collection.output
    )


def test_a_typed_path_that_only_normalises_to_tests_e2e_is_typed(forms):
    # tests/unit/../e2e/test_api_e2e.py IS tests/e2e/test_api_e2e.py, and the guard resolves a path
    # before comparing it, so the file is typed and collects. Path.absolute() would leave the ..
    # segment in place, tests/e2e would not be among the parents, and the same request would collect
    # nothing -- silently, since a guard that hides too much looks like a guard that works
    collection = forms["dotted_path_into_e2e"]
    assert collection.returncode == 0, collection.output
    assert collection.items == forms["e2e_all"], collection.output
    assert collection.deselected == set(), collection.output


def test_a_sibling_whose_name_merely_starts_with_the_end_to_end_directorys_is_not_inside_it():
    # in process, because the distinction is in one comparison: is_inside_e2e is an equality-or-
    # ancestor test and not a substring test, and the two agree on every path this repository holds
    # today. A tests/e2e_old beside tests/e2e, or a tests/e2e.bak, is all it takes for them to
    # disagree -- and a substring check would read either as opted in
    from tests.e2e_selection import E2E_DIR, is_inside_e2e

    assert is_inside_e2e(E2E_DIR)
    assert is_inside_e2e(E2E_DIR / "test_api_e2e.py")
    assert not is_inside_e2e(E2E_DIR.parent)
    assert not is_inside_e2e(Path(str(E2E_DIR) + "_old") / "test_api_e2e.py")
    assert not is_inside_e2e(Path(str(E2E_DIR) + ".bak"))


def test_a_path_passed_to_pytest_main_rather_than_on_the_command_line_counts_as_typed(
    repo_root, tmp_path
):
    # the guard reads config.invocation_params.args, which is the list pytest was CALLED with,
    # rather than sys.argv[1:]: the two are identical for `python -m pytest ...` and nothing else,
    # and every form in this file is that one. Here they differ -- the interpreter was given -c and
    # the path was handed to pytest.main -- and the path was typed by whoever called pytest
    record = tmp_path / "main_record.json"
    program = (
        "import sys, pytest;"
        " sys.exit(pytest.main(['--collect-only', '-q', '-p', 'no:cacheprovider', 'tests/e2e']))"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    env.update(DEAD_ENV)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                p for p in (str(tmp_path), env.get("PYTHONPATH")) if p
            ),
            "PYTEST_PLUGINS": "e2e_guard_recorder",
            "E2E_GUARD_RECORD": str(record),
        }
    )
    (tmp_path / "e2e_guard_recorder.py").write_text(_RECORDER)
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = (result.stdout + result.stderr)[-4000:]
    assert result.returncode == 0, output
    payload = json.loads(record.read_text())
    assert payload["items"], output
    e2e_dir = repo_root / "tests" / "e2e"
    assert all(_inside(key, e2e_dir) for key in payload["items"]), output
    assert payload["deselected"] == [], output


def test_naming_tests_e2e_collects_every_end_to_end_test(forms):
    collection = forms["tests_e2e"]
    assert collection.returncode == 0, collection.output
    assert collection.items == forms["e2e_all"], collection.output


def test_one_node_id_collects_that_test_alone(forms):
    collection = forms["node_alone"]
    assert collection.returncode == 0, collection.output
    assert collection.items == {forms["node_key"]}, collection.output


def test_one_node_id_beside_tests_adds_that_test_and_no_other_end_to_end_test(forms):
    for name in ("tests_and_node", "confcutdir_unit_tests_and_node"):
        collection = forms[name]
        assert collection.returncode == 0, (name, collection.output)
        expected = forms["every_other"].items | {forms["node_key"]}
        assert collection.items == expected, (name, collection.output)
        assert collection.deselected == forms["e2e_all"] - {forms["node_key"]}, (name, collection.output)


def test_a_confcutdir_below_tests_collects_no_end_to_end_test(forms):
    # --confcutdir below tests/ skips tests/conftest.py and loads tests/e2e/conftest.py, which
    # has to refuse every test it holds on its own
    for name in (
        "confcutdir_unit",
        "confcutdir_integration",
        "confcutdir_e2e",
        "confcutdir_e2e_as_a_value",
        "confcutdir_unit_tests_and_e2e_conftest",
    ):
        collection = forms[name]
        assert collection.returncode == 0, (name, collection.output)
        assert collection.items == forms["every_other"].items, (name, collection.output)
        assert collection.deselected == forms["e2e_all"], (name, collection.output)


def test_a_path_in_pytest_addopts_does_not_count_as_typed(forms, repo_root):
    # a stale `export PYTEST_ADDOPTS=tests/e2e` must not unlock every later run in that shell
    bare = forms["addopts_bare"]
    assert bare.returncode == 5, bare.output
    assert bare.items == set(), bare.output
    beside_unit = forms["addopts_beside_unit"]
    assert beside_unit.returncode == 0, beside_unit.output
    unit = {key for key in forms["every_other"].items if _inside(key, repo_root / "tests" / "unit")}
    assert unit and beside_unit.items == unit, beside_unit.output


def test_an_option_value_naming_an_end_to_end_test_does_not_count_as_typed(forms):
    collection = forms["deselect_option_naming_node"]
    assert collection.returncode == 0, collection.output
    assert collection.items == forms["every_other"].items, collection.output
