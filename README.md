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

| Quantity | Sample (5 tickers × 1 month) | Measured (100 × 71) |
| --- | --- | --- |
| Bars | 41,723 | 41,668,537 |
| Regular-session bars | 40,673 | 41,605,369 |
| Extended-hours share | 2.52% | 0.15% |
| Total relation bytes | 5,603,328 | 5,547,909,120 (5,291 MB) |

The right-hand column is measured on the loaded universe, not projected onto it. Two earlier
projections stood here and both are recorded rather than deleted, because the size of the error is
the useful part. The first multiplied the five-ticker sample by 1,420 and gave 59,246,660 bars and
≈7.4 GiB — **42% above** what the universe actually holds, because the sample month sits near this
feed's coverage ceiling at 8,344.6 bars per ticker-month where the full window averages 5,868.8.
The second doubled the fifty-ticker half to 44,871,360 bars and ≈5,704 MB, which landed within
**7.7%** on rows and **7.8%** on bytes — the second fifty are slightly thinner than the first, so
doubling the better-covered half ran a little high. A projection from a representative half beat one
from an unrepresentative month by a factor of five.

Extended-hours bars are real on this feed rather than absent, which is why the share is measured
and reported rather than assumed to be zero.

Wall-clock rate: **0.358 s per ticker-month**, measured over all 7,100 units of the full universe —
2,544.6 s in two halves, 1,391.7 s for the first fifty (including a deliberate kill and restart) and
1,152.9 s for the second. The whole load is **42.4 minutes**, no longer an extrapolation. The
earlier `7100 × 0.392 / 60 = 46.4` came from the first half alone, whose rate carries the restart;
the second half ran at 0.325 s per unit.

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
Measured over all 100 tickers, on the same `[open_ts, close_ts)` membership and the
same `SUM(session_minutes)` denominator: June 2026 pools to **85.04%** over a 43.94–100.00%
per-symbol range — fourteen points below what the five sample tickers read for the same month,
which is the clearest measure of how unrepresentative they are — June 2022 to **77.04%** over
13.25–99.90%, and June 2025 to **67.49%** over 7.59–99.88%. Across the whole window all 100
symbols pool to **72.16%**, replacing the 77.62% this page carried while only the first half was
loaded. The extremes of the 2022 and 2025 ranges are unchanged because both belong to symbols in
the first fifty; it is the pooled figures that moved.

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

## Data provenance

Every bar in this database came from one vendor over one window under one set of parameters, and
the table below is the whole of it. Nothing is synthesized, back-filled, or carried over from
another source.

| | |
| --- | --- |
| Vendor | Alpaca Market Data v2, `/v2/stocks/bars` |
| Feed | `iex` — one exchange's prints, not the consolidated tape |
| Adjustment | `split,spin-off` — prices adjusted for splits and spin-offs, not for dividends |
| Window | 2020-08-01 .. 2026-06-30, fixed and closed |
| Granularity | 1-minute bars, `limit=10000`, one page per ticker-month |
| Universe | 100 tickers, the committed `tickers.txt` |
| Loaded | 41,668,537 bars over 7,100 units, in two runs on 2026-08-26 and 2026-08-27 |
| Rejected | 26 bars, by the validation rules, across the whole load |
| Timestamps | UTC throughout; session bounds come from `market_days`, loaded from Alpaca's calendar |

`INGEST_LOG.md` carries the per-run detail: what each run requested, what came back, how long it
took, plus the feed comparison and the five corporate-action probes that establish what `iex` and
`split,spin-off` actually mean for this data.

## Survivorship

**The universe is the 100 companies that were large-cap in 2026, and it was selected then.** That
is a survivorship-biased sample and every cross-sectional result on it inherits the bias. A company
that was large-cap in 2020 and had left the index by 2026 is absent, so any backward-looking study
of this data sees only the firms that made it to the selection date. Measured returns are biased
upward and measured failure rates downward, by an amount this data cannot itself estimate.

Two narrower consequences are worth naming because they are easy to miss:

- **Delistings are invisible rather than sparse.** A delisted symbol is not a symbol with a short
  history here; it is a symbol that never appears. The gap it would have left is not observable.
- **A second filter removed companies renamed inside the window, and that filter cannot be
  audited.** The selection rule excludes anything renamed between 2020-08 and 2026-06, because such
  a series changes meaning mid-window. The vendor remaps renamed symbols, so a rename returns a
  *complete* series under the new ticker and **no completeness check in this repository can detect
  one** — which means the exclusion rests entirely on the recall of whoever applied it.
  `INGEST_LOG.md` records this in full, including the three names it was tested against and the
  fourth that was mistaken for a confirmation. If the recall missed a name, that name is in the
  universe now, indistinguishable from any other.

Both filters cut in the same direction: they remove discontinuity. What is left is a continuously
listed, continuously named large-cap universe, which is a cleaner dataset and a narrower claim. The
fix is a point-in-time constituent list, which this project does not have. The honest statement is
that this is a study of 100 companies that were large-cap in August 2026, over 2020–2026 — not a
study of the market over 2020–2026.

## Limitations

- **IEX coverage is not complete, and it varies by symbol and by period far more than any one
  month shows.** On the 2026-06 sample it runs 96.73–100.00% of regular-session minutes by symbol,
  averaging 99.32% — but that month is close to the ceiling. Over all 100 tickers across the
  whole window it pools to **72.16%**, and individual symbol-months run as low as **7.59%**. Read
  the single-month figure as an upper bound, not as a rate: the five sample tickers overstate the
  universe by 27 points.
  A missing minute is data and is never synthesized or forward-filled, so gaps in `bars` are gaps
  in the tape as this feed saw it.
- **There is no incremental ingest.** A run re-walks every requested unit and skips the ones
  already recorded in `ingest_progress`; there is no watermark that extends the window forward.
  Widening `INGEST_START` / `INGEST_END` and re-running is the supported path.
