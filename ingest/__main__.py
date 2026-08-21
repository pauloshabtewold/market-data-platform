import argparse
import logging
import sys
from pathlib import Path

from config import settings
from db.session import connect
from ingest.calendar import load_calendar
from ingest.client import AlpacaClient


def read_tickers(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest")
    parser.add_argument("--tickers-file", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # httpx logs every request at INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        tickers = read_tickers(args.tickers_file)
    except OSError as exc:
        # the universe is the owner's input
        print(f"{args.tickers_file}: {exc.strerror}", file=sys.stderr)
        return 2
    if not tickers:
        print(f"{args.tickers_file}: no tickers", file=sys.stderr)
        return 2

    with AlpacaClient() as client, connect(settings.DATABASE_URL) as conn:
        summary = load_calendar(conn, client)
    print(f"calendar: {summary.days} days loaded {summary.first}..{summary.last}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
