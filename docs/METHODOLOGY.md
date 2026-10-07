# Methodology

Sizing, feed behaviour, partitioning, provenance and survivorship. Split out of the
README so that file stays short. Every figure stated here as a result is measured; the
projections and the derived bounds are labelled as such where they appear, and the
projections that measurement has overtaken are kept rather than deleted so the size of
each error stays visible.

## Sizing

| Quantity | Sample (5 tickers × 1 month) | Measured (100 × 71) |
| --- | --- | --- |
| Bars | 41,723 | 41,668,537 |
| Regular-session bars | 40,673 | 41,605,369 |
| Extended-hours share | 2.52% | 0.15% |
| Total relation bytes | 5,603,328 | 5,547,909,120 (5,291 MB), 2026-08-27 |

The right-hand column is measured on the loaded universe, not projected onto it. The byte row is
dated because it is `pg_total_relation_size` and therefore counts indexes: migration 005's
`bars_ts_symbol_idx` and the four hot-window indexes landed after it, and the same sum reads
6,998,122,496 (6,674 MB) today, of which 4,211,064,832 (4,016 MB) is heap. Those two additions
account for the whole difference, to the byte: 5,547,909,120 + 1,315,995,648 for
`bars_ts_symbol_idx` summed over the 71 children + 134,217,728 for the four hot-window indexes
= 6,998,122,496. So the two readings differ by exactly those indexes and by nothing in the data,
which is what the date on the row is for. The projections below are compared against the
2026-08-27 figure, which is the like-for-like one. Two earlier
projections stood here and both are recorded rather than deleted, because the size of the error is
the useful part. The first multiplied the five-ticker sample by 1,420 and gave 59,246,660 bars and
≈7.4 GiB — **42% above** what the universe actually holds, because the sample month sits near this
feed's coverage ceiling at 8,344.6 bars per ticker-month where the full window averages 5,868.8.
The second doubled the fifty-ticker half to 44,871,360 bars and ≈5,704 MB, which landed within
**7.7%** on rows and **7.8%** on bytes — the second fifty are slightly thinner than the first, so
doubling the better-covered half ran a little high. A projection from a representative half beat one
from an unrepresentative month by a factor of five and a half.

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
why it landed **2.7× high** against the measured 42.4 minutes. The sample is what sized the
pipeline; it was never what timed it.

The second run of the same five units took 2.9 s and **inserted nothing**: every row met
`ON CONFLICT (symbol, ts) DO NOTHING` and the partition already existed, so it measures the
idempotency path and is reported under it in `INGEST_LOG.md` rather than here. Extrapolating it
would publish a rate no load of new data can reach.

The figure still moves with the network and with what else the host is doing. The rate-limit floor
— 7,100 requests at 200 requests per minute — is ≈36 minutes. That one is a bound computed from
the configured limit rather than a timed run, which is enough for the comparison it is used for:
the wall clock binds rather than the throttle, though no longer by much.

## Measured constants

Four values in `.env` are measured rather than defaulted, because a silently-defaulted one is the
failure they exist to prevent. Values and arithmetic are here; the two ratios' full derivation,
with the plans behind them, is in [Query performance](QUERY_PERFORMANCE.md).

`N` below is the committed line count of `tickers.txt`, currently **100**, and not a literal — the
universe may be cut, and a stale `N` overstates reachable page depth.

| Key | Value | Arithmetic |
| --- | --- | --- |
| `BARS_PER_TICKER_DAY` | 387.36 | 40,673 regular-session bars ÷ (5 tickers × 21 trading days) |
| `DEEP_PAGE_DEPTH` | 1,000,000 | `min(1e6, floor(0.8 × N × (40,673 ÷ 105) × TD))`, `TD` = 58, unclamped 1,797,359 |
| `HEAP_INDEX_BYTE_RATIO` | 3.1656441717791411 | 4,227,072 heap bytes ÷ 1,335,296 primary-key index bytes |
| `HEAP_INDEX_COVERING_RATIO` | 1.8924 | 4,211,064,832 heap bytes ÷ 2,225,233,920 covering-index bytes — the divisor measures an index that no longer exists, see below |

