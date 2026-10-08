import re
from pathlib import Path

QUERIES_DIR = Path(__file__).resolve().parent / "queries"

# one placeholder definition: the integration fixture imports it, so guard and code agree on a name
PLACEHOLDER = re.compile(r":'([a-z_]+)'")


def render(name: str) -> str:
    text = (QUERIES_DIR / name).read_text()
    # raise, not assert: python -O deletes an assert, and PYTHONOPTIMIZE=1 is routine in Feature 8's
    # Dockerfile -- an empty file renders "" and fails later naming neither file nor cause
    if not text.strip():
        raise ValueError(f"{name}: file is empty")
    # percents double BEFORE the rewrite: psycopg reads a bare % as a placeholder, and doubling
    # after would turn %(name)s into an escaped literal binding nothing
    rendered = PLACEHOLDER.sub(r"%(\1)s", text.replace("%", "%%"))
    # an uppercase or digit-bearing name escapes the pattern, reaching psycopg as a syntax error
    # naming neither file nor cause
    if ":'" in rendered:
        raise ValueError(f"{name}: a psql placeholder survived the render")
    return rendered
