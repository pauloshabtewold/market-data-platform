# Query performance

Ten analytical queries over 41,668,537 minute bars in 71 monthly partitions, measured on the
database the pipeline loaded. Every number below was taken on this machine, at the settings
named in [Measurement conditions](#measurement-conditions), with the parameters named beside
it. A latency figure without its parameters is not reproducible and not comparable to the next
one, so the parameters are part of every row.

The full `EXPLAIN (ANALYZE, BUFFERS)` output behind every table here — twenty plans, before and
after, one file per variant with its bound parameters and session settings in the header — is
kept alongside this document rather than quoted into it. The tables are summaries; the plans are
the evidence.

## What is being measured, and why it is mostly not wall-clock

The ten queries do not share a target, because they do not share a shape, and one target
applied to all ten would be wrong for at least seven of them.

| class | queries | what an index can do | target |
| --- | --- | --- | --- |
| **A** | 1, 3, 6 | serve a bounded window from an index | **< 100 ms** |
| **B** | 2 | let the query **read fewer rows** — it touches 2 symbols of 100 | **≥ 10× fewer blocks** |
| **C** | 4, 5, 7, 8, 9, 10 | nothing: they read every row by definition | **evidence of optimality** |

Class B is gated on **blocks touched**, not on time. Blocks are a property of the plan: the
same number on a warm cache, a cold cache, a laptop, or RDS. Wall-clock is not — on a warm
instance a plan that reads 35× fewer blocks can measure under 2× faster, and tuning until the
stopwatch says 10× is tuning the page cache. Wall-clock is recorded beside it and gated on
nothing.

Class C's target is not a speedup because there is no speedup to be had, and demanding one
would push a builder into adding an index that is never chosen and reporting the difference as
a result. Each Class C query instead carries four artifacts: the parallel plan the planner
should pick and did, a candidate index built and measured and shown not to help, the row math,
and — for 7, 9 and 10 — a byte ratio used as a **falsifiable prediction** rather than as a
recorded number.

## Measurement conditions

**Postgres 16, stock memory settings.** `shared_buffers` 128 MB, `work_mem` 4 MB,
`maintenance_work_mem` 64 MB, `effective_cache_size` 4 GB,
`max_parallel_workers_per_gather` 2, on a 4-CPU / 8 GiB VM.

Nothing here was measured with memory tuned up, and that was a deliberate decision rather than
an omission. Three things make it the right one:

- The gated metric is `Shared Hit + Shared Read` off the root plan node. **A sort that spills
  writes to `Temp Read/Written`, which is a separate counter** — so a spill cannot inflate the
  number being gated. Measured directly: at `work_mem` 4 MB, 64 MB and 256 MB, the root-node
  block counts for a Class A and a Class C shape were **bit-identical** (417 and 514,152).
- `shared_buffers` moves the split between `hit` and `read` and never their sum, which is the
  property that makes these figures portable to RDS.
- Raising `work_mem` made a Class C shape measurably *slower* on this host — 29.8 s at 4 MB
  against 46.9 s at 256 MB — because three workers each claiming 256 MB against a 128 MB
  `shared_buffers` displaces the page cache the query depends on.

**Three Class C queries spill, and that is not a defect.** At 4 MB, measured on the queries as
they ship: `04_volume_profile.sql` **655 MB**, `07_vwap_check.sql` **797 MB**,
`08_top_minutes.sql` **576 MB**, all `external merge`. Nothing is wrong with them — they
aggregate 41.7M rows into ~148k groups and that sort does not fit in 4 MB at any sane setting.
It costs them wall-clock and costs the gate nothing, because spills land in `Temp Read/Written`
and every Class C verdict is a plan property.

The two coverage queries were different: their 1.2 GB sorts came from a merge join the query was
forcing, and fixing the plan removed them entirely — at the same 4 MB. That is a plan problem
that looked like a memory problem, and it is the one case here where a spill was worth chasing.
See [The two coverage queries](#the-two-coverage-queries-9-and-10).

`maintenance_work_mem` is raised to 1 GB for index builds alone. It changes build wall-clock and
nothing any query measures, and no figure in this document was taken while it was raised.

**`VACUUM (ANALYZE)` on every partition first.** An index-only scan falls back to heap fetches
wherever the visibility map says a page may hold invisible tuples, so a freshly-ingested
partition reads far past its own byte ratio and the Class C ceilings look broken on a database
that is fine. The map went from **89.7053%** all-visible to **100.0000%** across all 71
partitions, in 92 s.

That starting figure is worth recording, because the spec expects zero: the ingest never
vacuums, but **autovacuum had already run**, on all 71 partitions, about six hours after the
load finished. The manual pass was still required — 10.3% of pages would have taken heap
fetches — but a session that expects 0% and measures 89.7% has not run the wrong statement.

**Which figures here are repeated, and which are one observation.** Every block count, node
type, worker count, spill size and byte ratio below is a **property of the plan** and was
identical every time it was measured — including on a full re-capture taken from scratch after
the first set was lost. Those are the numbers the gate rests on.

**Wall-clock is not one of them.** On this 5.3 GB working set the same query against the same
data varies by more than 2× run to run, and a variant measured after three others has read a
cache they filled. Where a timing is a median of repeats this document says so and shows the
runs; where it is a single observation it is marked as one. No gate verdict depends on a timing.

**Buffer counts come off the root plan node and are never summed.** Postgres buffer counts are
cumulative: every node already includes its children, so adding them counts the same read once
per level of the tree. On this database's own query 9 the root reports **514,251** blocks and
the sum over all nodes reports **4,943,377** — **9.61× more**, for one identical execution. The
inflation factor is plan-shape dependent, so it is not a constant that can be divided back out.

Every count in this document was **cross-checked two ways before it was recorded**: the root
object of `EXPLAIN (…, FORMAT JSON)` and the first `Buffers: shared hit=… read=…` line of the
same plan in text form. These are different output paths in Postgres, and the harness raises
rather than warns if they disagree.

## The heap the numbers rest on

Class B's gated number moves by **54–64× on the heap's write order alone**, so the layout is
part of the evidence rather than an assumption behind it. This database is also of mixed
provenance — the first fifty symbols were restored from a `pg_dump` and the second fifty loaded
live — and no before-measurement was taken at the time.

`pg_stats.correlation` cannot discriminate this (it reads 0.3279 here, which is evidence of
nothing). Two `ctid` queries can, and they were run over **every symbol in every partition**
rather than as a spot check:

| partitions | rows checked | physical `ts` inversions | symbols split across runs | worst symbol's share of a partition |
| --- | --- | --- | --- | --- |
| 71 | 41,668,537 | **0** | **0** | 1.64% |

Every symbol occupies exactly one contiguous, ascending run in every partition. Per provenance
half, on the same four symbols across all 71 partitions:

| half | symbol | rows | distinct heap pages | % of heap | rows/page | inversions |
| --- | --- | --- | --- | --- | --- | --- |
| restored | AAPL | 576,962 | 7,153 | 1.39 | 80.7 | 0 |
| restored | MSFT | 544,918 | 6,795 | 1.32 | 80.2 | 0 |
| live | HON | 343,634 | 4,310 | 0.84 | 79.7 | 0 |
| live | LIN | 281,129 | 3,538 | 0.69 | 79.4 | 0 |

The two halves are indistinguishable. `pg_restore` replays a dump in source heap order and the
source heap was pipeline order, so the restored half is laid out exactly as if it had been
loaded live. A correctly loaded heap gives roughly 1% here; a day-major one would approach 100%.

## The heap/index byte ratio

The ceiling on what an index-only scan can buy is a ratio of two stored sizes, and no amount of
`VACUUM`, cache warmth or faster disk moves it. Measured against the **child** index by name —
`pg_relation_size` on a partitioned index returns 0, so the parent form is a division by zero,
and the whole-table figure sums over `pg_partition_tree`.

| PK index, per partition | value |
| --- | --- |
| partitions | 71 |
| min ratio | 3.1429 |
| mean ratio | 3.1580 |
| max ratio | 3.1710 |
| standard deviation | 0.0055 |
| **pooled whole-table** | **3.1584** |

**Feature 1's ratio survives the re-check.** It published `HEAP_INDEX_BYTE_RATIO =
3.1656441717791411`, taken on a 5-ticker, one-month partition. That same partition,
`bars_2026_06`, now holds 699,245 rows instead of 41,723 — 17× more — and reads **3.1654**, a
difference of **0.008%**. The pooled whole-table figure is 0.23% from the published one. The
threshold that would require this document to explain a discrepancy is ~25%, so there is
nothing to explain: the ratio is a property of the row width and the load order, not of the row
count.

## The two coverage queries (9 and 10)

These are the only two queries in the set whose plan was actually wrong, and the fix is the
clearest tuning story here — so it is written out in full rather than reduced to a row.

`09_coverage.sql` shipped at Feature 2. Over the full universe it took **173.9 s**, ran every
one of its 71 partition scans **serially**, and spilled. It launched no parallel workers at
all, which is not the plan an unfiltered full-universe aggregation should get.

Two independent causes, and **neither change alone is sufficient**:

| `actual` CTE | `bounded` CTE | exec, 3 interleaved runs | median | parallel scans | workers | spill | join |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| as shipped | materialized | 208,493 · 168,703 · 179,389 | 179,389 ms | 0 | 0 | 5,264 kB | Merge |
| as shipped | `NOT MATERIALIZED` | 159,437 · 209,426 · 147,600 | 159,437 ms | 0 | 0 | 5,264 kB | Merge |
| rewritten | materialized | 121,641 · 213,305 · 130,421 | 130,421 ms | 0 | 0 | **1,223,256 kB** | Merge |
| **rewritten** | **`NOT MATERIALIZED`** | 58,256 · 111,828 · 46,948 | **58,256 ms** | **71** | **2** | **none** | **Hash** |

**Read the four right-hand columns, not the timings.** They were identical in all twelve runs.
The timings were not: the tuned variant alone ranges 46,948–111,828 ms, a 2.4× spread on one
query against one dataset, and single runs of these four variants can swap places entirely.

An earlier version of this table published one run per variant — 173,923 / 128,138 / 112,306 /
**32,737** ms — taken back to back in that order, so each variant read a cache the ones before it
had filled. The ordering it showed is right and survives repeats; the magnitude did not. It made
the tuning look like **5.31×** where the medians make it **3.08×**. The superseded figures are
named here rather than deleted, and `73164af`'s commit message still carries 32,737.

**The counting join was against the 148,400-row session set, keyed on `symbol` as well as the
trading date.** 148k against 41.7M is a shape the planner serves with a merge join, sorting
every bar in the database. Joining to the 1,484-row calendar instead and grouping afterwards
costs nothing: `first_bar_ts` is `MIN(ts)`, so no bar can precede its own symbol's floor, and
the outer `LEFT JOIN` already discards any symbol not in `expected`. That premise was checked
rather than argued — all 100 symbols hold `first_bar_ts = MIN(ts)` exactly.

**`bounded` was materialized, because it is referenced three times.** A materialized CTE
carries no statistics, so the planner cannot cost a hash join against it and falls back to the
merge join. `NOT MATERIALIZED` restores `market_days`' statistics.

Row three is why both changes ship together: the rewrite *alone* spills 233× more than the
shipped form, because dropping the symbol key widens what the merge join has to sort. Taken as
a lone change it reads as a regression.

The output is unchanged, and that was verified rather than assumed: 103 rows, symmetric
difference 0, identical row for row against the shipped form over the full window, with the
pipeline's gate reading `missing_units 0`, `coverage_pct 72.16`, `uningested_symbols 0`.

`10_missing_minutes.sql` shares the shape and needed only the second change: **131.1 s serial
with the same 1.2 GB spill, against 42.6 s with 71 parallel scans, 2 workers and no spill.**
7,100 rows identical. Its **pooled** complement of query 9 — `100 − 100 × sum(missing) /
sum(expected)` over all 7,100 symbol-months — is **72.1564**, which is 72.16 at two decimals and
matches query 9's own `coverage_pct` of **72.16** exactly. That is the complement identity
checked on real data rather than on a fixture.

**Compute it as a pooled ratio and not as a mean of the per-month percentages**, because the two
disagree: the mean of the 7,100 `missing_pct` values gives a complement of **72.2265**, which
rounds to 72.23. Months carry wildly different denominators — a symbol's first partial month
against a full one — and a mean weighs them equally where a pooled ratio weighs them by size.

The other eight files reference every CTE once, so Postgres inlines them already and none
needed this.

## Class C — evidence of optimality

Six queries read every row by definition. There is no index that improves them and no rewrite
that avoids the scan, so a "10× faster" target is one a correct database fails — and demanding
it pushes a builder into adding an index that is never chosen and reporting the difference as a
result. Each carries four artifacts instead.

### Item 1 — the plan the planner should pick, actually picked

`Workers Launched`, not merely `Workers Planned`. All six:

**Two caveats on this table, both stated before it rather than after.** Its block counts were
taken **before migration 005** added `bars_ts_symbol_idx`; every table after it was re-captured
afterwards. The two eras differ by at most 40 blocks — query 5 reads 25,009 before and a stable
25,049 after, confirmed over three consecutive runs — and queries 9 and 10 are identical on both
sides, so it is a real boundary rather than noise. And the `exec` column is **one observation per
row**, kept only for scale; the columns that carry the verdict are the three on the right.

| query | root node | exec (single run) | root blocks | workers launched | parallel `bars` scans |
| --- | --- | ---: | ---: | ---: | ---: |
| `04_volume_profile.sql` | `WindowAgg` | 242,836 ms | 514,300 | 2 | 71 |
| `05_largest_moves.sql` | `Limit` | 8,191 ms | 25,009 | 2 | 3 |
| `07_vwap_check.sql` | `Finalize GroupAggregate` | 323,955 ms | 514,262 | 2 | 71 |
| `08_top_minutes.sql` | `Incremental Sort` | 163,541 ms | 514,280 | 2 | 71 |
| `09_coverage.sql` | `Sort` | 92,575 ms | 514,251 | 2 | 71 |
| `10_missing_minutes.sql` | `Merge Left Join` | 133,978 ms | 514,220 | 2 | 71 |

Query 5 scans 3 partitions rather than 71 because it is the only Class C file with a window —
`:start`/`:end` bind a 90-day range, which spans 3 monthly partitions. Partition pruning is
doing exactly what it should; the query is still Class C because ranking by magnitude must see
every row *in that window* before it knows which N come out.

### Item 2 — the candidate index, built, measured, and not chosen

Each index a reviewer would ask for, built on the full 41.7M rows, measured with the planner
free to use it, then dropped.

| query | candidate index | build | blocks without | blocks with | index-only scans | verdict |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 4 | `(volume)` | 139 s | 514,264 | **514,264** | 0 | not chosen |
| 5 | `(ts, (abs(100*(close-open)/open)) DESC)` | 206 s | 25,049 | **25,049** | 0 | not chosen |
| 7 | `(symbol, ts) INCLUDE (vwap, volume)` | 267 s | 514,222 | **514,222** | 0 | not chosen |
| 8 | `(ts, volume)` | 356 s | 514,242 | **514,242** | 0 | not chosen |

**The blocks are identical to the block on all four** — the same plan ran with the index on disk
as without it. That is the negative result: each index was built on the full 41.7M rows, costed
by the planner, and rejected. Timings are omitted from this table deliberately; they varied by
more than the difference they would be claiming.

**Query 5's is the one worth a sentence**, because it is the index a reviewer asks for first.
With the ranking expression indexed, the plan is unchanged to the sort key:
`Limit → Gather Merge → Sort (top-N heapsort) → Hash Join → Parallel Append`, ranking still done
by a sort. The reason is the one section 4 gives — the session-bounds join to `market_days` is
not in the index, so the planner cannot stream in ranked order — and this is that prediction
confirmed on the real dataset rather than carried in from a fixture.

The block counts are the evidence, not the timings: **identical to the block on all four**
means the same plan ran both times. The index was on disk, costed, and rejected. The wall-clock
differences either side of that are run-to-run noise on a 5.3 GB working set — measured at more
than 2× on a single query — which is exactly why this gate is on blocks.

Queries 4 and 8 are deliberately **not** measured forced onto their candidate index. Neither
index carries the columns those queries read — `trade_count`, `volume` and the session join — so
forcing them off a sequential scan means a heap fetch per row over 41.7M rows. That is
unbounded (it ran for over an hour without finishing) and shows nothing the free choice has not.
The three queries that *can* be served index-only are measured that way below, which is exactly
the split section 4 draws when it asks for the byte math for 7, 9 and 10 only.

### Item 3 — the row math

All six read **100% of the rows in their window**: five over the whole universe and query 5 over
the 90 days its parameters bind. There is no selectivity for an index to exploit.

The crossover follows from the cost constants rather than from taste. At `seq_page_cost` 1 and
`random_page_cost` 4, an index scan visiting a fraction *f* of the heap pays about `4f` per page
against a sequential scan's `1`, so it can only win below **f ≈ 25%** on a perfectly correlated
heap, and far below that on an uncorrelated one. Query 5 is the useful check on that reasoning:
its window puts it at **25,009 root-node blocks against the 514,300 the whole-universe queries
read — 4.9%** — and the planner *still* chose a parallel sequential scan over any index. That is
a measured point on the curve rather than a derived one. The other five sit at f = 1.0, where no index can win by
construction.

### Item 4 — the byte math, as a prediction that could have failed

The ceiling on an index-only scan is `heap bytes ÷ index bytes`, and no `VACUUM`, cache warmth
or faster disk moves it. That makes it a **falsifiable prediction**: force the index-only scan
and the root-node block ratio must land within 10% of the byte ratio. Without that test the
pass condition would be the measurement evaluating itself.

| query | index scanned | byte ratio | forced block ratio | apart | |
| --- | --- | ---: | ---: | ---: | --- |
| 7 | child covering `(symbol, ts) INCLUDE (vwap, volume)` | 1.8924 | 1.9021 | **0.51%** | holds |
| 9 | child PK | 3.1584 | 3.1716 | **0.42%** | holds |
| 10 | child PK | 3.1584 | 3.1719 | **0.43%** | holds |

**Query 7's ratio is its own number and not the PK's**, which is why there are two config keys
rather than one. Its index has to carry four of nine columns, close to a second copy of the
heap, so it is worth **1.89×** where the PK is worth 3.16× — the two are **67% apart on
identical data**, and writing the PK's figure into query 7's row would be wrong in the
flattering direction and would cite an index the query cannot use. `HEAP_INDEX_COVERING_RATIO`
is measured here because this is the only window in the whole build where that index is on
disk: it does not exist before this feature builds it for the negative result, and it does not
exist after this feature drops it.

**This prediction failed once, and the failure was the query rather than the database.** An
earlier draft of query 7 also reconstructed a typical price from `high`, `low` and `close` —
three columns the covering index does not carry — which makes an index-only scan impossible by
construction. The forced scan then reported **0 index-only scans and 682,813 root blocks against
the sequential scan's 514,260**, a ratio of **0.75×**: worse than what it replaced, and 60% away
from the byte ratio. Narrowing the query to the four columns the index carries took it to
0.51%. A self-evaluating measurement would have recorded 1.89× and moved on; this is what the
falsifiable form is for, and the test that now guards it fails if any other `bars` column is
read back in.

Both forced scans are genuinely index-only where it matters: **every `bars` partition reports 0
heap fetches**, in both queries, across all 71. Query 10 reports 0 in total.

Query 9 reports 1,584, and all of them come from **`ingest_progress`** — walked node by node
through its 73 `Index Only Scan` nodes, `bars` and `market_days` contribute none. The reason is
scope: this feature's mandated first action is `VACUUM (ANALYZE)` on the `bars` partitions, which
is what the spec asks for and what was run, so `bars` is spotless. A forced Class C scan also
reads `ingest_progress`, which no one has vacuumed by hand — it sits at **82.2%** all-visible
(37 of 45 pages) against `market_days`' **100.0%** (22 of 22). Eight stale pages against 162,142
blocks changes nothing, and the prediction still holds at 0.42%.

**Wall-clock at that ceiling is a wash and is not portable, exactly as the design predicted.**
Query 9's forced scan is 105,024 ms against 92,575 free — 0.88×, *slower*. Query 10's is
102,370 ms against 133,978 — 1.31×, faster. Two queries of the same shape against the same
index, disagreeing in sign. The blocks, meanwhile, agree with the bytes to two figures on both.
That gap is the whole argument for gating on blocks.

Neither planner ever chose the index-only scan on its own: free-choice block ratio **1.00×** for
both, which is the seq scan. The mechanism is real, it does what it always said it did, and it
is worth **3.16×** — not 10×.

## Class A — the three queries the API will serve

Measured with the parameters section 4's table binds, five runs each, median reported. Unbound
these run over seven years and miss 100 ms by three orders of magnitude on a database with
nothing wrong with it, so the parameters are half the measurement.

`:symbol = AAPL` (first of `tickers.txt`), `:end = 2026-06-30` (`INGEST_END`),
`:start = 2026-04-01` (`INGEST_END` − `AGG_MAX_WINDOW_DAYS`, 90 days).

| query | runs (ms) | median | root blocks | target | |
| --- | --- | ---: | ---: | ---: | --- |
| `01_volatility.sql` | 58 53 46 48 49 | **49.0 ms** | 423 | <100 ms | pass |
| `03_gaps.sql` | 27 27 27 26 28 | **26.8 ms** | 431 | <100 ms | pass |
| `06_daily_rollup.sql` | 27 29 28 26 27 | **26.7 ms** | 420 | <100 ms | pass |

Roughly 420 blocks each — about 3.4 MB — because partition pruning takes a 90-day window down
to three monthly partitions and the PK then serves one symbol out of them.

**Against the same constructed before Class B uses** — `enable_indexscan`, `enable_bitmapscan`
and `enable_indexonlyscan` all off, which is the only way to get an untuned state for a
mechanism that has existed since Feature 1:

| query | before (constructed) | after | ratio | before plan | after plan |
| --- | ---: | ---: | ---: | --- | --- |
| `01_volatility.sql` | 25,088 | 423 | **59.31×** | 3 parallel seq scans, 2 workers | 1 index scan, 0 workers |
| `03_gaps.sql` | 25,117 | 431 | **58.28×** | 3 parallel seq scans, 2 workers | 1 index scan, 0 workers |
| `06_daily_rollup.sql` | 25,085 | 420 | **59.73×** | 3 parallel seq scans, 2 workers | 1 index scan, 0 workers |
| `02_correlation.sql` (Class B) | 25,125 | 830 | 30.27× | 3 parallel seq scans, 2 workers | 1 index scan, 0 workers |

**Class A's block reduction is roughly double Class B's**, and the reason is the whole basis of
the class split: these read one symbol where query 2 reads two. Selectivity is what the index
buys, and it buys exactly twice as much at half the symbols. Class A is not *gated* on this
ratio — its target is the 100 ms above — but the before/after is recorded here because 6.3 #14
asks for it on all ten queries, not only on the one with a block target.

The first run of a cold sweep measured query 1 at **312 ms**, and that figure is worth keeping
next to the other five: it is the same plan and the same 423 blocks, differing only in what the
page cache happened to hold. It is also the reason the table above reports a median of five
rather than a single number.

## Class B — query 2, the one query with a selective filter

Query 2 touches 2 symbols of 100. That is the only query in the set where an index can make it
**read fewer rows**, which is why it is the only one asked for an order of magnitude.

**The "before" is constructed, and is labelled as constructed.** Query 2's mechanism is the PK,
which has existed since Feature 1, so there is no untuned state to measure. Section 4 mandates
`enable_indexscan=off; enable_bitmapscan=off; enable_indexonlyscan=off` in the same session
rather than dropping and rebuilding a primary key over 41.7M rows to arrive at the same number.

| | root blocks | median exec | root node |
| --- | ---: | ---: | --- |
| simulated before | 25,125 | 561.8 ms | `WindowAgg` |
| after | **830** | 80.3 ms | `WindowAgg` |
| **ratio** | **30.27×** | 7.00× | |

**Blocks 30.27× against a ≥10× target — passes.** Wall-clock moved 7.00× over the same pair,
and the gap between those two numbers is the entire argument for gating on blocks: a stopwatch
tuned to 10× would have called this query finished at less than a quarter of the block
reduction it actually achieves, and a stopwatch on a colder cache would have called it done
sooner still.

This number is only meaningful because of the heap it was measured on. On a day-major heap the
same query and the same index measure 1.13–1.42×, and the gate fails. See
[The heap the numbers rest on](#the-heap-the-numbers-rest-on) — 0 inversions, 0 split runs,
every symbol on ~1% of the pages.

## The other two index decisions

### The hot-window partial index — provisionally kept

Created on the four recent **child** partitions directly, never on the parent, with
`HOT_WINDOW_MONTHS = 4` fixing the cutoff at `INGEST_END` − 4 months:

```sql
CREATE INDEX bars_2026_0N_hot_idx ON bars_2026_0N (ts, symbol) INCLUDE (open, close)
  WHERE ts >= TIMESTAMPTZ '2026-03-01 00:00:00+00';
```

Measured against the plain `(ts, symbol)` index the children inherit from the parent, on the
access path `/analytics/largest-moves` will use — universe-wide, ordered `(ts, symbol)`,
keyset-paginated, reading only `open` and `close`:

| | root blocks | index-only scans | plain index scans |
| --- | ---: | ---: | ---: |
| inherited `(ts, symbol)` | 1,012 | 0 | 4 |
| with the partial index | **16** | **4** | 0 |

**63.25× fewer blocks, and the planner chose it unprompted.** 134 MB across four partitions,
built in 8 s. The verdict is **provisional**: there is no endpoint until Feature 7, so this
measures the `.sql` form of the same access path rather than the endpoint itself. Feature 7
confirms or reverses it, and whichever feature drops it says so here.

Two things about the placement are rules rather than preferences, both verified: creating this
on the *parent* propagates the predicate to every child, so every pre-2026 partition gets a
partial index no row can satisfy — real catalog entries, real maintenance, zero rows — and the
propagated copies then **cannot be dropped individually** (`SQLSTATE 2BP01`; a partition index
attached to a partitioned index is a dependent object). The parent mistake is all-or-nothing,
not prune-later. It also has a consequence at `v2`: because the index lives on children,
`CREATE TABLE ... (LIKE bars INCLUDING ALL)` on RDS copies **no** partial index, and Feature 10
recreates it by hand over the same `HOT_WINDOW_MONTHS`.

The predicate is an explicit `timestamptz` and not `DATE '2026-03-01'`. Comparing a
`timestamptz` to a `date` coerces through the session zone, which is not `IMMUTABLE`, and
Postgres refuses it outright — *functions in index predicate must be marked IMMUTABLE*. It is
the same hazard the partition bounds carry, caught by the server rather than stored wrong.

### BRIN on `ts` — measured, and not kept

The honest expectation up front was that monthly partitioning already prunes by time, so BRIN's
win here would be **index size, not query time**. That is what it turned out to be.

Measured on `bars_2026_06` — a real partition of 699,245 rows, never a fixture, because BRIN has
a fixed floor of about 24 kB and on a small table comes out *larger* than a b-tree:

| | bytes | |
| --- | ---: | --- |
| heap | 70,688,768 | |
| b-tree PK | 22,331,392 | |
| BRIN on `ts` | **24,576** | **908.7× smaller than the b-tree** |

24,576 bytes is the floor itself — this partition's whole month of timestamps fits in the
metapage, revmap and first range page.

On a five-day range probe the planner **did not choose it**: free choice was a sequential scan
at 32.93 ms and 8,629 blocks. Forced onto BRIN it read 2,532 blocks — genuinely fewer — and took
**367.89 ms, 11× slower**, because a BRIN range scan rechecks every tuple in every block its
summary admits. Dropped.

That is the documented negative result the design asked for, and it is the right answer: the
partition key already does BRIN's job here, and a 24 kB index that the planner never picks is
worth knowing about rather than carrying.

### What is deliberately not indexed

Nothing on `volume`, and the row math above is why: queries 4 and 8 are the only two that filter
or rank on it, both read 100% of the universe, and both were measured with `(volume)` and
`(ts, volume)` on disk and rejected the index. An index that is never chosen is not free — it is
maintained on every one of 41.7M inserts.