The Value column above uses thousands separators for readability; both `.env` and `.env.example`
take plain digits, so wherever the key is given a real value it reads `DEEP_PAGE_DEPTH=1000000` —
`DEEP_PAGE_DEPTH=1,000,000` fails config's integer parser. `.env` is never committed and carries
that real value once measured. `.env.example`, which is committed and every reader of this
repository can check, carries the key empty on purpose, along with the other three measured
constants: they are recorded in this document rather than defaulted or checked in with a real
number. An empty value reads as unset for these four keys and for no others, so a copy of
`.env.example` constructs as it stands while every other key refuses or reports an empty value
under its own name rather than falling through to a default.

`TD` is the **minimum** trading-day count over every 90-calendar-day window in the loaded calendar,
read from `market_days` rather than assumed. It is a minimum and not a sample because the count
swings **58–64** across this history, so an arbitrary start date makes the derived value a coin
flip and two builders following the same instruction write configs 10% apart.

`DEEP_PAGE_DEPTH` is derived from the unrounded 40,673 ÷ 105 rather than from the 387.36 in the row
above — recomputing with the rounded figure gives 1,797,350, nine short. The rounded value is what
`.env` carries, since that is the number config consumes; the depth is taken before the rounding.

The `0.8` is headroom rather than a measured quantity. Coverage varies by symbol and by year — this
page reports a per-symbol spread of 22.28% to 98.99% below — so the rows a deep cursor can actually
reach fall short of what one flat per-ticker-day rate projects over `N` tickers and `TD` days, and
the factor keeps the target depth inside what the data holds. The `min` is the other half of the
same bound and holds the number down if the feed turns out dense. Here the clamp is what binds:
without it the depth would be 1,797,359.

`HEAP_INDEX_BYTE_RATIO` is the yardstick for the two coverage queries that scan the primary key,
and for nothing else. It was measured on a 5-ticker, one-month partition and has since been
re-checked against that same partition 16.8× fuller — **3.1654**, 0.008% from the published figure —
and against all 71 pooled, **3.1584**. It is driven by B-tree leaf fill, so it is comparable only
across partitions written in primary-key order, which this pipeline produces by construction: a
unit's rows all belong to one partition and arrive in ascending `ts`. A hand-built fixture does not
produce that order, and neither does a restore.

`HEAP_INDEX_COVERING_RATIO` is a different index and a different number. Query 7's covering index
carries four of nine columns, close to a second heap, so it is worth 1.89× where the primary key is
worth 3.16×. The comparison that holds is between the two whole-table figures — the pooled
**3.1584** from the paragraph above and this key's **1.8924** — because both divide the same
numerator, `bars`'s heap summed over all 71 children, by one index measured over that same table,
and they are **67% apart**. The single-partition figure in the table is the wrong partner for it:
that numerator is one month's heap, so setting it against 1.8924 compares one partition with 71.
The covering figure is measured only inside the runs that build that index for a negative result and
drop it again.

**Only one side of that division can be checked against this database, and it is worth knowing
which.** The numerator, 4,211,064,832, is the live heap of `bars` summed over its 71 children and
re-reads from the catalog at any time. The divisor is the size of an index that exists nowhere: it
was built on the full table, measured, and dropped once the negative result was recorded. No
migration and no line of `db/schema.sql` declares it — between them they put exactly two indexes on
`bars`, the `(symbol, ts)` primary key and `bars_ts_symbol_idx`, and neither is this one — and its
shape, `(symbol, ts) INCLUDE (vwap, volume)`, survives only as a comment in
`db/queries/07_vwap_check.sql`. Reproducing it would mean building that index again over 41.7M
rows, which is a write this figure does not justify. The constant is therefore published with its
divisor as a recorded measurement rather than as a reproducible one, and it is used as a yardstick
for exactly one query's index and for nothing else.

## Recorded rather than reproducible

Three figures across this page and [Query performance](QUERY_PERFORMANCE.md) are records of a
measurement rather than reproductions of one. A **fresh copy** of the data is where each comes
closest to being a measurement again rather than this database, but only one of the three comes
back as the same number — so the procedure below says, for each, what following it actually yields.

