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
Earlier projections, kept for the size of the error: **59,246,660** bars (**42% high**, from the
sample month at this feed's ceiling) and **44,871,360** (**7.7%** high, from doubling the
better-covered half).

Wall-clock rate: **0.358 s per ticker-month**, measured over all 7,100 units of the full universe —
2,544.6 s in two halves, 1,391.7 s for the first fifty (including a deliberate kill and restart) and
1,152.9 s for the second. The whole load is **42.4 minutes**, no longer an extrapolation. The
earlier `7100 × 0.392 / 60 = 46.4` came from the first half alone, whose rate carries the restart;
the second half ran at 0.325 s per unit.

An earlier **~114 minutes**, extrapolated from five units, ran **2.7x high**: five units carry
the first partition's DDL and the first connection.

The rate-limit floor, 7,100 requests at 200/min, is about **36 minutes**, so the wall clock
binds — no longer by much.

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

`.env` takes plain digits — no thousands separators.

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

Three figures here are records of a measurement rather than a procedure that reproduces it.

- The covering ratio **1.8924**: build the index on a full copy, read its size, drop it — and
  take the numerator off that same copy, or the ratio reads an order of magnitude high.
- The hot-window index's **1,012 -> 16 blocks, a ratio of 63.25**: both rows are four-partition
  measurements taken before the index was dropped, and the ratio is exact.
- The visibility map at **89.7053%** before the manual `VACUUM (ANALYZE)`: a pre-vacuum state
  that cannot be reproduced once the vacuum has run. `DEPLOY.md` records the one real execution.

## Feed

The stored data is **IEX minute bars**. SIP was available on this account rather than locked; IEX
is a deliberate choice, because its gaps make the coverage queries real data-quality work instead
of a formality.

`INGEST_LOG.md` carries the IEX-against-SIP comparison. It is not a coverage ratio.
The five sample tickers pool to **99.32%** for 2026-06, near this feed's ceiling and fourteen
points above what all 100 symbols read for the same month — which is why a sample is an upper
bound rather than a rate.
Measured over all 100 tickers, on the same `[open_ts, close_ts)` membership and the
same `SUM(session_minutes)` denominator: June 2026 pools to **85.04%** over a 43.94–100.00%
per-symbol range — fourteen points below what the five sample tickers read for the same month,
which is the clearest measure of how unrepresentative they are — June 2022 to **77.04%** over
13.25–99.90%, and June 2025 to **67.49%** over 7.59–99.88%. Across the whole window all 100
symbols pool to **72.16%**, replacing the 77.62% this page carried while only the first half was
loaded. The extremes of the 2022 and 2025 ranges are unchanged because both belong to symbols in
the first fifty; it is the pooled figures that moved.

The thinnest single cell is **BKNG 2025-09 at 3.02%** — 247 bars against 8,190, which is
21 sessions x 390 minutes.

Those extremes belong largely to one symbol: the five lowest cells and thirteen of the
twenty lowest are BKNG, which pools to **22.28%** against AAPL's 98.99%. **No cell among the
7,100 is zero**, and every symbol has a complete `first_bar_ts`.

Of 277 missing minutes found by the per-minute query, 268 are ORCL's.

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

`42P17` — a real bounds overlap — kills the run, deliberately: this path carries no exception
handler, and the three-way probe needs no SQLSTATE allowlist to be correct.

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

`INGEST_LOG.md` carries the per-run detail.

## Survivorship

**The universe is the 100 companies that were large-cap in 2026, and it was selected then.** That
is a survivorship-biased sample and every cross-sectional result on it inherits the bias. A company
that was large-cap in 2020 and had left the index by 2026 is absent, so any backward-looking study
of this data sees only the firms that made it to the selection date. Measured returns are biased
upward and measured failure rates downward, by an amount this data cannot itself estimate.

Two narrower consequences follow. Delistings are invisible rather than sparse: a company
that left the index inside the window is absent, not thin. And a second filter removed companies
renamed inside the window, which cannot be audited from the loaded data.
`INGEST_LOG.md` carries both mechanisms in full.

fix is a point-in-time constituent list, which this project does not have. The honest statement is
that this is a study of 100 companies that were large-cap in August 2026, over 2020–2026 — not a
study of the market over 2020–2026.

