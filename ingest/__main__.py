import argparse
import logging
import sys
from datetime import date, datetime
from functools import partial
from pathlib import Path

from config import settings
from db.session import connect
from ingest.calendar import load_calendar
from ingest.client import AlpacaClient, fetch_bars
from ingest.pipeline import run
from ingest.symbols import seed_symbols


def read_tickers(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def month_arg(value: str) -> date:
    return datetime.strptime(value, "%Y-%m").date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest")
    parser.add_argument("--tickers-file", required=True, type=Path)
    # repeated rather than a comma list so the shell commands that drive this stay free of commas.
    parser.add_argument("--symbol", action="append", dest="symbols", metavar="TICKER")
    parser.add_argument("--start-month", type=month_arg, metavar="YYYY-MM")
    parser.add_argument("--end-month", type=month_arg, metavar="YYYY-MM")
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

    unknown = sorted(set(args.symbols or []) - set(tickers))
    if unknown:
        print(f"{args.tickers_file}: does not carry {' '.join(unknown)}", file=sys.stderr)
        return 2

    start = args.start_month or settings.INGEST_START.replace(day=1)
    end = args.end_month or settings.INGEST_END

    with AlpacaClient() as client, connect(settings.DATABASE_URL) as conn:
        calendar = load_calendar(conn, client)
        print(f"calendar: {calendar.days} days loaded {calendar.first}..{calendar.last}")

        # seeding covers every line of the file however --symbol narrows the bars phase, so symbols stays checkable against a file this project never wrote.
        seeded = seed_symbols(conn, client, tickers)
        print(
            f"symbols: upserted={seeded.upserted} deleted={len(seeded.deleted)} "
            f"refused={len(seeded.refused)} inactive={len(seeded.inactive)}"
        )

        summary = run(conn, args.symbols or tickers, start, end, partial(fetch_bars, client))
        counts = client.request_counts

    print(
        f"run complete: units={summary.units} skipped={summary.skipped} "
        f"calendar_requests={counts['calendar']} symbols_requests={counts['symbols']} "
        f"bars_requests={counts['bars']} rows={summary.rows} "
        f"elapsed={summary.elapsed:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
