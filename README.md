[![tests](https://github.com/pauloshabtewold/market-data-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/pauloshabtewold/market-data-platform/actions/workflows/ci.yml)

# Market Data Platform

Minute-bar ingestion, analytical queries, and a paginated read API over a month-partitioned
PostgreSQL database. The target universe is 100 large-cap US equities over 2020-08-01 to
2026-06-30, sourced from Alpaca's IEX feed. The sizing and byte measurements below come from a
5-ticker, one-month sample loaded before the full run; the coverage figures also draw on the first
50 tickers, loaded over the whole window.

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

The run reports on stderr, so an unattended launch needs both streams: `python -m ingest
--tickers-file tickers.txt > run.log 2>&1 &`. Redirecting stdout alone records nothing.

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
| Bars | 41,723 | 44,871,360 |
| Regular-session bars | 40,673 | — |
| Extended-hours share | 2.52% | — |
| Total relation bytes | 5,603,328 | ≈5,704 MB (≈5.6 GiB) |

The projection doubles the fifty-ticker half that is loaded — `22,435,680 × 2` and
`2,852 MB × 2` — rather than scaling the five-ticker sample. An earlier version of this page projected 59,246,660
bars and ≈7.4 GiB by multiplying the sample column by 1,420, which ran **32% high**: the sample
month carries 8,344.6 bars per ticker-month against the loaded half's 6,319.9, because June 2026
sits near this feed's coverage ceiling and the whole window does not. Doubling carries an
assumption of its own — the second fifty are different symbols, selected by the rule
`INGEST_LOG.md` records — so this stays a projection until Feature 3 loads them and it becomes a
measurement.

Extended-hours bars are real on this feed rather than absent, which is why the share is measured
and reported rather than assumed to be zero.

Wall-clock rate: **0.392 s per ticker-month**, measured over the 3,550 units of the fifty-ticker
load — 1,391.7 s, including a deliberate kill and restart. The full 7,100 units extrapolate to
**≈46 minutes**: `7100 × 0.392 / 60 = 46.4`.

An earlier version of this page published ≈114 minutes, from timing the five-unit sample at 4.8 s
and computing `7100 / 5 × 4.8 / 60`. Five units pay once for the first partition DDL and the first
connection, and dividing by five leaves that one-off cost inside every extrapolated unit, which is
why it landed 2.5× high. The sample is what sized the pipeline; it was never what timed it.

The second run of the same five units took 2.9 s and **inserted nothing**: every row met
`ON CONFLICT (symbol, ts) DO NOTHING` and the partition already existed, so it measures the
idempotency path and is reported under it in `INGEST_LOG.md` rather than here. Extrapolating it
would publish a rate no load of new data can reach.

The figure still moves with the network and with what else the host is doing. The rate-limit floor
— 7,100 requests at 200 requests per minute — is ≈36 minutes, so the wall clock binds rather than
the throttle, though no longer by much.

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
number arrived at from the other direction — and it is near this feed's ceiling rather than
typical of it. Coverage is not constant across this history, so it is reported per period.
Measured over the 50 tickers loaded so far, on the same `[open_ts, close_ts)` membership and the
same `SUM(session_minutes)` denominator: June 2026 pools to **92.37%** over a 53.54–100.00%
per-symbol range — seven points below what the five sample tickers read for the same month, which
is the clearest measure of how unrepresentative they are — June 2022 to **81.01%** over
13.25–99.90%, and June 2025 to **74.77%** over 7.59–99.88%. Across the whole window those 50
symbols pool to **77.62%**. The hundred-symbol figure replaces that one once the second half is
loaded.

The finding this project reports is *where* the missing minutes fall rather than a headline gap,
and on this sample they are concentrated rather than spread: 277 minutes are missing in total and
**268 of them are ORCL's**, with three of the five symbols complete. `db/queries/09_coverage.sql`
extends that breakdown to every ingested symbol across the whole window; the per-minute query that
would locate the missing minutes themselves is Feature 4's and is not in this repository yet.

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

- **IEX coverage is not complete, and it varies by symbol and by period far more than any one
  month shows.** On the 2026-06 sample it runs 96.73–100.00% of regular-session minutes by symbol,
  averaging 99.32% — but that month is close to the ceiling. Over the 50 tickers loaded across the
  whole window it pools to **77.62%**, and individual symbol-months run as low as **7.59%**. Read
  the single-month figure as an upper bound, not as a rate.
  A missing minute is data and is never synthesized or forward-filled, so gaps in `bars` are gaps
  in the tape as this feed saw it.
- **There is no incremental ingest.** A run re-walks every requested unit and skips the ones
  already recorded in `ingest_progress`; there is no watermark that extends the window forward.
  Widening `INGEST_START` / `INGEST_END` and re-running is the supported path.
