# Query performance

Ten analytical queries over 41,668,537 minute bars in 71 monthly partitions, measured on the
database the pipeline loaded. Every number below was taken on this machine, at the settings
named in [Measurement conditions](#measurement-conditions), with the parameters named beside
it. A latency figure without its parameters is not reproducible and not comparable to the next
one, so the parameters are part of every row.

The full `EXPLAIN (ANALYZE, BUFFERS)` output behind the before/after and candidate-index tables —
twenty plans, one file per variant with its bound parameters in the header — is kept alongside this
document rather than quoted into it. Two sets are an exception and are records rather than
summaries: the hot-index and BRIN tables, whose harness wrote to a path that no longer exists, and
the Class A/B wall-clock sweeps, whose harness overwrites one output file per run so only the most
recent survives.

**The hot-index table's 1,012-block "before" row cannot be retaken as the database stands**, because
the four partial indexes it is compared against are still live — retaking it means dropping them
first. What survives a retake is the ratio: `1,012 ÷ 16 = 63.25` exactly, and the ratio is what this
document argues from. `docs/METHODOLOGY.md` carries the procedure for reading that row again on a
fresh copy. **None of the gated block counts depend on the missing files** — 423, 431, 420, 830 and
25,125 triangulate exactly across the twenty captured plans, the surviving re-run and the narrative.

## What is being measured, and why it is mostly not wall-clock

The ten queries do not share a target, because they do not share a shape.

| class | queries | what an index can do | target |
| --- | --- | --- | --- |
| **A** | 1, 3, 6 | serve a bounded window from an index | **< 100 ms** |
| **B** | 2 | let the query **read fewer rows** — it touches 2 symbols of 100 | **≥ 10× fewer blocks** |
| **C** | 4, 5, 7, 8, 9, 10 | nothing: they read every row by definition | **evidence of optimality** |

Class B is gated on **blocks touched**, not on time. Blocks are a property of the plan — the same
number on a warm cache, a cold cache, a laptop or RDS. Wall-clock is not: the one query gated here
reads **30.27× fewer blocks** and measures **7.00× faster**, and two later sweeps read the same
blocks to the digit while the wall-clock half moved to **7.19×** and **7.87×**. Tuning until the
stopwatch says 10× is tuning the page cache.

Class C's target is not a speedup because there is none to be had, and demanding one would push a
builder into adding an index that is never chosen and reporting the difference as a result. Each
Class C query instead carries up to four artifacts: the parallel plan the planner should pick and
did, a candidate index built and shown not to help (queries 4, 5, 7, 8), the row math, and a byte
ratio used as a **falsifiable prediction** rather than a recorded number (7, 9, 10).

## Measurement conditions

**Postgres 16, stock memory settings.** `shared_buffers` 128 MB, `work_mem` 4 MB,
`maintenance_work_mem` 64 MB, `effective_cache_size` 4 GB, `max_parallel_workers_per_gather` 2, on a
4-CPU / 8 GiB VM. Nothing here was measured with memory tuned up, deliberately: the gated metric is
`Shared Hit + Shared Read` off the root plan node, and **a sort that spills writes to
`Temp Read/Written`, a separate counter** — so a spill cannot inflate the number being gated.
`shared_buffers` moves the split between `hit` and `read` and never their sum, which is what makes
these figures portable to RDS; it was not varied here because it has `postmaster` context and no run
behind any figure restarted a server.

**Three Class C queries spill, and that is not a defect.** At 4 MB each spill is three concurrent
per-worker external merges: `04_volume_profile.sql` **6.30 GiB** at the root, `07_vwap_check.sql`
**7.42 GiB**, `08_top_minutes.sql` **5.39 GiB**. They aggregate 41.7M rows into ~148k groups and that
sort does not fit in 4 MB at any sane setting. It costs them wall-clock and costs the gate nothing.
The two coverage queries were different — their sorts came from a merge join the query was forcing,
and fixing the plan removed them entirely at the same 4 MB. Query 9 as shipped spilled 5,264 kB
against query 10's 1.2 GB. That is a plan problem that looked like a memory problem, and it is the
one case here where a spill was worth chasing.
See [The two coverage queries](#the-two-coverage-queries-9-and-10).

`maintenance_work_mem` is raised to 1 GB for index builds alone, and no figure here was taken while
it was raised.

**`VACUUM (ANALYZE)` on every partition first.** An index-only scan falls back to heap fetches
wherever the visibility map says a page may hold invisible tuples, so a freshly-ingested partition
reads far past its own byte ratio and the Class C ceilings look broken on a database that is fine.
The map went from **89.7053%** all-visible to **100.0000%** across all 71 partitions, in 92 s
(`Time: 91770.519 ms`). The 100.0000% over 514,046 relpages is live-reproducible from `pg_class` and
was reproduced exactly; the 89.7053% is a pre-VACUUM state and stands as recorded rather than as
reproducible. It is worth recording because the spec expects zero: the ingest never vacuums, but
**autovacuum had already run** on all 71 partitions about six hours after the load. The manual pass
was still required — 10.3% of pages would have taken heap fetches — but a session that expects 0%
and measures 89.7% has not run the wrong statement.

**Every count in the Class A, B and C tables is a property of the plan** and was identical every time
it was measured on one side of migration 005, including on a full re-capture from scratch after the
first set was lost. **Migration 005 is where four of those counts move, and none moves by more than
40 blocks**: query 4 reads 514,300 before against 514,264 after (−36), query 5 reads 25,009 against
25,049 (+40), query 7 reads 514,262 against 514,222 (−40), query 8 reads 514,280 against 514,242
(−38). Each side is stable over repeats; queries 9 and 10 are identical on both. Query 7's forced
block ratio carries the same boundary at 0.01%, 1.9021 against 1.9019. That is a real boundary rather
than noise. The two endpoint-forms subsections under Class A are the exception: there a statement's
first run on a connection can read more blocks than its later runs, and each reports its own first
runs where they matter.

**Wall-clock is not a property of the plan.** This working set is one database at two sizes: summed
over `bars` and its children, `pg_total_relation_size` read 5,547,909,120 bytes (5,291 MB) when the
universe finished loading and reads 6,998,122,496 bytes (6,674 MB) with migration 005's index and the
four hot-window indexes on disk. On it the same query against the same data varies by more than 2×
run to run, and a variant measured after three others has read a cache they filled. **Two verdicts
here are gated on a timing rather than a count: Class A's <100 ms target, and 6.3 #13's requirement
that the deep page's HTTP p50 sit within 1.5× page 1's.** Both are read as a statistic over at least
five runs and never off a single observation.

**Buffer counts come off the root plan node and are never summed.** Postgres buffer counts are
cumulative: every node already includes its children, so adding them counts the same read once per
level of the tree. On query 9 the root reports **514,251** blocks and the sum over all nodes reports
**4,943,377** — **9.61× more**, for one identical execution. The inflation factor is plan-shape
dependent, so it cannot be divided back out.

Every count in the **Class A and Class B** tables was **cross-checked two ways before it was
recorded**: the root object of `EXPLAIN (…, FORMAT JSON)` against the first `Buffers:` line of the
same plan in text form, with the harness raising rather than warning if they disagree. **Five sets of
figures do not carry that check in full**, and each is named where it appears: Class C, whose harness
emits no JSON plan at all; the two endpoint-forms subsections, which check execution blocks only; and
the hot-window index and BRIN tables, whose harness wrote to a path that no longer exists, so each is
the record of its own measurement rather than a summary of one.

**Every measurement here is a fresh connection, with two exceptions.** Each harness spawns a new
`docker compose exec` per query, which pays catalog and sort-operator lookups a warm backend already
has cached, so the published counts are cold and self-consistent. Running the ten committed query
files inside one continuous `psql` session instead gives counts a few blocks lower —
`06_daily_rollup.sql` settles at 414 against the published 420, `03_gaps.sql` at 418 against 431 —
and a verifier who re-runs them that way and finds those numbers has not found a defect. The
endpoint-forms subsections are measured inside one session per harness and are therefore already on
the lower side of that pair; "The bounds a request runs under" is measured against scratch containers
rather than the loaded database and states its own conditions where it opens.

## The heap the numbers rest on

Class B's gated number moves by **54–64× on the heap's write order alone** — a fixture measurement
carried in from the design work — so the layout is part of the evidence rather than an assumption
behind it. This database is of mixed provenance: the first fifty symbols were restored from a
`pg_dump` and the second fifty loaded live, and no before-measurement was taken at the time.

`pg_stats.correlation` cannot discriminate this — across all 71 children it runs −0.0076 to 0.0843,
which is evidence of nothing either way. Two `ctid` queries can, and they were run over **every
symbol in every partition** rather than as a spot check:

| partitions | rows checked | physical `ts` inversions | symbols split across runs | worst symbol's share of heap |
| --- | --- | --- | --- | --- |
| 71 | 41,668,537 | **0** | **0** | 1.64% |

Every symbol occupies exactly one contiguous, ascending run in every partition. Per provenance half,
on the same four symbols across all 71 partitions:

| half | symbol | rows | distinct heap pages | % of heap | rows/page | inversions |
| --- | --- | --- | --- | --- | --- | --- |
| restored | AAPL | 576,962 | 7,153 | 1.39 | 80.7 | 0 |
| restored | MSFT | 544,918 | 6,795 | 1.32 | 80.2 | 0 |
| live | HON | 343,634 | 4,310 | 0.84 | 79.7 | 0 |
| live | LIN | 281,129 | 3,538 | 0.69 | 79.5 | 0 |

**The two halves are indistinguishable.** `pg_restore` replays a dump in source heap order and the
source heap was pipeline order, so the restored half is laid out exactly as if it had been loaded
live. A correctly loaded heap gives roughly 1% here; a day-major one would approach 100%.

## The heap/index byte ratio

The ceiling on what an index-only scan can buy is a ratio of two stored sizes, and no amount of
`VACUUM`, cache warmth or faster disk moves it. Measured against the **child** index by name —
`pg_relation_size` on a partitioned index returns 0, so the parent form is a division by zero, and
the whole-table figure sums over `pg_partition_tree`.

| PK index, per partition | value |
| --- | --- |
| partitions | 71 |
| min ratio | 3.1429 |
| mean ratio | 3.1580 |
| max ratio | 3.1710 |
| standard deviation | 0.0055 |
| **pooled whole-table** | **3.1584** |

**Feature 1's ratio survives the re-check.** It published `HEAP_INDEX_BYTE_RATIO =
3.1656441717791411` on a 5-ticker, one-month partition. That partition now holds 16.76× more rows
and reads **3.1654**, a difference of **0.008%**; the pooled figure is 0.23% from the published one.
The ratio is a property of the row width and the load order, not of the row count.

## The two coverage queries (9 and 10)

These are the only two queries whose plan was actually wrong, and the fix is the clearest tuning
story here. `09_coverage.sql` shipped at Feature 2 taking a median **179.4 s**, running every one of
its 71 partition scans **serially**, and spilling — not the plan an unfiltered full-universe
aggregation should get. **Two independent causes, and neither change alone is sufficient:**

| `actual` CTE | `bounded` CTE | exec, 3 interleaved runs | median | parallel scans | workers | spill | join |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| as shipped | materialized | 208,493 · 168,703 · 179,389 | 179,389 ms | 0 | 0 | 5,264 kB | Merge |
| as shipped | `NOT MATERIALIZED` | 159,437 · 209,426 · 147,600 | 159,437 ms | 0 | 0 | 5,264 kB | Merge |
| rewritten | materialized | 121,641 · 213,305 · 130,421 | 130,421 ms | 0 | 0 | **1,223,256 kB** | Merge |
| **rewritten** | **`NOT MATERIALIZED`** | 58,256 · 111,828 · 46,948 | **58,256 ms** | **71** | **2** | **none** | **Hash** |

**Read the four right-hand columns, not the timings.** They were identical in all twelve runs; the
timings were not — the tuned variant alone ranges 46,948–111,828 ms, a **2.4× spread** on one query
against one dataset, and single runs of these four variants can swap places entirely.

**`bounded` was materialized, because it is referenced three times.** A materialized CTE carries no
statistics, so the planner cannot cost a hash join against it and falls back to a merge join;
`NOT MATERIALIZED` restores `market_days`' statistics. Row three is why both changes ship together:
the rewrite *alone* spills 232× more than the shipped form, because dropping the symbol key widens
what the merge join has to sort — taken as a lone change it reads as a regression.

The output is unchanged, verified rather than assumed: 103 rows, symmetric difference 0, identical
row for row, with the pipeline's gate reading `missing_units 0`, `coverage_pct 72.16`,
`uningested_symbols 0`.

`10_missing_minutes.sql` shares the shape and needed only the second change: 131.1 s serial with a
1.2 GB spill against 42.6 s with 71 parallel scans, 2 workers and no spill — **one observation per
variant, not a median**. A later re-capture read the shipped form at **89.3 s** on the same plan,
**2.1× the wall-clock**, which is the run-to-run spread the table above shows. Its pooled complement
of query 9 is **72.1564**, matching query 9's own `coverage_pct` of 72.16 — the complement identity
checked on real data rather than on a fixture.

**Compute it as a pooled ratio and not as a mean of the per-month percentages**, because the two
disagree: the mean of the 7,100 `missing_pct` values gives **72.2265**. Months carry wildly
different denominators, and a mean weighs them equally where a pooled ratio weighs them by size.

## Class C — evidence of optimality

Six queries read every row by definition. There is no index that improves them and no rewrite that
avoids the scan, so a "10× faster" target is one a correct database fails. Each carries three or four
artifacts instead: the plan and the row math for all six, a candidate index for 4, 5, 7 and 8, and
the byte math for 7, 9 and 10.

### Item 1 — the plan the planner should pick, actually picked

`Workers Launched`, not merely `Workers Planned`. The `exec` column is **one observation per row**
from a pre-005 capture, kept only for scale; the three columns on the right carry the verdict.

| query | root node | exec (single run) | root blocks | workers launched | parallel `bars` scans |
| --- | --- | ---: | ---: | ---: | ---: |
| `04_volume_profile.sql` | `WindowAgg` | 242,836 ms | 514,300 | 2 | 71 |
| `05_largest_moves.sql` | `Limit` | 8,191 ms | 25,009 | 2 | 3 |
| `07_vwap_check.sql` | `Finalize GroupAggregate` | 323,955 ms | 514,262 | 2 | 71 |
| `08_top_minutes.sql` | `Incremental Sort` | 163,541 ms | 514,280 | 2 | 71 |
| `09_coverage.sql` | `Sort` | 92,575 ms | 514,251 | 2 | 71 |
| `10_missing_minutes.sql` | `Merge Left Join` | 133,978 ms | 514,220 | 2 | 71 |

Query 5 scans 3 partitions rather than 71 because it is the only Class C file with a window. **The 3
is this window's alignment, not a property of a 90-day range** — one can fall across as many as five
monthly partitions. The query is still Class C because ranking by magnitude must see every row *in
that window* before it knows which come out.

### Item 2 — the candidate index, built, measured, and not chosen

Each index a reviewer would ask for, built on the full 41.7M rows, measured with the planner free to
use it, then dropped.

| query | candidate index | build | blocks without | blocks with | index-only scans | verdict |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 4 | `(volume)` | 139 s | 514,264 | **514,264** | 0 | not chosen |
| 5 | `(ts, (abs(100*(close-open)/open)) DESC)` | 206 s | 25,049 | **25,049** | 0 | not chosen |
| 7 | `(symbol, ts) INCLUDE (vwap, volume)` | 267 s | 514,222 | **514,222** | 0 | not chosen |
| 8 | `(ts, volume)` | 356 s | 514,242 | **514,242** | 0 | not chosen |

**The blocks are identical to the block on all four** — the same plan ran with the index on disk as
without it. Timings are omitted deliberately: they varied by more than the difference they would be
claiming, which is exactly why this gate is on blocks.

**Query 5's is the one worth a sentence**, because it is the index a reviewer asks for first. With
the ranking expression indexed the plan is unchanged to the sort key, because the session-bounds
join to `market_days` is not in the index, so the planner cannot stream in ranked order.

Queries 4 and 8 are deliberately **not** measured forced onto their candidate index: neither index
carries the columns those queries read, so forcing them means a heap fetch per row over 41.7M rows —
unbounded, and it ran over an hour without finishing.

### Item 3 — the row math

All six read **100% of the rows in their window**. There is no selectivity for an index to exploit.
At `seq_page_cost` 1 and `random_page_cost` 4, an index scan visiting a fraction *f* of the heap pays
about `4f` per page against a sequential scan's `1`, so it can only win below **f ≈ 25%** on a
perfectly correlated heap and far below that on an uncorrelated one. Query 5 is the only one off
f = 1.0: **25,049 root blocks against the 514,264 the whole-universe queries read — 4.87%** — and the
planner *still* chose a parallel sequential scan. **4.87% is nowhere near 25%, so this is not a
measurement of where the crossover lies**; what it bounds is the planner's own crossover on this
heap, which sits under 4.87%. The other five sit at f = 1.0, where no index can win by construction.

### Item 4 — the byte math, as a prediction that could have failed

The ceiling on an index-only scan is `heap bytes ÷ index bytes`, and nothing about cache or disk
moves it. That makes it a **falsifiable prediction**: force the index-only scan and the root-node
block ratio must land within 10% of the byte ratio. Without that test the pass condition would be the
measurement evaluating itself.

| query | index scanned | byte ratio | forced block ratio | apart | |
| --- | --- | ---: | ---: | ---: | --- |
| 7 | child covering `(symbol, ts) INCLUDE (vwap, volume)` | 1.8924 | 1.9021 | **0.51%** | holds |
| 9 | child PK | 3.1584 | 3.1716 | **0.42%** | holds |
| 10 | child PK | 3.1584 | 3.1719 | **0.43%** | holds |

**Query 7's ratio is its own number and not the PK's**, which is why there are two config keys rather
than one. Its index carries four of nine columns, close to a second copy of the heap, so it is worth
**1.89×** where the PK is worth 3.16× — **67% apart on identical data**. `HEAP_INDEX_COVERING_RATIO`
is measured here because that index is on disk only inside the runs this feature makes.

**This prediction failed once, and the failure was the query rather than the database.** An earlier
draft of query 7 also reconstructed a typical price from `high`, `low` and `close` — three columns
the covering index does not carry — which makes an index-only scan impossible by construction. The
forced scan reported **0 index-only scans and 682,813 root blocks against the sequential scan's
514,260**, a ratio of **0.75×**: worse than what it replaced, and 60% from the byte ratio. Narrowing
the query to the four columns the index carries took it to 0.51%. A self-evaluating measurement would
have recorded 1.89× and moved on.

**Query 7's row was re-derived from scratch**, because the covering index exists only inside the run
that builds it. Rebuilt over the full 41.7M rows in 264 s, measured and dropped again: byte ratio
**1.8924**, forced block ratio **1.9019**, **0.50% apart**, with **71 index-only scans and 0 heap
fetches**. The negative result came back in the same run — with the index on disk the planner still
read 514,222 blocks and chose no index-only scan.

Both forced scans are genuinely index-only where it matters: **every `bars` partition reports 0 heap
fetches**, across all 71. Query 10 reports 0 in total. Query 9 reports 1,584, all from
**`ingest_progress`** — `bars` and `market_days` contribute none. The reason is scope: this feature's
mandated `VACUUM (ANALYZE)` covers the `bars` partitions, so `bars` is spotless, while
`ingest_progress` sits at **82.2%** all-visible. Eight stale pages against 162,142 blocks changes
nothing, and the prediction still holds at 0.42%.

## Class A — the queries and endpoint forms the API serves

Measured with the parameters section 4's table binds, five runs each, median reported. Unbound these
run over seven years and miss 100 ms by three orders of magnitude on a database with nothing wrong
with it, so the parameters are half the measurement: `:symbol = AAPL`, `:start = 2026-04-01`,
`:end = 2026-06-30`.

| query | runs (ms) | median | root blocks | target | |
| --- | --- | ---: | ---: | ---: | --- |
| `01_volatility.sql` | 58 53 46 48 49 | **49.0 ms** | 423 | <100 ms | pass |
| `03_gaps.sql` | 27 27 27 26 28 | **26.8 ms** | 431 | <100 ms | pass |
| `06_daily_rollup.sql` | 27 29 28 26 27 | **26.7 ms** | 420 | <100 ms | pass |

The run column is the harness's console output rounded to whole milliseconds while the median is
computed from the unrounded values, so a verifier recomputing a median from the printed column and
getting 27 rather than 26.8 has not found a defect.

Roughly 420 blocks each — 3.28 MiB — because partition pruning takes this 90-day window down to
three monthly partitions and the PK then serves one symbol out of them.

**Against the same constructed before Class B uses** — `enable_indexscan`, `enable_bitmapscan` and
`enable_indexonlyscan` all off, which is the only way to get an untuned state for a mechanism that
has existed since Feature 1:

| query | before (constructed) | after | ratio |
| --- | ---: | ---: | ---: |
| `01_volatility.sql` | 25,088 | 423 | **59.31×** |
| `03_gaps.sql` | 25,117 | 431 | **58.28×** |
| `06_daily_rollup.sql` | 25,085 | 420 | **59.73×** |
| `02_correlation.sql` (Class B) | 25,125 | 830 | 30.27× |

Every before plan is three parallel sequential scans plus a sequential scan on `market_days`; every
after plan is an index path over the three pruned children.

**Class A's block reduction is roughly double Class B's**, and that is the whole basis of the class
split: these read one symbol where query 2 reads two. Selectivity is what the index buys, and halving
the symbols buys very nearly twice as much — 59.31 ÷ 30.27 = **1.96**, where the mechanism predicts
2. Class A is not *gated* on this ratio — its target is the 100 ms above — but 6.3 #14 asks for a
before/after on all ten queries, not only the one with a block target.

The first run of a cold sweep measured query 1 at **312 ms** on the same plan and the same 423
blocks, differing only in what the page cache held. That is why the table reports a median of five
rather than a single number.

**Two further five-run sweeps say the same thing, and the contrast between what moved and what did
not is the point.** The root blocks came back **identical to the block** in both, while every median
moved and the Class B wall-clock ratio read 7.19× and then 7.87× against the 7.00× above:

| | table above | re-run | re-run |
| --- | ---: | ---: | ---: |
| `01_volatility.sql` | 49.0 ms | 53.0 ms | 55.2 ms |
| `03_gaps.sql` | 26.8 ms | 28.2 ms | 25.6 ms |
| `06_daily_rollup.sql` | 26.7 ms | 30.0 ms | 29.6 ms |
| query 2, wall-clock ratio | 7.00× | 7.19× | 7.87× |

The medians above stand as the original measurement; these are a different day's cache, recorded as
corroboration. All three pass on all three sweeps, and no block count moved across any of them.

### The endpoint forms — `/bars` per-page cost and the `/daily` wrapper

Measured 2026-09-07 against the same database, read-only: `EXPLAIN (ANALYZE, BUFFERS, FORMAT
JSON)` on the exact statements the endpoints run, five runs each, median reported. Corrected and
extended 2026-09-08 by a second read-only pass, which is what the planning columns and the
`fetch = 1,001` row for `/daily` come from.

#### `/bars` — three windows, because two of them control the smaller variable

`BARS_PAGE_DEFAULT` and `BARS_PAGE_MAX` govern `/bars` over the whole ingested span: 71 monthly
partitions, four carrying the hot-window partial index and 67 not. So per-page cost is measured on
two axes, the index set and the number of partitions a request opens.

| window | parameters | days | partitions | index the planner chose |
| --- | --- | ---: | ---: | --- |
| W-HOT | `AAPL`, 2026-04-01 → 2026-06-30 | 90 | 3 | per-partition `_pkey` |
| W-COLD | `AAPL`, 2025-04-01 → 2025-06-30 | 90 | 3 | per-partition `_pkey` |
| W-WIDE | `AAPL`, 2020-08-01 → 2026-06-30 | 2,159 | 71 | per-partition `_pkey` |

**Every row below is page 1**, bound with the endpoint's own first-page sentinel.

| window | fetch = 101 | fetch = 1,001 | fetch = 10,001 | planning |
| --- | ---: | ---: | ---: | ---: |
| W-HOT | **5** blocks, 0.04 ms | **19**, 0.32 ms | **168**, 2.45 ms | 36 blocks, 0.11 ms |
| W-COLD | **5** blocks, 0.03 ms | **19**, 0.25 ms | **401**, 10.18 ms | 36 blocks, 0.11 ms |
| W-WIDE | **5** blocks, 0.04 ms | **19**, 0.23 ms | **167**, 2.39 ms | 852 blocks, 1.62 ms |

`fetch` is the page limit plus one, so 1,001 is `BARS_PAGE_DEFAULT` and 10,001 is `BARS_PAGE_MAX`.
Planning does not vary with `fetch`. One execution cell is not invariant and it is the one the cap is
set from: W-COLD at fetch = 10,001 read `404, 401, 401, 401, 401` — the first run's cold cache moved
the blocks, not only the milliseconds. The published 401 is the median and reproduced five times out
of five.

**The planner chose the per-partition primary key on all three windows and never a hot-window
index, measured across all five runs of every window.** That is the load-bearing fact about
reproducibility: the four `bars_2026_0N_hot_idx` partial indexes exist in no migration and no
`db/schema.sql`, so a number depending on one would hold here and nowhere else. None of these does.

**Planning is elevated in three of nine cells, and the reason is not the obvious one.** The three
`fetch = 101` cells are the first time each *window* was planned on the connection, not the first
time the statement was — all nine run the same statement text, since `fetch`, `lo` and `hi` are bind
parameters. What is new is the set of partitions the window opens, and the excess is exactly **6
blocks per newly-planned child**: W-COLD adds 3 children, `54 − 36 = 18`; W-WIDE adds the remaining
65, `1242 − 852 = 390`. Both divide to 6.0.

**Opening 71 partitions instead of three costs a default page 15.8× more blocks, and it is setup
rather than work.**

| window | partitions | planning | execution at fetch = 1,001 | whole page |
| --- | ---: | ---: | ---: | ---: |
| W-HOT | 3 | 36 blocks | 19 blocks | **55 blocks** |
| W-COLD | 3 | 36 blocks | 19 blocks | **55 blocks** |
| W-WIDE | 71 | 852 blocks | 19 blocks | **871 blocks** |

The execution halves are within one block of each other at the two smaller fetches, and not because
the plan descends into every partition. An ordered `Append` under a bound `LIMIT` stops once the page
is full: the saved W-WIDE plan executes exactly **one** of its 71 children and lists the other 70 as
`(never executed)`. The partition count moves planning, which is 852 blocks — **23.7× the planning
of a three-partition window** and 7.0× W-WIDE's own execution time. A page whose execution is 0.23 ms
spends 1.62 ms being planned.

**Planning has three states, not two:** steady **852**; **1,242** when the connection is warm but
this window's partitions are new to it; and **6,348** on a brand-new connection, which reproduced on
five fresh connections. Their times do not reproduce as narrowly — 6.40 to 10.55 ms, median 7.17 —
so take the blocks as the measurement and the milliseconds as an order of magnitude.

**All three are a custom plan's, and a pooled connection can leave that regime.** `api/deps.py` sets
no `prepare_threshold`, so psycopg prepares after five executions and Postgres can switch to a
generic plan from the eleventh. Measured: the generic plan over W-WIDE plans **8** blocks for page 1
against the custom plan's 852. So 852 and 1,242 are what a fresh or lightly used connection pays —
the state every figure here was captured in — not a standing per-page cost for every pooled
connection.

**Verdict: `BARS_PAGE_DEFAULT = 1000` and `BARS_PAGE_MAX = 10000` are confirmed, not moved.** On
execution blocks the worst window at the cap is **W-COLD at 401**; on whole-page blocks it is
**W-WIDE at 1,019** against W-COLD's 437. At the default a page costs 19 execution blocks — 152 KiB
— on every window; at the cap the worst execution page costs 401 blocks, 3.13 MiB, in 10.2 ms.
Neither is near a limit worth lowering a cap for, and raising it has no measurement asking for it.

#### `/daily` — the wrapper, and the endpoint number Class A's table does not carry

The endpoint wraps `06_daily_rollup.sql` in an outer `SELECT … ORDER BY day LIMIT`, and a page
narrows the committed query's own `:start` from the cursor rather than filtering its output.
Measured at W-HOT's parameters, page 1:

| fetch | root blocks | median ms | rows returned |
| ---: | ---: | ---: | ---: |
| 51 | 414 | 24.08 | 51 |
| 65 | 414 | 24.51 | 62 |
| 101 | 414 | 24.05 | 62 |
| 1,001 | 414 | 24.36 | 62 |

**The prediction that cost is flat in the limit is confirmed**: the aggregation spans the window, not
the page, so 414 blocks is the whole window's cost at every limit measured.

This window holds 62 trading days. The bound is not 62: `AGG_MAX_WINDOW_DAYS = 90` is checked as
`(end - start).days > 90`, so a legal window spans at most 91 calendar days, and measured against all
1,484 rows of `market_days` the maximum is **64 trading days**. That is below `AGG_PAGE_DEFAULT =
100`, so no legal `/daily` window can fill a page at the default and the aggregating caps are not
what bounds this endpoint's cost. Feature 7 confirmed both caps on `/analytics/largest-moves`, the
one endpoint reading them that can fill a page at the default.

Planning, excluded from the block counts above as it is for `/bars`: **571 blocks, 1.93 ms**, flat
across every fetch, so a warm whole page is 985 blocks. **On a connection that has not planned the
statement it is 6,149** — **5,729 planning blocks** on each of three fresh connections against 420
execution blocks, **6.2× the warm figure**, the same shape as `/bars`'s 6,348. **The <100 ms Class A
comparison below is therefore a warm-connection number**: a pooled connection's first request costs
roughly 6 ms of planning rather than 1.93, still inside the target.

**The wrapper costs zero blocks, not six.** Measured like for like in one session, the unwrapped file
reads `420, 414, 414, 414, 414` and the wrapper reads `414` five times out of five; the six-block
difference is the first-execution catalog read, not the outer `LIMIT` and `ORDER BY`.

**The Class A number for the `/daily` rollup path in its endpoint form is 24.05–24.51 ms execution**
— 22.95–28.41 ms across the twenty underlying runs, about 26.3 ms once warm planning is counted —
against the <100 ms target.

**Narrowing against filtering, on the same page 2.** Page 1 at limit 30 returns its 30th row on
2026-05-13, so page 2 binds `:start = 2026-05-14`; the alternative leaves `:start` at 2026-04-01 and
cuts the page with an outer `WHERE day > '2026-05-13'`. **Both rows are a single observation each.**

| page 2, fetch = 31 | root blocks | ms (n=1) | partitions | whole-page blocks | whole-page ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| narrowing `:start` | **221** | 14.08 | 2 | **788** | 16.503 |
| outer `WHERE day >` | **414** | 18.09 | 3 | **988** | 20.216 |

`:start` drives the scan bound inside the committed query while the outer predicate throws away rows
the aggregate has already built. Read whole-page, that is **20.2% fewer blocks and 1.22× faster**,
not the 47% and 1.3× execution alone suggests — planning barely moves between the two forms, since
both plan the same query over nearly the same children. Narrowing is still the right design. The
partition count is an observation rather than a target: narrowing removes a child only on pages that
cross a month boundary, which this one does.

### The analytics endpoint forms — Class A, the page caps and the deep page

`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on the exact statements the three analytics endpoints run,
one recorded warm-up then five runs each, median reported. **Conditions this subsection does not
inherit:** each harness ran in one psycopg session with automatic statement preparation off, so every
run is planned the way an endpoint's first executions on a connection are; block counts are execution
only, with planning in its own column; every time labelled HTTP is end-to-end through the compose
`app` service, and every other time is SQL. **Every `/analytics/largest-moves` figure is for the
statement as it is now, at `min_move_pct = 0`** unless it says otherwise.

**What `min_move_pct = 0` returns, exactly.** Every regular-session bar in the window **whose open
and close are both a real number** — a deviation from "no filter at all" worth stating, because
`db/schema.sql` declares `open` and `close` as bare `numeric` and the move is a percentage of the
open, so three values the database accepts have no answer and each fails differently. A **zero** open
is `division by zero`, a 500 for the page that reaches it rather than one missing row. A **NULL** open
makes the comparison NULL, so the row is dropped with or without a guard — "not zero" does not
describe it. A **NaN** in either column passes both `<> 0` and `abs(...) >= threshold`, because
Postgres orders NaN above every number, and then reaches the wire as the bare token `NaN`, which no
JSON parser accepts: one such row costs a client the whole page. The statement therefore carries
`AND b.open <> 0 AND b.open <> 'NaN'::numeric AND b.close <> 'NaN'::numeric`, and
`db/queries/05_largest_moves.sql` carries the same three.

The ingest refuses a zero open and is the only thing that does; it does not cleanly refuse a NaN,
since `Decimal('NaN') <= 0` raises rather than returning False. **None of the three exists in the
loaded database**, measured over all 41,668,537 rows, so no figure here moves either way. Adding the
two NaN clauses leaves the plan structurally identical node for node — they join a `Filter` that was
already there.

Bound at `:symbol = AAPL`, `:start = 2026-04-01`, `:end = 2026-06-30`. `/analytics/largest-moves`
takes no symbol and binds `min_move_pct = 0` with `limit = AGG_PAGE_DEFAULT`.

| endpoint | runs (ms) | median | execution blocks | planning | target |
| --- | --- | ---: | ---: | ---: | ---: |
| `/analytics/volatility` | 52.37 47.54 47.82 48.11 48.27 | **48.11 ms** | 414 | 571 blocks, 2.52 ms | <100 ms |
| `/analytics/gaps` | 27.35 29.11 24.34 26.17 24.26 | **26.17 ms** | 418 | 574 blocks, 2.49 ms | <100 ms |
| `/analytics/largest-moves` | 0.921 0.921 0.922 0.935 1.945 | **0.922 ms** | 9 | 39 blocks, 0.18 ms | <100 ms |

**All three medians pass; `/analytics/volatility`'s first run did not.** First statement on a fresh
connection, it read **108.17 ms** at **423** execution blocks against the 414 its median settles at,
plus **4,875 planning blocks**. The gate is the five-run median, as for every Class A row — so like
`/daily`'s, these are warm-connection numbers.

#### `/analytics/largest-moves` — the page caps

`AGG_PAGE_DEFAULT` and `AGG_PAGE_MAX` govern `/daily` and all three analytics endpoints. `/daily`
cannot fill a page at either and volatility and gaps take no page at all, so the pair is set from
`/analytics/largest-moves`, measured on page 1 over two windows. W-HOT's children carry the
hot-window partial index; W-COLD's carry none, so the planner reads each child's `_ts_symbol_idx`
with an `Index Scan` and visits the heap for `open` and `close`.

| window | fetch = 101 | fetch = 1,001 | planning |
| --- | ---: | ---: | ---: |
| W-HOT, 2026-04-01 → 2026-06-30 | **9** blocks, 0.922 ms | **14** blocks, 5.811 ms | 39 blocks |
| W-COLD, 2025-04-01 → 2025-06-30 | **121** blocks, 0.666 ms | **1,024** blocks, 5.624 ms | 36 blocks |

Every cell is a `Nested Loop` over the ordered per-partition scans and none carries a `Sort`.

**Verdict: `AGG_PAGE_DEFAULT = 100` and `AGG_PAGE_MAX = 1000` are confirmed, not moved.** At
`min_move_pct = 0`, page 1's worst reading at the cap is **W-COLD at 1,024 execution blocks** — 8.00
MiB — in 5.62 ms, and 1,060 whole-page; at the default its worst is 121 and 157. A cursor at a
session's last minute reads a little more on W-COLD (1,048–1,053 at the cap) and nothing more on
W-HOT, the extra being the out-of-session run the scan crosses. How deep the cursor sits was not
varied. Neither reading is near a limit worth lowering a cap for, and raising it has no measurement
asking for it.

**The caps bound the rows a page returns, not the rows it reads, and a positive threshold separates
the two.** At `min_move_pct = 0` every bar passes, so the loop stops once `limit + 1` bars have
joined; a rarer threshold makes the same loop read on until it has found that many. On W-HOT page 1
at the default limit, at `min_move_pct = 1` the page read **3,208 blocks in 128.3 ms** to return 101
rows; at `min_move_pct = 100`, which no bar in the window meets, it read every bar in the window
through the hot-window index — **12,039 blocks in 518.1 ms** — and returned none.

**That is W-HOT's cost, and 67 of the 71 partitions carry no hot-window index.** On W-COLD's three,
at `min_move_pct = 100` the plan falls back to a plain `Index Scan` visiting the heap on every row,
and reads **1,737,160 execution blocks** to return 0 rows; at `min_move_pct = 1` it reads **126,182
blocks**. The other 64 unindexed partitions are unmeasured, and none of those timings is a median.

`min_move_pct` is unbounded, so in place of a bound on the parameter a request is bounded by the
timeouts below — and a threshold scan over a window without the hot-window index is the one
statement published here that can reach them.

#### The bounds a request runs under

Four bounds sit between a client and the database:

| bound | constant | value | what the client sees when it is reached |
| --- | --- | ---: | --- |
| waiting for a pooled connection | `POOL_CHECKOUT_TIMEOUT_SECONDS` | 5 s | 500 `internal` |
| one statement on a pooled connection | `STATEMENT_TIMEOUT_SECONDS` | 5 s | 500 `internal` |
| the check that connection answers before it is handed out | `POOL_CHECK_TIMEOUT_SECONDS` | 1 s | that connection is discarded and replaced |
| `/health`'s own probe | `HEALTH_TIMEOUT_SECONDS` | 2 s | 500 `internal` |

**The first and third compose, so a checkout is bounded at about six seconds and not at five.** In
`psycopg_pool` only the wait is given the checkout deadline; the check that follows it is given
nothing, which is why the check carries a deadline of its own here. That six seconds is arithmetic
over two published constants rather than a measurement — nothing in the suite bounds the two
composed. What was measured is the case where the wait is short and the check is what costs:
6.03-6.16 s.

**A threshold scan can reach the statement bound, and it takes a cold cache to do it.** The two
W-COLD readings above, 1,508.8 ms and 943.6 ms for a 1,737,160-block scan, were taken against a warm
page cache. Against a cold one they are not. On a scratch replica of W-COLD's shape, restarted and
with its data files evicted before each reading, `EXPLAIN ANALYZE` read **4,862.7, 5,257.9 and
5,371.9 ms**, of which 3.84-4.16 s was disk. Through the service at `min_move_pct = 100`, the first
request after each of three cold starts answered **200 in 3.12 s, 500 `internal` in 5.215 s, and 200
in 3.47 s**; ten concurrent cold requests all answered **500, at 5.07-5.19 s**. So the statement
bound is not comfortably clear of this statement — it is what decides whether a cold threshold scan
is served or refused. A cold request against the loaded database was not measured; the replica is
the evidence.

**Sharing a check means a success can be as stale as the check that produced it.** Overlapping
`/health` probes wait on one check rather than opening a connection each. Measured against a peer
that held the check's answer for 1.5 s, a probe arriving 1.0 s into a check was answered **200**
although the database had stopped accepting connections 0.5 s in. A success is stale by at most
`HEALTH_TIMEOUT_SECONDS`.

**What that leaves a client.** `min_move_pct` takes no bound: the caps bound rows returned and this
parameter decides rows read. So a 500 at a high threshold is a timeout rather than a bad request and
carries no information about the request's validity; a retry has a good chance, because the refused
attempt leaves the window in cache; and concurrency is the case retrying does not fix, so a client
that needs the answer reliably narrows the window rather than raising the threshold.

#### Why a page is usually an index descent, and where it still is not

**The verdict above rests on a change to the statement, measured both ways at `min_move_pct = 0`.**
As first shipped, the endpoint joined `bars` to `market_days` the way `06_daily_rollup.sql` does: a
New York date equality plus the half-open session bounds. The planner estimated that join badly — at
page 1 with `fetch = 1,001` it put it at **1,280 rows per process** where the three processes
returned **669,829** each. The equality also made the join hashable, so past a threshold the planner
gave up the ordered nested loop, which stops after one page, for a **hash join that reads every row
left in the window and sorts them to return one page**. The threshold moved with the cursor's
position, which is why a test pinned at one depth never saw it.

| reading | as first shipped | joined on the session bounds alone |
| --- | ---: | ---: |
| page 1, fetch = 101 | 524 blocks, 0.252 ms | 9 blocks, 0.922 ms |
| page 1, fetch = 1,001 | 25,035 blocks, 840.11 ms | **14 blocks**, 5.811 ms |
| 1,000,000 rows in, fetch = 101 | 307 blocks, 0.175 ms | 8 blocks, 0.581 ms |
| cursor 2026-06-25 13:30Z, fetch = 101 | 822 blocks, 139.19 ms | 9 blocks, 0.627 ms |

Over HTTP at the default limit the 2026-06-25 page answered in **139.14 ms, 28.97× page 1**, before
the change and 3.02 ms after it.

`06_daily_rollup.sql` keeps the equality on purpose — its own comment says the equality is what gives
the planner a hash — because it aggregates a whole window, which a keyset page never does. **The
equality is safe to delete because the bounds imply it**: every one of the **1,484** sessions in
`market_days` opens and closes inside its own New York date, checked rather than assumed. With no
equality clause neither a hash join nor a merge join is available to the planner.

**The suite holds this two ways, because one is not enough.** A plan-shape test asserts no hash join
on a fixture where the rollup's own equality produces one — but that catches the equality in one
spelling only, and a UTC date cast plans like the shipped statement on a small fixture. So the join
is pinned by its **text** as well, read the way Postgres reads identifiers (comments dropped, names
folded, quoting normalised): the join must be the half-open pair alone, with no second reference to
a `market_days` column anywhere in the statement. A respelling is refused as the reference it is
rather than as the characters it is written with.

The change trades index probes for an in-memory filter: the equality let the loop probe
`market_days_pkey` once per bar — 519 of the first-shipped page 1's 524 blocks were those probes —
where the loop now filters each bar against the window's materialised sessions. Blocks fall in every
row of the table while the `EXPLAIN ANALYZE` times rise in two of them; timed from the client the
rise is far smaller, and over HTTP page 1's p50 went 4.80 ms to 3.33 and the deep page's 4.18 to
3.74. Every one is far inside the Class A target.

**The fix is not universal, and the exception is a window's own last session when that session also
ends a partition without the hot index.** Such a cursor leaves the ordered plan for a
`Bitmap Heap Scan` with a top-N `Sort`, over the closing slice of that session only. **The condition is an
inference, not a measured rule**: five windows were swept, two flip and three do not, and only one
of the three non-flipping windows tests the partition-end half. Nothing isolates that half from the
missing index. The flip is not confined to `min_move_pct = 0` — the sweep ran at 0 and at 1 and the
two thresholds flip identically, cell for cell.

| window swept | last session | flips? | positions flipped of 79, at fetch 101 / 251 / 1,001 |
| --- | --- | --- | --- |
| W-COLD 2025-04-01..06-30 | 2025-06-30, ends its partition | yes | 11 / 22 / 51 |
| 2020-08-01..10-30 | 2020-10-30, ends its partition | yes | 8 / 17 / 42 |
| W-HOT 2026-04-01..06-30 | in a hot partition | no | 0 |
| 2025-10-15..2026-01-12 | not its partition's last day | no | 0 |
| 2026-02-01..04-30 | in the hot `bars_2026_04` | no | 0 |

Month-end sessions that are not a window's own last session do not flip either: 0 of 1,551 cells
over seven such sessions in three cold windows. The cause is a second estimate the join change did
not touch — the planner multiplies the redundant `b.ts >= after_ts` bound's selectivity by the row
comparison's, so at 2025-06-30 19:00Z the bitmap index scan is estimated at 43 rows where the heap
scan returns 4,731, and a bitmap feeding a sort the planner estimates at 98 rows looks cheaper than
the ordered scan. The sort stays bounded — it sees only the rows left in that final session. Section
4's join predicate is kept as written; this tail is documented rather than designed around, and it
is why 6.3 #13's condition (d) is checked at its one pinned window rather than asserted for every
window a client could request.

#### The deep page — keyset at `DEEP_PAGE_DEPTH`

The cursor was derived by `OFFSET 1000000 LIMIT 1` over the endpoint's own statement, and all five
runs named the same row — `2026-05-15T15:35:00+00:00`, `CVX` — with exactly 1,000,000 rows before it
in `(ts, symbol)` order.

| page | HTTP runs (ms), 11 interleaved rounds | HTTP p50 |
| --- | --- | ---: |
| page 1 | 59.3 3.6 21.7 3.3 3.3 3.8 3.1 3.6 2.8 3.3 3.0 | **3.33 ms** |
| 1,000,000 rows in | 8.0 4.9 3.7 2.9 4.9 4.2 3.1 3.8 2.6 3.1 3.2 | **3.74 ms** |
| cursor 2026-06-25 13:30Z | 3.8 3.8 4.6 2.7 6.8 2.9 2.9 2.9 3.0 3.0 3.3 | **3.02 ms** |

**6.3 #13 passes.** (a) the cursor is 1,000,000 rows in by construction; (b) every deep response held
100 rows and a non-null `next_cursor`, asserted on all eleven.

**(c) is published both ways, because the two readings disagree.** Read as p50 against p50 — the
reading the owner fixed — the deep page is **1.125×** page 1 against a target of under 1.5×, and a
re-measurement reads **1.050×**. Read per request instead, the first deep run of each series is its
slowest, 8.015 ms recorded and 10.382 ms re-measured, and exceeds 1.5× the page-1 p50 on its own.
Under the fixed reading that run sits inside the p50 statistic; under the per-request reading the
gate would not pass. Both are here so the verdict can be read against either.

**(d) At the deep cursor the plan has no Sort.** A plain `Append` over **2 children** reads each
through an `Index Only Scan` on its own `hot_idx` with 0 heap fetches; only the first runs, because
the page fills from `bars_2026_05` alone. Execution is 8 blocks. With the redundant `b.ts >=
after_ts` bound deleted the same statement plans **all 71 children**. Neither form prints a
`Subplans Removed` line, and the cause is the bound's type under a **custom** plan: `_instant_bounds`
binds a `timestamptz`, which the planner folds into a range and prunes by at plan time, so a pruned
child never enters the plan to be counted. Under a generic plan the same bound is not known at
planning time and the pruning moves to executor init, where it **is** counted — which is why the
generic `/bars` plan below prints `Subplans Removed: 70`, and why the date-bound `01_volatility.sql`
and `03_gaps.sql` print `Subplans Removed: 68`. **Counting the children that survived is the
measurement.** `_MOVES_SQL` itself stayed on a custom plan on 0 of 41 executions past psycopg's
five-execution threshold, so this is the plan the endpoint actually serves.

#### The offset number

All three timed from the client over SQL, on one connection:

| statement | runs (ms, client) | median | blocks |
| --- | --- | ---: | ---: |
| keyset page 1, fetch = 101 | 1.397 1.199 1.195 1.200 1.199 | **1.199 ms** | 9 |
| keyset page 1,000,000 rows in, fetch = 101 | 3.095 1.136 1.025 1.996 1.134 | **1.136 ms** | 8 |
| `OFFSET 1000000 LIMIT 1` over the endpoint's statement | 5,224 2,243 2,061 2,020 2,175 | **2,175 ms** | 6,000 |

**The same depth reached by `OFFSET` costs 1,915 times the time of the keyset page at that depth and
750 times its blocks** — and the `OFFSET` statement returns one row where the keyset page returns
101, so the comparison favours it. The two series are not on the same footing: the `OFFSET` median
carries a cold first execution, 5,224 ms, where the keyset rows had already run seven times at the
same cursor. **Dropping every series' first run leaves 1,866×.**

As first shipped, the same `OFFSET` statement ran as a hash join over sequential scans of three
children with an external merge sort that spilled, at a median of 991 ms over **25,035 blocks**. The
join change made the `OFFSET` form 2.2× slower and 4.2× cheaper in blocks.

#### The single-symbol deep page, at its real maximum

`/symbols/AAPL/bars` over the whole span holds **576,962** raw bars, so with 575,961 rows before the
cursor the deepest page holds exactly the final 1,000 and has no successor.

| page | execution | planning | children | HTTP p50, 7 interleaved rounds |
| --- | ---: | ---: | ---: | ---: |
| page 1 | 19 blocks, 0.366 ms | 852 blocks | 71 | 22.12 ms |
| deepest | 20 blocks, 0.361 ms | 8 blocks | 1 | 21.30 ms |

Flat: the deepest page's HTTP p50 is **0.963×** page 1's, and a re-measurement reads 0.925×.
Planning is the axis that moves, in the deep page's favour — 852 blocks against 8. **On a connection
shaped like the pool's the plan changes**: psycopg prepares after five executions and from the
eleventh Postgres serves a generic plan — `Index Scan` on the surviving child, `Subplans Removed:
70`, no sort, 20 execution blocks and 0 planning blocks. Client time falls with it, from a 6.025 ms
median to 4.492, 2.772 and 2.759 ms.

**First runs.** Each `EXPLAIN` median above excludes a recorded first run. Three carry information:
`/analytics/volatility` on a fresh connection read **108.174 ms** against its 48.107 ms median, which
is above the <100 ms Class A target and is why that target is stated as a five-run median;
largest-moves at `min_move_pct = 1` read **402.414 ms** against 128.316; and at `min_move_pct = 100`
**1,590.699 ms** against 518.112.

**What this hands to the deployed copy.** Condition (d) has to be re-established on RDS by the
relative comparison — the statement as written against the same statement with the redundant bound
deleted — because a child count under 71 is not the test: a statement missing the bound also passes
that on any window ending before 2026-06. The hot-window index has to be recreated by hand, and the
session join stays on the bounds alone.

## Class B — query 2, the one query with a selective filter

Query 2 touches 2 symbols of 100 — the only query in the set where an index can make it **read fewer
rows**, which is why it is the only one asked for an order of magnitude.

**The "before" is constructed, and is labelled as constructed.** Query 2's mechanism is the PK, which
has existed since Feature 1, so there is no untuned state to measure. Section 4 mandates
`enable_indexscan=off; enable_bitmapscan=off; enable_indexonlyscan=off` in the same session rather
than dropping and rebuilding a primary key over 41.7M rows to arrive at the same number. Bound at
`:symbol_a = AAPL`, `:symbol_b = MSFT`, over the same window Class A uses.

| | root blocks | median exec (5 runs) | root node |
| --- | ---: | ---: | --- |
| simulated before | 25,125 | 561.8 ms | `WindowAgg` |
| after | **830** | 80.3 ms | `WindowAgg` |
| **ratio** | **30.27×** | 7.00× | |

**Blocks 30.27× against a ≥10× target — passes**, and two later five-run sweeps read the same 25,125
and 830 to the block while their wall-clock ratios came out at 7.19× and 7.87×. That is this
section's argument demonstrating itself: **a 10× target read off the stopwatch fails this query at
7.00× on the same runs where its block ratio passes three times over**, and a colder cache would have
moved that verdict again without a single block changing.

The number is only meaningful because of the heap it was measured on. On a day-major heap — a fixture
measurement from the design work, since reproducing it here would mean rewriting 41.7M rows — the
same query and the same index measure **1.13–1.42×** and the gate fails. See
[The heap the numbers rest on](#the-heap-the-numbers-rest-on).

## The other two index decisions

### The hot-window partial index — kept

Created on the four recent **child** partitions directly, never on the parent. The cutoff is
`date_trunc('month', INGEST_END − (HOT_WINDOW_MONTHS − 1) months)` with `HOT_WINDOW_MONTHS = 4` — the
four most recent **whole** months. The naive reading, `INGEST_END` − 4 months with no truncation,
gives `2026-02-28` and pulls `bars_2026_02` in instead; a restore or an RDS copy has to re-derive
this cutoff and needs the whole-month rule to land on the same four partitions.

```sql
CREATE INDEX bars_2026_0N_hot_idx ON bars_2026_0N (ts, symbol) INCLUDE (open, close)
  WHERE ts >= TIMESTAMPTZ '2026-03-01 00:00:00+00';
```

Measured against the plain `(ts, symbol)` index the children inherit, on the access path
`/analytics/largest-moves` uses — universe-wide, ordered `(ts, symbol)`, reading only `open` and
`close`:

| | root blocks | index-only scans | plain index scans |
| --- | ---: | ---: | ---: |
| inherited `(ts, symbol)` | 1,012 | 0 | 4 |
| with the partial index | **16** | **4** | 0 |

**63.25× fewer blocks, and the planner chose it unprompted.** **134,217,728 bytes across four
partitions**, built in 8 s — exactly 128 MiB over exactly 16,384 pages, so the per-partition mean is
exactly 32.0 MiB. The four are not equal: 32,022,528 bytes on `bars_2026_05` to 35,119,104 on
`bars_2026_03`, and the mean is the only round number among them.

**Kept, and the verdict is read off the endpoint.** On `/analytics/largest-moves` the planner chose
each child's `hot_idx` as an `Index Only Scan` with 0 heap fetches — **9 execution blocks at the
default page, 14 at the cap**, 8 at the deep cursor. A positive threshold reads further through the
same index: 3,208 blocks at 1%, and **12,039** for a threshold no bar meets. **On the one window
without the hot index that was measured, the same thresholds read 126,182 and 1,737,160 blocks**,
because there the planner has only the plain index and must visit the heap for `open` and `close`;
at `min_move_pct = 0` that window reads 121 blocks at the default and 1,024 at the cap. The saving is
in blocks, not time — warm, 0.922 ms against 0.666 ms at the default. W-COLD is a different quarter's
data as well as a different index set, so the table above stays the like-for-like measurement and the
endpoint agrees with it in direction. **No `/bars` page measured has used the index**, so this
endpoint is its whole case.

**It holds only while the session join stays on the bounds alone.** With the day equality back, the
page at the cap leaves this index for sequential scans. An index on four children is not inherited by
a partition created later, so keeping it means recreating it by hand whenever the window moves.

Two things about the placement are rules rather than preferences, both verified. Creating this on the
*parent* propagates the predicate to every child, so every pre-2026 partition gets a partial index no
row can satisfy — and the propagated copies then **cannot be dropped individually** (`SQLSTATE
2BP01`): the parent mistake is all-or-nothing, not prune-later. And because the index lives on
children, `CREATE TABLE ... (LIKE bars INCLUDING ALL)` copies **no** partial index, so a copy arrives
missing exactly the index the hot window exists for.

The predicate is an explicit `timestamptz` and not `DATE '2026-03-01'`. Comparing a `timestamptz` to
a `date` coerces through the session zone, which is not `IMMUTABLE`, and Postgres refuses it outright
— *functions in index predicate must be marked IMMUTABLE*.

### BRIN on `ts` — measured, and not kept

The honest expectation up front was that monthly partitioning already prunes by time, so BRIN's win
would be **index size, not query time**. That is what it turned out to be. Measured on
`bars_2026_06`, a real partition of 699,245 rows — never a fixture, because BRIN has a fixed floor of
about 24 kB and on a small table comes out *larger* than a b-tree:

| | bytes | |
| --- | ---: | --- |
| heap | 70,688,768 | |
| b-tree PK | 22,331,392 | |
| BRIN on `ts` | **24,576** | **908.7× smaller than the b-tree** |

24,576 bytes is the floor itself. On a five-day range probe the planner **did not choose it**: free
choice was a sequential scan at 32.93 ms and 8,629 blocks. Forced onto BRIN it read 2,532 blocks —
genuinely fewer — and took **367.89 ms, 11× slower** (one run each; the blocks are the durable half),
because a BRIN range scan rechecks every tuple in every block its summary admits. Dropped. The
partition key already does BRIN's job here, and a 24 kB index the planner never picks is worth
knowing about rather than carrying.

### What is deliberately not indexed

Nothing on `volume`. Queries 4 and 8 are the only two that filter or rank on it, both read 100% of
the universe, and each was measured with its own volume index on disk and read the same blocks with
it as without. An index that is never chosen is not free — it is maintained on every one of 41.7M
inserts.