- **The covering index's 2,225,233,920 bytes**, the divisor above. On the copy: build
  `(symbol, ts) INCLUDE (vwap, volume)`, read the size out of the catalog, drop it. On this database
  those three steps are a write over 41.7M rows for a number that is already recorded. **The copy
  has to carry the whole universe, and the numerator has to come off that same copy.** A B-tree
  built by `CREATE INDEX` is sorted and bulk-loaded, so its size follows row count and index-tuple
  width rather than heap order — which is why this is the one figure on the list that a full copy
  reproduces closely, and why building it over the four-month hot window instead returns something
  an order of magnitude smaller. Divide this database's heap by that copy's index and the ratio
  reads an order of magnitude high.
- **The hot-window index's 1,012-block "before" row.** Take it on a copy that carries the plain
  `(ts, symbol)` index the children inherit and not the four partial ones. Here all four are live, so
  the state that row describes is not a state this database is in, and reading it would mean dropping
  them first. What a copy yields is a new before/after pair in its own blocks rather than a second
  reading of this one: the harness that produced the row wrote its output and its parameters to a
  path that no longer exists, so there is nothing left to compare like for like against. The durable
  claim is the **ratio**, 1,012 ÷ 16 = **63.25**, which survives a retake because layout moves both
  rows at once.
- **The pre-vacuum 89.7053% all-visible.** Read `relallvisible / relpages` over the children on the
  next copy **before** `VACUUM (ANALYZE)` runs on them. That reading exists only in the window
  between the copy landing and the vacuum, and the vacuum closes it for good. **The procedure
  yields a pre-vacuum reading and never this one**, because the cause is time rather than layout:
  89.7053% samples an autovacuum part-way through 71 freshly-written children some hours after a
  42-minute load, and a four-month copy is swept in well under a minute. `DEPLOY.md` records the one
  real execution of this procedure — read 27 seconds after the copy landed, its four partitions were
  already at 99.42% to 99.94%.

One caveat, and it reaches the block row above and nothing else on this list: a copy reproduces
every row count exactly and its block figures are its own, because block counts follow physical
layout, so a copy written in a different order from the one this pipeline writes answers a slightly
different question — the same reason `HEAP_INDEX_BYTE_RATIO` is comparable only across partitions
written in primary-key order.

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

Pooled, that is 40,673 ÷ 40,950 = **99.32%**, which is `BARS_PER_TICKER_DAY` ÷ 390 and is the same
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

Each of those three ranges is a floor over one month. **The floor over the whole window is
3.02%: BKNG in September 2025, 247 regular-session bars against 8,190 minutes** — 21 full
390-minute sessions, so the denominator is 21 × 390 exactly. That is the minimum over all 7,100
symbol-months, 100 symbols × 71 months, measured on the same `[open_ts, close_ts)` membership and
the same `SUM(session_minutes)` denominator as every figure above, broken down one cell per symbol
per month instead of per symbol. It is the lowest coverage figure this project publishes, and
`README.md` quotes it. **Nineteen symbol-months read below the 7.59% June-2025 figure above,
this one among them, and four read below 5%** — the twentieth-lowest reads exactly 7.59% and is
not below it.

Those extremes belong to one symbol rather than to the universe, which is why a single headline gap
would be the wrong summary. The five lowest cells of the 7,100 are all BKNG, as are thirteen of the
lowest twenty, and all four below 5% are BKNG. Across the whole window BKNG pools to **22.28%** —
128,487 bars against 576,600 minutes, the full 1,484-session calendar — against the universe's
72.16%. None of that makes BKNG an ingest failure, and the distinction is checkable rather than
asserted: it has a complete `first_bar_ts`, it leaves no missing unit, and it returns bars in all
71 of its months — no cell among the 7,100 is zero, which is what the 3.02% minimum establishes.
The thinness is in what this feed printed, not in what the load collected. Why it is this symbol
and not another is not something this data answers, and nothing here guesses at it; the point it
does settle is that a universe-wide figure averages over a per-symbol spread of 22.28% to 98.99%,
which is why coverage is reported per symbol and per period and never as one number.

The finding this project reports is *where* the missing minutes fall rather than a headline gap,
and on this sample they are concentrated rather than spread: 277 minutes are missing in total and
**268 of them are ORCL's**, with three of the five symbols complete. `db/queries/09_coverage.sql`
extends that breakdown to every ingested symbol across the whole window, and
`db/queries/10_missing_minutes.sql` reports the missing-minute count and share per symbol per
month. Neither enumerates the individual missing minutes, and no query here does.

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

