import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path

from config import settings
from db.session import connect
from ingest.calendar import load_calendar
from ingest.client import AlpacaClient, fetch_bars, next_month
from ingest.pipeline import recompute_first_bar_ts, run
from ingest.symbols import seed_symbols

log = logging.getLogger("ingest")


def read_tickers(path: Path) -> list[str]:
    # a whitespace-only line survives as an empty entry rather than being skipped, because `grep -c .` counts it and the gate counts this file that way.
    return [line.strip() for line in path.read_text().splitlines() if line != ""]


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
    # one stream for the whole run: a redirected stdout is block-buffered, so a kill -9 discards every progress line still sitting in it.
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

    if "" in tickers:
        # dropping it silently would put the seeded count one below the line count the gate compares it against
        print(f"{args.tickers_file}: has a line holding only whitespace", file=sys.stderr)
        return 2

    repeated = sorted({t for t in tickers if tickers.count(t) > 1})
    if repeated:
        # the universe is a set, and a repeated line makes the seeded count disagree with the file the gate counts
        print(f"{args.tickers_file}: repeats {' '.join(repeated)}", file=sys.stderr)
        return 2

    unknown = sorted(set(args.symbols or []) - set(tickers))
    if unknown:
        print(f"{args.tickers_file}: does not carry {' '.join(unknown)}", file=sys.stderr)
        return 2

    window_start = settings.INGEST_START.replace(day=1)
    window_end = next_month(settings.INGEST_END) - timedelta(days=1)
    if (settings.INGEST_START, settings.INGEST_END) != (window_start, window_end):
        # the unit of work is a whole month, so a window that starts or ends mid-month requests days outside itself
        print(
            f"INGEST_START/INGEST_END must span whole months, so "
            f"{window_start}..{window_end}, not {settings.INGEST_START}..{settings.INGEST_END}",
            file=sys.stderr,
        )
        return 2

    start = args.start_month or window_start
    end = args.end_month or settings.INGEST_END
    if start > end:
        # an inverted range enumerates no units and would otherwise print a run-complete line that reads like success
        print(f"--start-month {start:%Y-%m} is after --end-month {end:%Y-%m}", file=sys.stderr)
        return 2
    if start < window_start or end > settings.INGEST_END:
        # the window is fixed and closed, and a mistyped year outside it spends the shared budget on months the calendar has no rows for
        print(
            f"--start-month/--end-month must fall inside "
            f"{window_start:%Y-%m}..{settings.INGEST_END:%Y-%m}",
            file=sys.stderr,
        )
        return 2

    # deduped because the progress key is (symbol, month) and a repeated symbol would fail its second insert mid-run
    wanted = list(dict.fromkeys(args.symbols or tickers))

    counts = None
    summary = None
    try:
        with AlpacaClient() as client, connect(settings.DATABASE_URL) as conn:
            counts = client.request_counts
            calendar = load_calendar(conn, client)
            log.info("calendar: %d days loaded %s..%s", calendar.days, calendar.first, calendar.last)

            # seeding covers every line of the file however --symbol narrows the bars phase, so symbols stays checkable against a file this project never wrote.
            seeded = seed_symbols(conn, client, tickers)
            log.info(
                "symbols: upserted=%d deleted=%d refused=%d inactive=%d",
                seeded.upserted, len(seeded.deleted), len(seeded.refused), len(seeded.inactive),
            )

            summary = run(conn, wanted, start, end, partial(fetch_bars, client))
            # inside the same try, so a run that aborts leaves the column to the next run rather than publishing a half-updated one
            log.info("first_bar_ts: %d symbols recomputed", recompute_first_bar_ts(conn))
    finally:
        # request_counts lives only in the client and is the one figure a failed run cannot re-derive from ingest_progress afterwards
        _report(summary, counts, args)
    return 0


def _resume_command(args) -> str:
    # the flags travel with it: a narrowed run resumed by the bare form walks every ticker and every month instead of the ones that were asked for
    parts = ["python -m ingest --tickers-file", str(args.tickers_file)]
    for symbol in dict.fromkeys(args.symbols or []):
        parts += ["--symbol", symbol]
    for flag, month in (("--start-month", args.start_month), ("--end-month", args.end_month)):
        if month is not None:
            parts += [flag, f"{month:%Y-%m}"]
    return " ".join(parts)


def _report(summary, counts, args) -> None:
    counts = counts or dict.fromkeys(("calendar", "symbols", "bars"), 0)
    head = (
        f"run complete: units={summary.units} skipped={summary.skipped}"
        if summary is not None
        else "run incomplete:"
    )
    # appended to the tail rather than inserted into it, so the assertions written against the existing head keep matching what they were written to match
    tail = (
        f" rows={summary.rows} elapsed={summary.elapsed:.1f}s"
        f" rejected={summary.rejected} failed={len(summary.failed)}"
        if summary is not None
        else f"; rerun `{_resume_command(args)}` to resume"
    )
    log.info(
        "%s calendar_requests=%d symbols_requests=%d bars_requests=%d%s",
        head, counts["calendar"], counts["symbols"], counts["bars"], tail,
    )
    if summary is not None and summary.failed:
        # named as they are, because re-running the same command retries exactly those units and nothing else
        log.info(
            "failed units: %s; rerun `%s` to retry them",
            " ".join(f"{symbol} {month:%Y-%m}" for symbol, month in summary.failed),
            _resume_command(args),
        )


if __name__ == "__main__":
    raise SystemExit(main())
