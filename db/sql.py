import re
from pathlib import Path

QUERIES_DIR = Path(__file__).resolve().parent / "queries"

# the one object deciding what counts as a placeholder: the integration fixture imports it rather
# than keeping a second copy, so the guard and the code under it cannot disagree about a name
PLACEHOLDER = re.compile(r":'([a-z_]+)'")


def render(name: str) -> str:
    text = (QUERIES_DIR / name).read_text()
    # raise rather than assert: python -O deletes an assert, and Feature 8 ships a Dockerfile where
    # PYTHONOPTIMIZE=1 is routine -- under it an empty file would render to "" and fail several
    # calls later naming neither the file nor the cause
    if not text.strip():
        raise ValueError(f"{name}: file is empty")
    # every literal percent is doubled BEFORE the rewrite, never after: psycopg reads a bare % as
    # its own placeholder, so a "100%" in a comment fails the bind with an error naming neither
    # comments nor the file -- and doubling afterwards would turn %(name)s into %%(name)s, which
    # psycopg reads as an escaped literal and binds nothing at all
    rendered = PLACEHOLDER.sub(r"%(\1)s", text.replace("%", "%%"))
    # an uppercase or digit-bearing name is outside the pattern above, so it survives silently and
    # reaches psycopg as a syntax error naming neither the file nor the cause
    if ":'" in rendered:
        raise ValueError(f"{name}: a psql placeholder survived the render")
    return rendered
