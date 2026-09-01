[![tests](https://github.com/pauloshabtewold/market-data-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/pauloshabtewold/market-data-platform/actions/workflows/ci.yml)

# Market Data Platform

Minute-bar ingestion into a month-partitioned PostgreSQL database. 100 large-cap US equities
over 2020-08-01 to 2026-06-30, from Alpaca's IEX feed.

## Status

The ingest pipeline and the database layer are built and tested. **The read API is not** —
`api/` is a directory of empty files, so there is no HTTP surface yet.

Loaded: **41,668,537 bars** across the full universe, in **42.4 minutes**. That is measured
on the loaded data, not projected onto it.

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker compose up -d --wait db
.venv/bin/python -m db.migrate            # separate operator action; ingest never migrates
.venv/bin/python -m ingest --tickers-file tickers.txt
.venv/bin/python -m pytest
```

Copy `.env.example` to `.env` first. The four measured constants in it are recorded rather
than defaulted, because a silently-defaulted value is the failure they exist to prevent.

## What's interesting here

**Ingestion resumes after a hard kill.** Progress is recorded per `(symbol, month)` unit in
the same transaction as the bars it covers, so a `kill -9` mid-run loses no committed work and
re-running skips what already landed. `tests/integration/test_crash_resume.py` kills the
process and asserts it.

**A token-bucket limiter holds the request rate under its own cap.** The load peaked at 185
requests a minute against a 200/minute client-side limit — the cap is ours, not a published
vendor ceiling — with the clock injected so the tests don't sleep.

**Missing minutes stay missing.** A gap is never synthesized or forward-filled, so gaps in
`bars` are gaps in the tape as this feed saw it. That matters more than it sounds: IEX
coverage pools to **72.16%** of regular-session minutes across the full universe, and
individual symbol-months run as low as **3.02%**. A well-covered month reads **85.04%** over the
universe and 99.32% over the five sample tickers — an upper bound, not a rate.

**The ten analytical queries are gated on blocks read, not on a stopwatch.** Blocks touched are
a property of the plan — the same number on a warm cache, a cold one, or RDS — while wall-clock
is mostly a measure of what the page cache happened to hold. The one query with a selective
filter reads **30.27× fewer blocks** after tuning while the clock moves only 7×, and the six
that must read every row are held to evidence of optimality instead: the parallel plan the
planner should pick and did, a candidate index built and shown not to be chosen, the row math,
and — for three of the six — a byte ratio used as a prediction that had to survive being
tested.

**There is no incremental ingest.** A run re-walks every requested unit and skips what is
already recorded. Widening the window and re-running is the supported path.

## Layout

```
ingest/     feed client, retry/throttle, calendar, symbol resolution, validation, pipeline
db/         schema, migrations, session
api/        empty — the read API is not built yet
tests/      unit and integration suites, run in CI
```

[Query performance](docs/QUERY_PERFORMANCE.md) has the before/after plans for all ten queries,
the index decisions including two negative results, and the measurement conditions every number
depends on. [Methodology](docs/METHODOLOGY.md) covers sizing, partitioning, provenance and
survivorship, including two earlier projections kept rather than deleted so the size of each
error stays visible. [INGEST_LOG.md](INGEST_LOG.md) is the run-by-run record.
