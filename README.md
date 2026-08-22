[![tests](https://github.com/pauloshabtewold/market-data-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/pauloshabtewold/market-data-platform/actions/workflows/ci.yml)

# Market Data Platform

Minute-bar ingestion, analytical queries, and a paginated read API over a month-partitioned
PostgreSQL database. The target universe is 100 large-cap US equities over 2020-08-01 to
2026-06-30, sourced from Alpaca's IEX feed; the measurements below come from a 5-ticker,
one-month sample loaded to size the pipeline before the full run.

## Setup

Install the package and its development dependencies:

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Start the database:

```
docker compose up -d --wait db
```

Apply the migrations. This is a separate operator action and the ingest never performs it —
later features add index migrations against a populated database, and auto-migrating on every
invocation would open a long run by building an index over tens of millions of rows:

```
.venv/bin/python -m db.migrate
```

Load the calendar, seed `symbols` and ingest bars:

```
.venv/bin/python -m ingest --tickers-file tickers.txt
```

The run reads `.env` for credentials and the window; `.env.example` lists every key. Repeated
`--symbol` flags and `--start-month` / `--end-month` narrow the bars phase only — the calendar
and `symbols` phases always cover the whole file, which is what lets `count(*) FROM symbols` be
checked against a file the ingest did not write.

## Measured values

All three come from one sample: the first five tickers of `tickers.txt` (AAPL, MSFT, NVDA, AVGO,
ORCL) over 2026-06, the most recent fully-completed month inside the window. They are recorded in
`.env` rather than defaulted, because a silently-defaulted value is the failure they exist to
prevent.

`N` below is the committed line count of `tickers.txt`, currently **100**, and not a literal — the
universe may be cut, and a stale `N` overstates reachable page depth.

| Key | Value | Arithmetic |
| --- | --- | --- |
| `BARS_PER_TICKER_DAY` | 387.36 | 40,673 regular-session bars ÷ (5 tickers × 21 trading days) |
| `DEEP_PAGE_DEPTH` | 1,000,000 | `min(1e6, floor(0.8 × N × (40,673 ÷ 105) × TD))`, `TD` = 58, unclamped 1,797,359 |
| `HEAP_INDEX_BYTE_RATIO` | 3.1656441717791411 | 4,227,072 heap bytes ÷ 1,335,296 primary-key index bytes |

`TD` is the **minimum** trading-day count over every 90-calendar-day window in the loaded
calendar, read from `market_days` rather than assumed. It is a minimum and not a sample because
the count swings 58–64 across this history, so an arbitrary start date makes the derived value a
coin flip and two builders following the same instruction write configs 10% apart.

`DEEP_PAGE_DEPTH` is derived from the unrounded 40,673 ÷ 105 rather than from the 387.36 in the
row above — recomputing with the rounded figure gives 1,797,350, nine short. The rounded value is
what `.env` carries, since that is the number config consumes; the depth is taken before the
rounding.

`HEAP_INDEX_BYTE_RATIO` is the yardstick for the two coverage queries that scan the primary key,
and for nothing else. Two caveats travel with it. It is a first approximation measured on a
5-ticker, one-month partition and is re-checked against a full partition later. And it is driven
by B-tree leaf fill, so it is comparable only across partitions written in primary-key order —
which this pipeline produces by construction, since a unit's rows all belong to one partition and
arrive in ascending `ts`, and which a hand-built fixture does not. What diagnoses a badly loaded
heap is the index side rather than the ratio — a primary-key index materially larger than the
sorted-arrival size for its row count means the keys did not arrive sorted — so the number below
is recorded as measured rather than checked against a target.

## Sizing

| Quantity | Sample (5 tickers × 1 month) | Projected (100 × 71) |
| --- | --- | --- |
| Bars | 41,723 | 59,246,660 |
| Regular-session bars | 40,673 | — |
| Extended-hours share | 2.52% | — |
| Total relation bytes | 5,603,328 | 7,956,725,760 (≈7.4 GiB) |

Extended-hours bars are real on this feed rather than absent, which is why the share is measured
and reported rather than assumed to be zero.

Wall-clock rate: 5 units in **2.9–4.8 s** over two runs, each from a cleared `ingest_progress`,
extrapolating to **≈69–114 minutes** for the full 7,100 units. Clearing the progress rows is what
makes the second run fetch at all — the identical command left unchanged skips every completed
unit and issues no bars requests. It is published as a range because that is what was observed —
the spread is network latency between runs rather than anything in the pipeline, and it is the
only figure on this page that a re-run does not land on exactly. The rate-limit floor — 7,100
requests at 200 requests per minute — is ≈36 minutes, so the wall clock binds rather than the
throttle, and the top of the range is the one to plan against.

## Feed

The stored data is **IEX minute bars**. SIP was available on this account rather than locked; IEX
is a deliberate choice, because its gaps make the coverage queries real data-quality work instead
of a formality.

The feed comparison, AAPL over 2026-06 at `limit=10000`:

| Feed | Bars | Pages |
| --- | --- | --- |
| `iex` | 8,547 | 1 |
| `sip` | 17,904 | 2 |

**These two counts are not a coverage ratio and 8,547/17,904 must not be read as one.** SIP carries
the full 04:00–20:00 extended session and its first bar of the day is 08:00Z, so the whole-month
figures compare different windows. The comparable number is regular-session coverage measured
against the calendar. All 21 sessions in 2026-06 are full 390-minute days, so the denominator is
8,190 minutes per symbol:

| Symbol | Regular-session bars | Coverage |
| --- | --- | --- |
| `AAPL` | 8,190 | 100.00% |
| `MSFT` | 8,190 | 100.00% |
| `NVDA` | 8,190 | 100.00% |
| `AVGO` | 8,181 | 99.89% |
| `ORCL` | 7,922 | 96.73% |

The mean is 40,673 ÷ 40,950 = **99.32%**, which is `BARS_PER_TICKER_DAY` ÷ 390 and is the same
number arrived at from the other direction. An earlier reading on June 2022 and June 2025 gave
384–389 minutes of 390 against SIP's 391; coverage is not constant across this history, so the
figure is reported per period rather than as one rate, and the full-window number replaces both
once the universe is loaded.

The finding this project reports is *where* the missing minutes fall rather than a headline gap,
and on this sample they are concentrated rather than spread: 277 minutes are missing in total and
**268 of them are ORCL's**, with three of the five symbols complete. Queries 9 and 10 extend that
breakdown to the full universe and the full window.

## Partition methodology

`bars` is `RANGE` partitioned by month on `ts`. Children are runtime artifacts: `db/schema.sql`
declares the parent state only, and the pipeline creates each month's child on first use.

Creation is idempotent through a three-way probe on `pg_class.relispartition`:

```sql
SELECT relispartition FROM pg_class WHERE oid = to_regclass('public.bars_2026_06')
```

- **zero rows** — the table does not exist. `to_regclass` yields `NULL` for a missing relation, so
  the predicate matches no row at all. This is the create-and-attach branch, and the distinction
  matters: a handler written against a `NULL` *value* never fires, so the first unit of every fresh
  run would fall through to the attach-only branch and raise `42P01`.
- **`false`** — the table exists but is not attached. Attach only. Note what does *not* produce
  this state: both statements run in one transaction and PostgreSQL DDL is transactional, so a
  crash between them rolls the `CREATE` back too and the next run sees zero rows, not `false`. It
  is reachable by an out-of-band `DETACH`, by a restore, or by any future path that commits
  between the two — the branch is cheap and the state is real, but the crash story is not why.
- **`true`** — already attached. Skip.

Creation is `CREATE TABLE ... (LIKE bars INCLUDING ALL)` followed by `ALTER TABLE ... ATTACH
PARTITION`, never `CREATE TABLE ... PARTITION OF`: the one-statement form takes an
`AccessExclusiveLock` on the parent and blocks every reader, where `(LIKE)` plus `ATTACH` takes
only a `ShareUpdateExclusiveLock`. The partition step, the bar insert and the progress write share
one transaction per unit, so the DDL has to sit inside it.

`42P17` — a genuine wrong-bounds overlap — kills the run by construction, because this path
contains no exception handler to catch anything. That is deliberate. The alternative was an
allowlist of `42P07` (table exists) and `42809` (already a partition) around unguarded DDL, and
the three-way probe makes it unnecessary: it needs no SQLSTATE table to be correct and it already
distinguishes the crash-in-between state. The allowlist is recorded here as the rejected design
rather than as the mechanism, and it is an allowlist of two rather than a denylist because the set
of things that can go wrong on `ATTACH` is open. Catching psycopg's `InvalidObjectDefinition`
instead would have been worse than either: that name *is* `42P17`, so it would swallow the real
overlap while leaving "already a partition" uncaught and the run dead on the second unit.

## Limitations

- **IEX coverage is not complete.** On the 2026-06 sample it runs 96.73–100.00% of regular-session
  minutes by symbol, averaging 99.32%; an earlier reading on June 2022 and June 2025 gave
  98.46–99.74% against the same 390-minute denominator.
  A missing minute is data and is never synthesized or forward-filled, so gaps in `bars` are gaps
  in the tape as this feed saw it.
- **There is no incremental ingest.** A run re-walks every requested unit and skips the ones
  already recorded in `ingest_progress`; there is no watermark that extends the window forward.
  Widening `INGEST_START` / `INGEST_END` and re-running is the supported path.
