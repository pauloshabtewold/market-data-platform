from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent / "e2e"


def typed_e2e_targets(config: pytest.Config) -> list[tuple[Path, str]]:
    # the positional paths typed on the command line that sit inside tests/e2e, each with the node
    # names after its first "::". Parsed from invocation_params.args alone, because config.args also
    # carries PYTEST_ADDOPTS and ini addopts, which nobody typed for this run, and through pytest's
    # own parser, so an option's value such as `--confcutdir tests/e2e` is never read as a path
    try:
        namespace = config._parser.parse_known_args(list(config.invocation_params.args))
        targets = []
        for raw in namespace.file_or_dir:
            path_part, _, names = str(raw).partition("::")
            resolved = (Path(config.invocation_params.dir) / path_part).resolve()
            if is_inside_e2e(resolved):
                targets.append((resolved, names))
        return targets
    except Exception:
        # refusing every e2e path is the safe reading of arguments this cannot parse
        return []


def is_inside_e2e(path: Path) -> bool:
    resolved = Path(path).resolve()
    return resolved == E2E_DIR or E2E_DIR in resolved.parents


def path_is_typed(collection_path: Path, config: pytest.Config) -> bool:
    # a directory above a typed target stays open: pytest folds `tests/e2e/test_x.py::test_y` into
    # a `tests` typed beside it, so the target is reached only by walking down through its parents
    resolved = Path(collection_path).resolve()
    return any(
        target == resolved or target in resolved.parents or resolved in target.parents
        for target, _ in typed_e2e_targets(config)
    )


def item_is_typed(item: pytest.Item, config: pytest.Config) -> bool:
    # per item, not per directory: `pytest tests tests/e2e/test_x.py::test_y` walks the whole file
    # through `tests`, and only test_y is named
    path = Path(item.path).resolve()
    item_names = item.nodeid.partition("::")[2]
    for target, names in typed_e2e_targets(config):
        if target != path and target not in path.parents:
            continue
        # a bare function name selects every parametrized case of it, as pytest itself reads it
        if not names or item_names == names or item_names.startswith((names + "::", names + "[")):
            return True
    return False


def deselect_untyped_e2e_items(config: pytest.Config, items: list[pytest.Item]) -> None:
    kept, dropped = [], []
    for item in items:
        if is_inside_e2e(item.path) and not item_is_typed(item, config):
            dropped.append(item)
        else:
            kept.append(item)
    config.hook.pytest_deselected(items=dropped)
    items[:] = kept
