# Query performance

Ten analytical queries over 41,668,537 minute bars in 71 monthly partitions, measured on the
database the pipeline loaded. Every number below was taken on this machine, at the settings
named in [Measurement conditions](#measurement-conditions), with the parameters named beside
it. A latency figure without its parameters is not reproducible and not comparable to the next
one, so the parameters are part of every row.

The full `EXPLAIN (ANALYZE, BUFFERS)` output behind the before/after and candidate-index tables
— twenty plans, one file per variant with its bound parameters and session settings in the header
— is kept alongside this document rather than quoted into it. For those, the tables are summaries
and the plans are the evidence. The coverage-variant table comes from a separate harness run
whose output is kept beside it. The hot-index and BRIN tables are one exception: their harness
wrote to a path that no longer exists, so those two tables are the record of that measurement
rather than a summary of one. **Those two tables also differ in what re-running them would cost.**
BRIN's rows can be retaken, because that index was built for the measurement and dropped after it.
The hot-index table's 1,012-block "before" row cannot: it is the reading taken against the plain
inherited index, and the four partial indexes it is compared with are still on this database — no
`DROP INDEX` has been run — so retaking that row means dropping four live indexes first, and
re-running the harness with them in place measures the 16-block "with" row twice instead. That
table is the hot-window index's one like-for-like measurement, and the index exists in no migration,
so a restore has to recreate it by hand — which is the reason to record here that the comparison
behind the 63.25× cannot be retaken as the database stands.
The Class A/B wall-clock re-run sweeps are a second exception: that harness overwrites a single
output file on every run, so only the most recent survives as an artifact —
the second sweep, 55.2, 25.6 and 29.6 ms with a 7.87× ratio for query 2. The main table's 49.0,
26.8 and 26.7 ms with 7.00×, and the first re-run's 53.0, 28.2 and 30.0 ms with 7.19×, survive
only as prose in this document. None of the gated block counts depend on the missing files —
423, 431, 420, 830 and 25,125 triangulate exactly across the twenty captured plans, the
surviving re-run and the narrative.

## What is being measured, and why it is mostly not wall-clock

The ten queries do not share a target, because they do not share a shape, and one target
applied to all ten would be wrong for at least seven of them.

| class | queries | what an index can do | target |
| --- | --- | --- | --- |
| **A** | 1, 3, 6 | serve a bounded window from an index | **< 100 ms** |
| **B** | 2 | let the query **read fewer rows** — it touches 2 symbols of 100 | **≥ 10× fewer blocks** |
| **C** | 4, 5, 7, 8, 9, 10 | nothing: they read every row by definition | **evidence of optimality** |

Class B is gated on **blocks touched**, not on time. Blocks are a property of the plan: the
same number on a warm cache, a cold cache, a laptop, or RDS. Wall-clock is not — the one query
gated here reads **30.27× fewer blocks** and measures **7.00× faster**, and two later sweeps read
the same blocks to the digit while the wall-clock half moved to 7.19× and 7.87×. Tuning until the
stopwatch says 10× is tuning the page cache. Wall-clock is recorded beside it and gated on
nothing.

Class C's target is not a speedup because there is no speedup to be had, and demanding one
would push a builder into adding an index that is never chosen and reporting the difference as
a result. Each Class C query instead carries up to four artifacts: the parallel plan the planner
should pick and did, a candidate index — for 4, 5, 7 and 8 — built and measured and shown not to
help, the row math, and — for 7, 9 and 10 — a byte ratio used as a **falsifiable prediction**
rather than as a recorded number. The plan and the row math are there for all six; the candidate
index covers four of them and the byte ratio three, and they overlap only at query 7.

## Measurement conditions

**Postgres 16, stock memory settings.** `shared_buffers` 128 MB, `work_mem` 4 MB,
`maintenance_work_mem` 64 MB, `effective_cache_size` 4 GB,
`max_parallel_workers_per_gather` 2, on a 4-CPU / 8 GiB VM.

Nothing here was measured with memory tuned up, and that was a deliberate decision rather than
an omission. Three things make it the right one:

- The gated metric is `Shared Hit + Shared Read` off the root plan node. **A sort that spills
  writes to `Temp Read/Written`, which is a separate counter** — so a spill cannot inflate the
  number being gated. At `work_mem` 4 MB, 64 MB and 256 MB, the root-node block counts for a
  Class A and a Class C shape were **bit-identical** (417 and 514,152) — but all twelve sorts in
  that probe are `Sort Method: quicksort` at 2,466 kB and 37 kB, so neither shape spills at any
  of the three settings. The probe demonstrates work_mem-invariance of the gated total on a query
  that does not spill; it is not a direct measurement of a spilling query's gated total holding
  steady. **It is a one-off memory probe and its readings stand on their own**: 417 and 514,152 are
  not cells of any table below and appear in no other passage, so there is no matching figure
  further down to look for.
- `shared_buffers` moves the split between `hit` and `read` and never their sum, which is the
  property that makes these figures portable to RDS. **The setting itself was not varied here,
  because it cannot be without restarting the server** — it has `postmaster` context, and no run
  behind any figure in this document restarted one. The deployed RDS instance supplies the second
  value instead: it reports `shared_buffers` at **23,081 pages** against this database's **16,384**,
  which is the 128 MB above. Across six endpoint-form cells measured on both, execution blocks
  differ by **at most 2** while planning collapses **16.3×** on the statements that plan every
  partition. That is an endpoint-form comparison and not a `shared_buffers` experiment — the two
  instances differ in more than this one setting — but the quantity it agrees on to two blocks is
  the sum this document gates, and the quantity that moves is planning, which this document
  attributes to the partition count rather than to buffers.
- Raising `work_mem` bought a Class C shape no speed on this host, and the mechanism is the reason
  to expect that: the leader and its two workers are three processes, each free to claim the full
  `work_mem` per sort, against a 128 MB `shared_buffers` — private memory at 256 MB apiece
  displaces the page cache the query depends on. The readings point the same way — 29.8 s at 4 MB,
  43.7 s at 64 MB and 46.9 s at 256 MB, one run each — but 46.9 against 29.8 is **1.57×**, inside
  the greater-than-2× run-to-run spread this working set shows and below the 2.4× measured on one
  query further down, so they illustrate the mechanism rather than measure it. Neither the query
  nor its bound parameters was recorded beside them, which is a second reason not to read the three
  as a series. **They come from a one-off memory probe too**: 29.8, 43.7 and 46.9 are cells of no
  table below and appear in no other passage, so nothing further down restates them.

**Three Class C queries spill, and that is not a defect.** At 4 MB, measured on the queries as
they ship, each spill is three concurrent per-worker external merges rather than one, and the
figure this table used to publish was the smallest of the three — the leader's. Per-worker range
and root-node temp total — the per-worker figures are kB as `EXPLAIN` prints them, the root
`temp` counters are 8 kB blocks: `04_volume_profile.sql` **671,080–784,944 kB** (root `temp
read=824,411 written=825,400`, **6.30 GiB**); `07_vwap_check.sql` **816,464–893,824 kB** (root
`temp read=971,967 written=973,048`, **7.42 GiB**); `08_top_minutes.sql` **590,112–644,504 kB**
(root `temp read=705,564 written=706,482`, **5.39 GiB**), all `external merge`. Nothing is wrong
with them — they aggregate 41.7M rows into ~148k groups and that sort does not fit in 4 MB at
any sane setting. It costs them wall-clock and costs the gate nothing, because spills land in
`Temp Read/Written` and every Class C verdict is a plan property.

The two coverage queries were different: their sorts came from a merge join the query was
forcing, and fixing the plan removed them entirely — at the same 4 MB. The two sizes are two
orders of magnitude apart: query 10 spilled 1.2 GB, query 9 **as shipped** spilled 5,264 kB, and
the 1.2 GB that also appears against query 9 belongs to an intermediate variant that ships
nowhere. That is a plan problem that looked like a memory problem, and it is the one case here
where a spill was worth chasing.
See [The two coverage queries](#the-two-coverage-queries-9-and-10).

`maintenance_work_mem` is raised to 1 GB for index builds alone. It changes build wall-clock and
nothing any query measures, and no figure in this document was taken while it was raised.

**`VACUUM (ANALYZE)` on every partition first.** An index-only scan falls back to heap fetches
wherever the visibility map says a page may hold invisible tuples, so a freshly-ingested
partition reads far past its own byte ratio and the Class C ceilings look broken on a database
that is fine. The map went from **89.7053%** all-visible to **100.0000%** across all 71
partitions, in 92 s. The vacuum log backs the elapsed time — `Time: 91770.519 ms` — with
per-partition output, but not the two percentages themselves: **100.0000%** over 514,046
relpages is live-reproducible from `pg_class` and was reproduced exactly, while **89.7053%** is
a pre-VACUUM state that cannot be reproduced and stands as recorded rather than as reproducible.

That starting figure is worth recording, because the spec expects zero: the ingest never
vacuums, but **autovacuum had already run**, on all 71 partitions, about six hours after the
load finished. The manual pass was still required — 10.3% of pages would have taken heap
fetches — but a session that expects 0% and measures 89.7% has not run the wrong statement.

**Which figures here are repeated, and which are one observation.** Every block count, node type,
worker count, spill size and byte ratio in the Class A, B and C tables below is a **property of the
plan** and was identical every time it was measured on one side of migration 005 — including on a
full re-capture taken from scratch after the first set was lost. **Migration 005 is where one of
these counts moves, and it moves by 40 blocks.** Item 1's figures were taken before it added
`bars_ts_symbol_idx` and every table after Item 1 was captured afterwards, so query 5 reads 25,009
there against 25,049 in Item 2 — up 40 — and query 7 reads 514,262 there against the 514,222 Items 2
and 4 read — down 40 — with each side stable over repeated runs and queries 9 and 10 identical on
both. Query 7's forced block ratio carries the same boundary at 0.01%, 1.9021 against 1.9019. That
is a real boundary rather than noise, and Item 1's own caveat and query 7's re-derivation each state
it where they appear. Those are the numbers the gate rests on. **The two endpoint-forms subsections
under Class A are the other exception**: there a statement's first run on a connection can read more
blocks than its later runs. Neither publishes every first run the same way as its medians, and
neither is purely inside or outside one. The `/bars`-and-`/daily` subsection shows some first runs
inside the five-run series it publishes — W-COLD's cap page reads `404, 401, 401, 401, 401` and
W-WIDE's planning at `fetch = 101` reads `1242, 852, 852, 852, 852` (that is the smallest of the
three measured page sizes, not the default, which is `fetch = 1,001` and is flat at 852), both
medians published as the steady value — and its worst first runs, on a connection that has planned
nothing at all, are reported separately rather than inside a five-run series: a brand-new
connection's 6,348 planning blocks, `/daily`'s 5,729, `REQUIRE_SYMBOL_SQL`'s 70. All three are
repeated readings, identical on every one: 6,348 and 70 over five fresh connections each, with
6,348's milliseconds also carrying a published five-run median, and `/daily`'s 5,729 over three
fresh connections, first statement on each. The analytics subsection's first runs are reported
beside the median each excludes, in milliseconds and planning blocks; the execution blocks a first
run also reads are reported apart from any series for two of them, `/analytics/volatility`'s 423 and
`/analytics/gaps`' 428, in the prose beside that subsection's Class A table; `/daily`'s 420 is
reported both ways in the `/bars`-and-`/daily` subsection before it: apart from any series where the
planning figures are given, and inside its own five-run series where the wrapper is compared with
the file. W-COLD's cap page above does publish its own first-run execution blocks, inside its
five-run series, as the leading 404.

**Wall-clock is not one of them.** This working set is one database at two sizes: summed over `bars`
and its children, `pg_total_relation_size` read 5,547,909,120 bytes (5,291 MB) on 2026-08-27, when
the universe finished loading, and reads 6,998,122,496 bytes (6,674 MB) with migration 005's index
and the four hot-window indexes on disk (`docs/METHODOLOGY.md` carries the 2026-08-27 figure as a
table row and the current one in the prose under it). On it the same query against the same data
varies by more than 2× run to run, and a variant measured after three others has read a cache they
filled. Where a timing is a median of repeats this document says so; where it is a single
observation it is marked as one. **Two verdicts in this document are gated on a timing rather than
on a block count or a node type: Class A's <100 ms target, and 6.3 #13's requirement that the deep
page's HTTP p50 sit within 1.5× page 1's.** Both are read as a statistic over at least five runs and
never off a single observation; every other verdict here rests on a count that does not move when
the cache does. (Ungated timings elsewhere in this document can use fewer runs, stated at each one —
the 1% threshold reading further down is a three-run median.)

**Buffer counts come off the root plan node and are never summed.** Postgres buffer counts are
cumulative: every node already includes its children, so adding them counts the same read once
per level of the tree. On this database's own query 9 the root reports **514,251** blocks and
the sum over all nodes reports **4,943,377** — **9.61× more**, for one identical execution. The
inflation factor is plan-shape dependent, so it is not a constant that can be divided back out.

Every count in the **Class A and Class B** tables below was **cross-checked two ways before it was
recorded**: the root object of `EXPLAIN (…, FORMAT JSON)` and the first `Buffers: shared hit=…
read=…` line of the same plan in text form. These are different output paths in Postgres, and the
harness raises rather than warns if they disagree. **Five sets of figures here do not carry that
check in full.** The first is **Class C**, whose tables come from a harness that emits no JSON plan
at all — there is no second output path for them to be checked against, so their agreement is
unverified rather than verified. The second is the **`/bars` and `/daily` endpoint-forms
subsection** under Class A, whose original run had no check and whose
correcting pass added one. (That added check compared execution blocks only when this sentence was
first written; it compares execution **and planning** blocks now, and raises on either.) The third
is the **analytics endpoint-forms subsection** after it, which checks execution blocks on the cells
it names and no planning count at all. The second and third are each named again in the subsection
they describe, and the `/bars` one names Class C's alongside its own. The fourth and fifth are the
**hot-window partial index** and **BRIN** tables under "The other two index decisions": their
harness wrote to a path that no longer exists, so each of those two tables is the record of its own
measurement rather than a summary of one, and there is no second output path left to check it
against. Those two are named here and not at their own tables.

One limitation the check used to carry, stated because it bounds what "cross-checked" buys for
the figures already recorded. It matched the first `Buffers: shared hit=…` line of the text plan,
requiring `hit=`. A node that read only misses prints `Buffers: shared read=N` with no `hit=` at
all — **231 of the 1,021 Buffers lines in the captured plan set** — so on a root in that shape
the match walked past it and compared a descendant's number, or the planning group's, against
the root's. Every figure below was taken on a warm root, where that cannot happen. The
cross-checking harness makes both fields optional now and raises on a line carrying neither, so
no figure moves;
the `hit=`-requiring form survives in the three harnesses that perform **no** cross-check at all,
which is where the residual risk actually sits.

**Every measurement here is also a fresh connection — with three exceptions.** Each harness spawns a
new `docker compose exec` per query, which pays catalog and sort-operator lookups a warm backend
already has cached — so the published counts are cold and self-consistent with each other. Running
the ten committed query files five times inside one continuous `psql` session instead gives
root-block counts a few blocks lower — `06_daily_rollup.sql` settles at 414 against the published
420, six blocks and 1.43%; `03_gaps.sql` at 418 against 431, thirteen blocks and 3.02%. Neither
moves a target or a ratio in this document, and a verifier who re-runs them in one session and finds
these numbers has not found a defect. **The `/bars`-and-`/daily` subsection is measured inside one
continuous session per harness and is therefore already on the lower side of this pair.** The
analytics subsection's SQL medians are measured the same way, but its HTTP figures are not — they
ran through the compose `app` service's own connection pool, not a harness session — and it
separately publishes fresh-connection first runs on the **upper** side of this pair — five rows in
the First runs table. Three of them are **402.414 ms** for a W-HOT `/analytics/largest-moves` page
at `min_move_pct = 1`, **108.174 ms** for `/analytics/volatility`, the only Class A statement of the
five, and **50.787 ms** for a W-COLD `/analytics/largest-moves` page at the default limit; the other
two are the 2026-06-25 cursor at **0.812 ms** on the statement as it stands and **162.381 ms** as
first shipped. The Class A table's other two statements are not among the five: `/analytics/gaps`'
own first run is 31.835 ms on a used connection, and the Class A `/analytics/largest-moves` cell's
is 0.949 ms, also used. `/analytics/volatility`'s was measured again on 2026-09-15 and read
**168.528 ms**, at the same 423 execution blocks and the same 4,875 planning blocks: the block
counts reproduce exactly and only the timings moved, the execution by 1.56×, which puts that first
run further above the 100 ms target than 108.174 is. Each of those two is one observation, and the
paragraph on the Class A gate below carries the 108.174 alone. So only a subsection's SQL medians
support a lower-side reading; a figure from either must not be compared against a published
fresh-connection count without saying which kind of figure it is — one paragraph in the first of
them did, and is corrected in place. **The third exception is "The bounds a request runs under"
below**, which is measured against scratch Postgres containers rather than the loaded database, most
of it through the application's own connection pool in a server started for the measurement, at one
to three runs per figure rather than five; the connection state there is whatever the pool holds,
and the subsection states its conditions where it opens.

## The heap the numbers rest on

Class B's gated number moves by **54–64× on the heap's write order alone** — a fixture
measurement carried in from the design work, not one taken on this database — so the layout is
part of the evidence rather than an assumption behind it. This database is also of mixed
provenance — the first fifty symbols were restored from a `pg_dump` and the second fifty loaded
live — and no before-measurement was taken at the time.

`pg_stats.correlation` cannot discriminate this — re-measured on 2026-08-31, after this
feature's own mandated `VACUUM (ANALYZE)`, the parent-inherited `symbol` correlation reads
0.0067, `bars_2026_06` alone reads 0.0260, and across all 71 children it runs −0.0076 to 0.0843,
mean 0.0320, which is evidence of nothing either way. Two `ctid` queries can discriminate it,
and they were run over **every symbol in every partition** rather than as a spot check:

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
| live | LIN | 281,129 | 3,538 | 0.69 | 79.5 | 0 |

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
`bars_2026_06`, now holds 699,245 rows instead of 41,723 — 16.76× more — and reads **3.1654**, a
difference of **0.008%**. The pooled whole-table figure is 0.23% from the published one. The
threshold that would require this document to explain a discrepancy is ~25%, so there is
nothing to explain: the ratio is a property of the row width and the load order, not of the row
count.

## The two coverage queries (9 and 10)

These are the only two queries in the set whose plan was actually wrong, and the fix is the
clearest tuning story here — so it is written out in full rather than reduced to a row.

`09_coverage.sql` shipped at Feature 2. Over the full universe it took a median **179.4 s**
over the three interleaved runs in the table below, ran every one of its 71 partition scans
**serially**, and spilled. It launched no parallel workers at all, which is not the plan an
unfiltered full-universe aggregation should get.

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
named here rather than deleted: `73164af`'s commit message carries the whole table — 173,923,
128,138, 112,306 and 32,737 — not only the last figure, and against the interleaved medians the
middle two read **19.6% low** (128,138 against 159,437) and **13.9% low** (112,306 against
130,421).

**The counting join was against the 148,400-row session set, keyed on `symbol` as well as the
trading date.** 148k against 41.7M is a shape the planner serves with a merge join, sorting
every bar in the database. Joining to the 1,484-row calendar instead and grouping afterwards
costs nothing: `first_bar_ts` is `MIN(ts)`, so no bar can precede its own symbol's floor, and
the outer `LEFT JOIN` already discards any symbol not in `expected`. That premise was checked
rather than argued — all 100 symbols hold `first_bar_ts = MIN(ts)` exactly.

**`bounded` was materialized, because it is referenced three times.** A materialized CTE
carries no statistics, so the planner cannot cost a hash join against it and falls back to the
merge join. `NOT MATERIALIZED` restores `market_days`' statistics.

Row three is why both changes ship together: the rewrite *alone* spills 232× more than the
shipped form, because dropping the symbol key widens what the merge join has to sort. Taken as
a lone change it reads as a regression.

The output is unchanged, and that was verified rather than assumed: 103 rows, symmetric
difference 0, identical row for row against the shipped form over the full window, with the
pipeline's gate reading `missing_units 0`, `coverage_pct 72.16`, `uningested_symbols 0`.

`10_missing_minutes.sql` shares the shape and needed only the second change: 131.1 s serial
with a 1.2 GB spill, against 42.6 s with 71 parallel scans, 2 workers and no spill. **One
observation per variant, not a median.** The structural half is a plan property and holds; treat
the two timings as indicative rather than as a ratio. The 2026-08-31 re-capture read the shipped
form at **89.3 s** — the same plan, the same 71 scans and 2 workers, **2.1× the wall-clock** —
which is the run-to-run spread the coverage table above shows on query 9. 7,100 rows identical.
Its **pooled**
complement of query 9 — `100 − 100 × sum(missing) / sum(expected)` over all 7,100 symbol-months —
is **72.1564**, which is 72.16 at two decimals and
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
result. Each carries three or four artifacts instead: the plan and the row math for all six, a
candidate index for 4, 5, 7 and 8, and the byte math for 7, 9 and 10.

### Item 1 — the plan the planner should pick, actually picked

`Workers Launched`, not merely `Workers Planned`. All six:

**Two caveats on this table, both stated before it rather than after.** Its block counts were
taken **before migration 005** added `bars_ts_symbol_idx`; every table after it was re-captured
afterwards. The two eras differ by at most 40 blocks — query 5 reads 25,009 before and a stable
25,049 after, confirmed over three consecutive runs — and queries 9 and 10 are identical on both
sides, so it is a real boundary rather than noise. And the `exec` column is **one observation per
row** from that same pre-005 capture, kept only for scale; the columns that carry the verdict are
the three on the right. The 2026-08-31 re-capture timed all six differently — queries 9 and 10 at
122,178 ms and 89,312 ms against the 92,575 and 133,978 below — and it is the re-captured pair
that Item 4 reasons from. This column is not a second measurement of it.

| query | root node | exec (single run) | root blocks | workers launched | parallel `bars` scans |
| --- | --- | ---: | ---: | ---: | ---: |
| `04_volume_profile.sql` | `WindowAgg` | 242,836 ms | 514,300 | 2 | 71 |
| `05_largest_moves.sql` | `Limit` | 8,191 ms | 25,009 | 2 | 3 |
| `07_vwap_check.sql` | `Finalize GroupAggregate` | 323,955 ms | 514,262 | 2 | 71 |
| `08_top_minutes.sql` | `Incremental Sort` | 163,541 ms | 514,280 | 2 | 71 |
| `09_coverage.sql` | `Sort` | 92,575 ms | 514,251 | 2 | 71 |
| `10_missing_minutes.sql` | `Merge Left Join` | 133,978 ms | 514,220 | 2 | 71 |

Query 5 scans 3 partitions rather than 71 because it is the only Class C file with a window —
`:start`/`:end` bind 2026-04-01 → 2026-06-30, and that range falls inside 3 monthly partitions.
**The 3 is this window's alignment and not a property of a 90-day range** — a 90-day range can fall
across as many as five monthly partitions, and does for one running from 31 January to 1 May of a
non-leap year. Partition pruning is doing exactly what it should; the query is still Class C because
ranking by magnitude must see every row *in that window* before it knows which N come out.

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
more than the difference they would be claiming — run-to-run noise on a working set of the size
above, measured at more than 2× on a single query, which is exactly why this gate is on blocks.

**Query 5's is the one worth a sentence**, because it is the index a reviewer asks for first.
With the ranking expression indexed, the plan is unchanged to the sort key:
`Limit → Gather Merge → Sort (top-N heapsort) → Hash Join → Parallel Append`, ranking still done
by a sort. The reason is the one section 4 gives — the session-bounds join to `market_days` is
not in the index, so the planner cannot stream in ranked order — and this is that prediction
confirmed on the real dataset rather than carried in from a fixture.

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
heap, and far below that on an uncorrelated one. Query 5 is the only one of the six that sits off
f = 1.0: its window puts it at **25,049 root-node blocks against the 514,264 the whole-universe
queries read — 4.87%**, both post-005 and both from Item 2's table — and the planner *still* chose
a parallel sequential scan over any index. **4.87% is nowhere near 25%, so this is not a
measurement of where the crossover lies** — it is a candidate index rejected far below the fraction
at which the cost constants alone would still let one win. What it bounds is the planner's own
crossover on this heap, which sits under 4.87%, and that is the uncorrelated half of the sentence
above rather than its 25% ceiling. The other five sit at f = 1.0, where no index can win by
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
is measured here because that index is on disk only inside the runs this feature makes: it does
not exist before one builds it for the negative result, and it does not exist after that run
drops it.

**This prediction failed once, and the failure was the query rather than the database.** An
earlier draft of query 7 also reconstructed a typical price from `high`, `low` and `close` —
three columns the covering index does not carry — which makes an index-only scan impossible by
construction. The forced scan then reported **0 index-only scans and 682,813 root blocks against
the sequential scan's 514,260**, a ratio of **0.75×**: worse than what it replaced, and 60% away
from the byte ratio. Narrowing the query to the four columns the index carries took it to
0.51%. A self-evaluating measurement would have recorded 1.89× and moved on; this is what the
falsifiable form is for, and the test that now guards it fails if any other `bars` column is
read back in.

**Query 7's row was re-derived from scratch on 2026-08-31**, because it is the one row here whose
measurement is not a by-product of the before/after sweep — the covering index exists only inside
the run that builds it. Rebuilt over the full 41.7M rows in 264 s, measured, and dropped again:
byte ratio **1.8924**, forced block ratio **1.9019**, **0.50% apart**, with **71 index-only scans
and 0 heap fetches**. The negative result came back in the same run — with the index on disk the
planner still read 514,222 blocks, identical to its free choice, and chose no index-only scan.
The two block ratios are 0.01% apart. The original's own numerator is not recoverable — the run
that produced 1.9021 wrote to a path that no longer exists — but Item 1 records query 7 at
514,262 blocks before migration 005 against the 514,222 measured here, which is the same
≤40-block boundary noted there.

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
An earlier capture had query 9's forced scan at 105,024 ms against 92,575 free — 0.88×,
*slower* — and query 10's at 102,370 ms against 133,978 free — 1.31×, faster. The 2026-08-31
from-scratch re-capture moves both: query 9 forced now runs
**51,035 ms** against **122,178 ms** free — **2.39× faster** — and query 10 forced runs
**50,361 ms** against **89,312 ms** free — **1.77× faster**. Query 9 flips sides entirely,
slower becoming faster; query 10 stays on the faster side in both captures and only its
magnitude moves. Neither outcome is stable across a from-scratch re-capture, which argues the
wash at least as hard as the earlier split verdict did. The comparison is also serial against
parallel, not two queries of the same shape against the same index: the forced variant runs at
`max_parallel_workers_per_gather=0` while the free choice launches 2 workers over 71 parallel
scans. The blocks, meanwhile, agree with the bytes to two figures on both. That gap is the whole
argument for gating on blocks.

Neither planner ever chose the index-only scan on its own: free-choice block ratio **1.00×** for
both, which is the seq scan. The mechanism is real, it does what it always said it did, and it
is worth **3.16×** — not 10×.

## Class A — the queries and endpoint forms the API serves

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

The run column is the harness's console output, rounded to whole milliseconds; the median is
computed from the unrounded values to one decimal. That is why `03_gaps.sql`'s printed runs (27
27 27 26 28) have an integer median of 27 against the reported 26.8, and `06_daily_rollup.sql`'s
(27 29 28 26 27) likewise print a median of 27 against the reported 26.7 — a verifier
recomputing the median from the printed column has not found a defect.

Roughly 420 blocks each — 3.28 MiB, since a block is 8 KiB and every figure converted from a block
count in this document is binary (the working-set sizes above are not among those; each is a
`pg_total_relation_size` byte count and a binary megabyte rendering of it, which is what
`pg_size_pretty` prints and what the documents carrying those figures label MB) — because partition
pruning takes this 90-day window down to three monthly partitions — a 90-day window can fall across
as many as five — and the PK then serves one symbol out of them.

**Against the same constructed before Class B uses** — `enable_indexscan`, `enable_bitmapscan`
and `enable_indexonlyscan` all off, which is the only way to get an untuned state for a
mechanism that has existed since Feature 1:

| query | before (constructed) | after | ratio | before plan | after plan |
| --- | ---: | ---: | ---: | --- | --- |
| `01_volatility.sql` | 25,088 | 423 | **59.31×** | 3 parallel seq scans + seq scan on `market_days`, 2 workers | 3 bitmap index + 3 bitmap heap scans on `bars`, index scan on `market_days`, 0 workers |
| `03_gaps.sql` | 25,117 | 431 | **58.28×** | 3 parallel seq scans + seq scan on `market_days`, 2 workers | 3 bitmap index + 3 bitmap heap scans on `bars`, index scan on `market_days`, 0 workers |
| `06_daily_rollup.sql` | 25,085 | 420 | **59.73×** | 3 parallel seq scans + seq scan on `market_days`, 2 workers | 3 bitmap index + 3 bitmap heap scans on `bars`, index scan on `market_days`, 0 workers |
| `02_correlation.sql` (Class B) | 25,125 | 830 | 30.27× | 3 parallel seq scans + seq scan on `market_days`, 2 workers | 3 bitmap index + 3 bitmap heap scans on `bars`, index scan on `market_days`, 0 workers |

**Class A's block reduction is roughly double Class B's**, and the reason is the whole basis of
the class split: these read one symbol where query 2 reads two. Selectivity is what the index
buys, and halving the symbols buys very nearly twice as much — 59.31 ÷ 30.27 = 1.96, where the
mechanism predicts 2. Class A is not *gated* on this
ratio — its target is the 100 ms above — but the before/after is recorded here because 6.3 #14
asks for it on all ten queries, not only on the one with a block target.

The first run of a cold sweep measured query 1 at **312 ms**, and that figure is worth keeping
next to the other five: it is the same plan and the same 423 blocks, differing only in what the
page cache happened to hold. It is also the reason the table above reports a median of five
rather than a single number.

**Two further five-run sweeps on 2026-08-31 say the same thing, and the contrast between what
moved and what did not is the point.** The root blocks came back **identical to the block** in
both — 423, 431 and 420 for Class A, and 25,125 → 830 for Class B — while every median moved and
the Class B wall-clock ratio read 7.19× and then 7.87× against the 7.00× above:

| | table above | re-run | re-run |
| --- | ---: | ---: | ---: |
| `01_volatility.sql` | 49.0 ms | 53.0 ms | 55.2 ms |
| `03_gaps.sql` | 26.8 ms | 28.2 ms | 25.6 ms |
| `06_daily_rollup.sql` | 26.7 ms | 30.0 ms | 29.6 ms |
| query 2, wall-clock ratio | 7.00× | 7.19× | 7.87× |

The medians above stand as the original measurement; these are a different day's cache and are
recorded as corroboration rather than as a replacement. All three queries pass on all three
sweeps, and no block count has moved across any of them.

### The endpoint forms — `/bars` per-page cost and the `/daily` wrapper

Measured 2026-09-07 against the same database, read-only: `EXPLAIN (ANALYZE, BUFFERS, FORMAT
JSON)` on the exact statements the endpoints run, five runs each, median reported. Corrected and
extended 2026-09-08 by a second read-only pass, which is what the planning columns and the
`fetch = 1,001` row for `/daily` come from.

**Two conditions this subsection does not inherit from the rest of the document, stated here
because they change how its numbers compare with the tables above.** First, these figures come
from **one continuous psycopg session**, not a fresh `docker compose exec` per query — so the
caveat under "Measurement conditions" applies to them and not to the Class A/B/C tables, and the
first execution of any statement in the session reads a few blocks more than the rest. Second,
the harnesses behind this subsection do **not** perform the JSON-versus-text cross-check that
the Class A and Class B tables were recorded under — **not "every other count in this document",
which is what this sentence read until 2026-09-10 and is the claim the "Measurement conditions"
section above withdrew; Class C carries no such check either**; the 2026-09-08 pass added it
and every figure below survived it, but the original run did not have it.

**The block counts below are execution only.** Root-node `Shared Hit + Shared Read` excludes
planning, which Postgres reports separately, and on a partitioned table planning is not a
rounding error — see "What the per-page block counts leave out" below, which is the most
important paragraph in this subsection.

#### `/bars` — three windows, because two of them control the smaller variable

`BARS_PAGE_DEFAULT` and `BARS_PAGE_MAX` govern `/bars` over the whole ingested span. That span
is 71 monthly partitions, of which four carry the hot-window partial index and 67 do not, and
the endpoint accepts any window inside it. So the per-page cost is measured on both axes rather
than on one: the index set, and the number of partitions a request opens.

| window | parameters | days | partitions | index the planner chose |
| --- | --- | ---: | ---: | --- |
| W-HOT | `AAPL`, 2026-04-01 → 2026-06-30 | 90 | 3 | per-partition `_pkey` |
| W-COLD | `AAPL`, 2025-04-01 → 2025-06-30 | 90 | 3 | per-partition `_pkey` |
| W-WIDE | `AAPL`, 2020-08-01 → 2026-06-30 | 2,159 | 71 | per-partition `_pkey` |

W-HOT is the window this document already binds Class A at, so its number is comparable with the
table above. W-COLD is the same calendar window one year earlier and lies entirely in partitions
that carry no hot index. W-WIDE is `INGEST_START` to `INGEST_END`, the widest request the spec
permits on this endpoint.

**Every row below is page 1**, bound with the endpoint's own first-page sentinel. Nothing here
measures a deep page; `DEEP_PAGE_DEPTH` exists because that is a separate question.

| window | fetch = 101 | fetch = 1,001 | fetch = 10,001 | planning |
| --- | ---: | ---: | ---: | ---: |
| W-HOT | **5** blocks, 0.04 ms | **19**, 0.32 ms | **168**, 2.45 ms | 36 blocks, 0.11 ms |
| W-COLD | **5** blocks, 0.03 ms | **19**, 0.25 ms | **401**, 10.18 ms | 36 blocks, 0.11 ms |
| W-WIDE | **5** blocks, 0.04 ms | **19**, 0.23 ms | **167**, 2.39 ms | 852 blocks, 1.62 ms |

`fetch` is the page limit plus one, so these are the caps as the endpoint binds them: 1,001 is
`BARS_PAGE_DEFAULT` and 10,001 is `BARS_PAGE_MAX`. Planning does not vary with `fetch` and is
therefore reported once per window rather than per cell.

**One execution cell is not invariant across its five runs, and it is the cell the cap is set
from.** W-COLD at fetch = 10,001 read `404, 401, 401, 401, 401` — the first run's cold cache
moved the *blocks*, not only the milliseconds. Every other execution cell in this subsection is
identical to the block across all five runs. The published 401 is the median and the
steady-state value; it reproduced as `401` five times out of five on 2026-09-08. The worst
single observation is 404.

**The planning column is not invariant either, but only in three of the nine cells, and the
reason is not the one you would guess.** The elevated runs are the three `fetch = 101` cells —
the first time each **window** was planned on the connection, not the first time the statement was:

    W-HOT  fetch=101   68, 36, 36, 36, 36        W-WIDE fetch=101   1242, 852, 852, 852, 852
    W-COLD fetch=101   54, 36, 36, 36, 36

`fetch`, `lo` and `hi` are bind parameters, so all nine cells run the *same statement text*.
W-COLD's elevated run came after that text had already executed fifteen times on the connection,
and W-WIDE's after thirty. What is new in each is the set of **partitions** the window opens, and
the excess is exactly **6 blocks per newly-planned child**: W-COLD adds 3 children,
`54 − 36 = 18`; W-WIDE adds the remaining 65, `1242 − 852 = 390`. Both divide to 6.0.

The other six `/bars` cells and **all four measured `/daily` cells are flat** — `/daily` reads
`571, 571, 571, 571, 571` at fetch 51, 65, 101 and 1,001. An earlier form of this paragraph
printed `5729, 571, …` as a `/daily` row; that array is the first statement of a fresh *session*
and is the subject of the `/daily` paragraph further down, not a cell of this table.

The published planning figures are the steady-state medians. **Before quoting one as flat, ask
whether the connection has planned that window before — not merely that statement.**

**A request is two statements, and only one of them is in the table.** Every `/bars` and
`/daily` request also runs `SELECT 1 FROM symbols WHERE symbol = …` for the 404 tier. Measured
2026-09-08 in a warm session: **2 blocks, 0 planning blocks, 0.01 ms** — the blocks and the
planning blocks identical on a hit and a miss, the milliseconds not quite (a miss reads
0.00). On a connection that has not planned it before, measured 2026-09-09 over five fresh
connections, it reads **70 planning blocks** (identical on all five, 0.10–0.19 ms) against those
same 2 execution blocks — thirty-five times the statement's own execution cost, once per
connection. It is not included in any figure above.

Postgres omits the `Planning:` section from a text plan entirely when planning read no buffers,
so in that output form "0" and "not reported" are the same string. The warm figure above is
taken from the JSON plan, where the zero is explicit.

**The planner chose the per-partition primary key on all three windows and never a hot-window
index, and that was measured rather than assumed** — on 2026-09-08 across all five runs of every
window, not only the first. It is the load-bearing fact about reproducibility here: the four
`bars_2026_0N_hot_idx` partial indexes exist in no migration and no `db/schema.sql`, so a number
that depended on one would hold on this database and nowhere else. None of these does. The index
set behind every row above is the set a fresh database gets from the migrations.

**Two residuals, stated rather than left implicit.** W-HOT covers three of 71 partitions, so on
its own it would have measured the best-indexed **4.2 percent** of the table — the hot index
itself covers four of 71, or 5.6 percent, and W-HOT spans three of those four. That is why
W-COLD exists. And W-WIDE's *index set* reproduces on a fresh database while its *data* does
not: no CI or testcontainer database holds 41.7M bars over 71 partitions.

**What the per-page block counts leave out, and it is the largest single quantity in this
subsection.** Root-node blocks are execution only. Postgres reports planning separately, and an
`Append` over 71 children has to plan 71 children. (This read `Merge Append` until Feature 7 saved
W-WIDE's plan and found a plain `Append`: each child's range already gives the order.)

| window | partitions | planning | execution at fetch = 1,001 | whole page |
| --- | ---: | ---: | ---: | ---: |
| W-HOT | 3 | 36 blocks | 19 blocks | **55 blocks** |
| W-COLD | 3 | 36 blocks | 19 blocks | **55 blocks** |
| W-WIDE | 71 | 852 blocks | 19 blocks | **871 blocks** |

**So opening 71 partitions instead of three costs a default page 15.8× more blocks, not
nothing.** The execution halves are within one block of each other at `fetch = 101` and
`fetch = 1,001` — not at `fetch = 10,001`, where they read 168, 401 and 167 (above). At those two
sizes it is a real result — but not because the plan descends into every partition it opens. An ordered
`Append` under a bound `LIMIT` stops once the page is full: the saved W-WIDE plan for this cell
executes exactly **one** of its 71 children (`bars_2020_08`, the earliest and so the first in
`Append` order, 19 blocks) and lists the other 70 as `(never executed)`. One descent per child is
what a `Merge Append` would cost; a plain `Append` under a `LIMIT` costs only as many descents as
filling the page needs, whatever the child count. The partition count moves the setup — planning
— and not the work. And the setup is 852 blocks: **23.7× the planning of a three-partition window,
14.7× its planning time, and 7.0× W-WIDE's own execution time.** A page whose execution is 0.23 ms
spends 1.62 ms being planned.

Worse on a connection that has planned nothing at all — and that is a narrower condition than "has
not planned this statement". The artifact holds **three** states, not two: steady **852**; **1,242**
when the connection is warm but this window's partitions are new to it; and **6,348** on a brand-new
connection. A pooled connection that has already run this statement over other windows pays the
middle one when this window's partitions are new to it; one that has run only other statements can
pay more. `/bars` page 1 over W-WIDE's span, planned for the first time on a connection whose
earlier statements had already planned all 71 children, read **1,920** planning blocks (see "First
runs" in the analytics subsection below) — 1,068 blocks above the steady 852, though by this
model's own terms no child was newly planned here; what the extra 1,068 pay for is not measured.
W-WIDE at fetch = 10,001 reads **6,348 planning blocks**
against 167 execution blocks. The block count is exact and reproduces: five fresh connections on
2026-09-09 read 6,348 on every one. The time does not reproduce as narrowly — the same five read
6.40, 6.70, 7.17, 7.89 and 10.55 ms, median **7.17 ms**, so the 5.893 ms recorded from a single
observation on 2026-09-08 is below the whole of that range and should not be quoted as typical. Take
the blocks as the measurement and the milliseconds as an order of magnitude.

**All three of those states are a custom plan's, and a pooled connection can leave that regime.**
`api/deps.py` sets no `prepare_threshold`, so psycopg prepares this statement after five executions
and Postgres can switch it to a generic plan from the eleventh. On a connection shaped like the
pool's it did, measured 2026-09-15: the generic plan over the same W-WIDE span plans **8** blocks
for page 1 and **0** at the deepest cursor, against the 852 and 8 a custom plan reads ("The
single-symbol deep page" below). So 852 and 1,242 are what a fresh or lightly used connection pays
per page — the state every figure in this subsection was captured in — and not a standing per-page
cost for every connection the pool holds. That switch was measured on one traffic pattern, page 1
alternating with the deepest page over the whole ingested span; Postgres adopts a generic plan only
when its estimated cost beats the average custom one, so whether a connection serving only narrow
windows switches the same way is not measured.

This is why the index-set axis and the partition axis are not comparable as published: the index
set was identical on all three windows, so there is no index-set spread to compare against. The
remaining execution spread — W-COLD's 401 blocks against W-WIDE's 167 at the cap, 2.4× — is not
on either declared axis, and the heap-contiguity explanation for it is a hypothesis this
document has not measured.

**Verdict: `BARS_PAGE_DEFAULT = 1000` and `BARS_PAGE_MAX = 10000` are confirmed, not moved.**
On execution blocks the worst window at the cap is **W-COLD at 401**; on whole-page blocks it is
**W-WIDE at 1,019** against W-COLD's 437. The cap is confirmed against both readings, which is
why the disagreement does not change it. At the default a page costs 19 execution blocks —
152 KiB — on every window, 55 blocks whole-page on a narrow one and 871 on the widest; at the
cap the worst execution page costs 401 blocks, 3.13 MiB, in 10.2 ms. Neither is near a limit
worth lowering a cap for, and raising the cap has no measurement asking for it. Confirming a
value with evidence is the revision; changing it without evidence would not be.

**What this leaves for Feature 7.** The hot-window index's verdict is not settled by "the
planner never chose one": on the widest window, for as long as the statement is on a custom plan,
the dominant per-page cost is planning over 71 children, which no index on any child changes. An endpoint that routinely serves W-WIDE-shaped
requests is arguing for a narrower default window or for `DEEP_PAGE_DEPTH`, not for or against
`hot_idx`.

**Answered at Feature 7, in "The analytics endpoint forms" below.** The verdict was read off
`/analytics/largest-moves`, the access path the index was built for, where the window cap holds a
page to at most five children: page 1 at the default and `min_move_pct = 0` plans 39 blocks on W-HOT
and 36 on W-COLD — both warm-connection numbers for a different statement, set beside W-WIDE's 852
only for scale — and executes 9 against 121. Execution is where the two windows differ, and the
index is kept. The deep-page half is measured there too: over W-WIDE's span,
the deepest `/bars` page for `AAPL`, with 575,961 rows before its cursor, plans 8 blocks where page
1 plans 852, because the cursor prunes what the window does not.

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

**The prediction that cost is flat in the limit is confirmed**: the aggregation spans the
window, not the page, so 414 blocks is the whole window's cost at every limit measured. The
1,001 row is `AGG_PAGE_MAX` plus one and was measured on 2026-09-08 for that reason — the
original run stopped at 101, so the cap itself had been inferred from flatness rather than run.

This window holds 62 trading days, which is why fetch 65 and 101 return the same 62 rows. The
bound is not 62: `AGG_MAX_WINDOW_DAYS = 90` is checked as `(end - start).days > 90`, so a legal
window spans at most 91 calendar days. Measured against the loaded calendar on 2026-09-09 — the
1,484 rows of `market_days`, every 91-day span in them — the maximum is **64 trading days**, and
the maximum over a 90-day span is also 64. (`docs/METHODOLOGY.md` records the 58 minimum and 64
maximum over 90-day windows as a sizing input; the 91-day figure is measured here because a
legal window is 91 days and a maximum over a narrower class cannot bound a wider one. The
arithmetic ceiling is 65, since 91 days is exactly 13 weeks and so exactly 65 weekdays; holidays
hold the observed maximum at 64.) 64 is the bound, and it is below `AGG_PAGE_DEFAULT = 100` — so
no legal `/daily` window can fill a page at the default, there is no page 2 at the default limit,
and the aggregating caps are not what bounds this endpoint's cost. `AGG_PAGE_DEFAULT` and
`AGG_PAGE_MAX` are both measured here and recorded, and Feature 7 confirmed both on
`/analytics/largest-moves`, the one endpoint reading them that can fill a page at the default
limit — see "The analytics endpoint forms" below.

Planning, which the block counts above exclude as they do for `/bars`: **571 blocks, 1.93 ms**,
flat across every fetch — measured at 51, 65, 101 and 1,001, and re-measured at all four on
2026-09-09. On a warm connection the whole page is therefore 985 blocks, not 414.

**On a connection that has not planned the statement, the whole page is 6,149 blocks.** Measured
2026-09-09 over three fresh connections, first statement on each: **5,729 planning blocks** on
all three, against 420 execution blocks — the 420 rather than 414 being the same first-execution
catalog read this subsection discusses two paragraphs below. That is **6.2× the warm whole-page
figure**, and it is the same shape as `/bars`'s 6,348: on a partitioned table the first plan of
a statement on a connection is where the partition count is paid, and a pool hands every new
connection that cost once.

This bears directly on the Class A number below. **The <100 ms comparison that follows is a
warm-connection number.** Planning on a fresh connection measured 5.98, 6.03 and 6.35 ms in the
three runs above, so the first request a pooled connection serves costs roughly 6 ms of planning
on top of its execution rather than 1.93 — still inside the target, but the target is met by the
warm figure and not by the cold one being small.

**This is the wrapper's number and not the file's, and the two are not directly comparable.**
The table above publishes the unwrapped `06_daily_rollup.sql` at 26.7 ms over 420 blocks for
these same bound parameters — but under a fresh connection per query, where this subsection
measures inside one session. Measured like for like on 2026-09-08, both forms in one session at
the same parameters, the unwrapped file reads `420, 414, 414, 414, 414` and the wrapper reads
`414` five times out of five. **The outer `LIMIT` and `ORDER BY` cost zero blocks, not six**;
the six-block difference between 420 and 414 is the first-execution catalog read this document
already attributes to the connection model under "Measurement conditions". An earlier form of
this paragraph read the six blocks as the wrapper's cost, which would have made a strict
superset of work measure cheaper than the work it contains.

**The Class A number for the `/daily` rollup path in its endpoint form is 24.05–24.51 ms
execution — 22.95–28.41 ms across the twenty underlying runs, and about 26.3 ms once warm
planning is counted — against the <100 ms target.** The twenty are four fetches at five runs
each; the worst single observation, 28.41 ms, is in the fetch = 1,001 row this document added
after the first three were published, which is why an earlier form of this sentence gave the
spread over fifteen. Section 3's gate for the feature that ships it is the test suite, so this
number is measured and reported rather than gating anything.

**Narrowing against filtering, on the same page 2.** Page 1 at limit 30 returns its 30th row on
2026-05-13, so page 2 binds `:start = 2026-05-14`. The alternative leaves `:start` at
2026-04-01 and cuts the page with an outer `WHERE day > '2026-05-13'`. **Both rows below are a
single observation each, not a median**, which is why the column is headed as it is:

| page 2, fetch = 31 | root blocks | ms (n=1) | partitions opened |
| --- | ---: | ---: | ---: |
| narrowing `:start` | **221** | 14.08 | 2 |
| outer `WHERE day >` | **414** | 18.09 | 3 |

On execution the narrowed form reads 47% fewer blocks and runs 1.3× faster, because `:start`
also drives the scan bound inside the committed query while the outer predicate throws away rows
the aggregate has already built. The plan carries a `Limit` above the aggregate, which is what
distinguishes the outer bound from an inner one.

**Both rows above are execution only, like every other cell in this subsection, and here the
omission changes the size of the result rather than only adding to it.** The saved plans carry
planning for both variants — narrowed **567 blocks / 2.427 ms**, filtering **574 / 2.123** —
and planning barely moves between them, because both forms plan the same committed query over
nearly the same children. Whole page:

| page 2, fetch = 31 | whole-page blocks | whole-page ms (n=1) |
| --- | ---: | ---: |
| narrowing `:start` | **788** | 16.503 |
| outer `WHERE day >` | **988** | 20.216 |

**20.2% fewer blocks and 1.22× faster, not 47% and 1.3×.** Narrowing is still the right design
and the reason for it is unchanged. The two halves shrink by different amounts and the sentence
that said "a little less than half" was distributing one fraction across both: whole-page keeps
**43.4%** of the block advantage (20.24 of 46.62 percentage points) and **79.0%** of the time
advantage (1.2250 against 1.2848).

The partition count is an observation and not a target: `bars` is partitioned by month, so
narrowing removes a partition only on the pages where it crosses a month boundary. This page
does — `:start` moves from April into May — which is why the narrowed plan opens two children
and the filtering plan three. On a page landing mid-month the narrowed form would open the same
three children as the filtering form, so no partition drops out and the saving is smaller than
this page's; how much smaller has not been measured.

### The analytics endpoint forms — Class A, the page caps and the deep page

Measured 2026-09-14 against the same database, read-only: `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`
on the exact statements `/analytics/volatility`, `/analytics/gaps` and `/analytics/largest-moves`
run, one recorded warm-up and then five runs each, median reported.

**Four conditions this subsection does not inherit.** First, each SQL harness ran in **one psycopg
session with automatic statement preparation turned off**, so every run is planned the way an
endpoint's first executions on a connection are. The service's own pool leaves psycopg's automatic
preparation on, so its connections prepare a statement after five executions, and no plan in the
tables below was captured from a prepared statement — the one exception is "The single-symbol deep
page" further down, whose custom-versus-generic comparison measures exactly that. Every SQL median
is a warm-connection number: each `EXPLAIN` median excludes a recorded first run, and every such
first run from the 2026-09-14 measurement is in "First runs" at the end, while the client-side
timings and the HTTP p50s include their first runs. The 2026-09-15 re-measurement of the Class A
statements and the `/analytics/largest-moves` page cells — one harness run over seven cells, five
runs each, the W-HOT `fetch = 101` cell being the Class A row itself — excluded a first run on each
of six of them, which that table does not carry: 168.528 ms on `/analytics/volatility`, 24.669 ms on
`/analytics/gaps`, and on `/analytics/largest-moves` 1.407 ms on W-HOT at `fetch = 101`, 7.047 ms on
W-HOT at `fetch = 1,001`, 14.383 ms on W-COLD at `fetch = 101` and 9.57 ms on W-COLD at `fetch =
1,001`. Second, the execution block counts of the Class A table, of both W-HOT cells, of the deep
page, of both `/bars` pages and of both threshold readings were **cross-checked**: the JSON root
node's count against the first `Buffers:` line of the same statement's text plan, and they agreed on
every one. The `OFFSET` statement and the 2026-06-25 page were not cross-checked, and no planning
count here is. The W-COLD cells were independently re-measured and cross-checked on 2026-09-15: the
same two-way agreement held on all ten runs (five per fetch), reproducing the 121-and-1,024 block
figures below to the block, at medians of 0.721 ms and 5.828 ms — the millisecond figures published
at those two cells below are from the original 2026-09-14 measurement, 0.666 ms and 5.624 ms. Third,
every time labelled HTTP is **end-to-end** through the compose `app` service, the form 6.3 #13 gates
the deep page on; every other time here is SQL. Fourth, **every `/analytics/largest-moves` figure is
for the statement as it is now**, joined to `market_days` on the session bounds alone, **at
`min_move_pct = 0`** unless it says otherwise. The exceptions say so where they appear, and why they
exist matters more than anything else in this subsection.

**Block counts are execution only**; planning has its own column, as in the `/bars` subsection
above.

**What `min_move_pct = 0` returns, exactly.** Every regular-session bar in the window **whose open and
close are both a real number**. That is a deviation from "no filter at all" worth stating rather than
leaving to a reader of the SQL, because `db/schema.sql` declares `open` and `close` as bare `numeric`
columns with no constraint, and the move is a percentage of the open — so three values the database
accepts are values this division has no answer for, and each one fails differently.

A **zero** open is `division by zero`, which is not one missing row but a 500 for the page that reads
it; whether every page of the window fails depends on how far each page's scan range reaches, and the
statement is an ordered walk under a `LIMIT`, so a page that never reaches the row never evaluates the
division on it. A **NULL** open makes the threshold comparison NULL, so the row is dropped whether or
not the guard is there — the guard is not what excludes it, and "not zero" does not describe it. A
**NaN** in either column passes both `<> 0` and `abs(...) >= threshold`, because Postgres orders NaN
above every number, and then reaches the wire as the bare token `NaN`, which no JSON parser accepts:
one such row costs every client the whole page rather than one row. The statement therefore carries
`AND b.open <> 0 AND b.open <> 'NaN'::numeric AND b.close <> 'NaN'::numeric`, and
`db/queries/05_largest_moves.sql` carries the same three.

The ingest refuses a zero open (`ingest/validate.py`) and is the only thing that does, so the guard
covers rows written by any route that bypasses it — and it does not cleanly refuse a NaN, since
`Decimal('NaN') <= 0` raises rather than returning False. **None of the three exists in the loaded
database**, measured over all 41,668,537 rows — zero opens 0, null opens 0, NaN opens 0, NaN closes 0
— so no figure in this subsection moves either way. Adding the two NaN clauses leaves the plan for
`db/queries/05_largest_moves.sql` structurally identical, node for node: they join the existing
`Filter` on the three partition scans and change no scan, no join order and no pruning.

`:symbol = AAPL`, `:start = 2026-04-01`, `:end = 2026-06-30` — the window Class A is bound at above.
`/analytics/largest-moves` takes no symbol; it binds `min_move_pct = 0` and `limit =
AGG_PAGE_DEFAULT`, so `fetch = 101`, and its page 1 binds the window's own lower instant as the
cursor.

| endpoint | runs (ms) | median | execution blocks | planning | target | |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `/analytics/volatility` | 52.37 47.54 47.82 48.11 48.27 | **48.11 ms** | 414 | 571 blocks, 2.52 ms | <100 ms | pass |
| `/analytics/gaps` | 27.35 29.11 24.34 26.17 24.26 | **26.17 ms** | 418 | 574 blocks, 2.49 ms | <100 ms | pass |
| `/analytics/largest-moves` | 0.921 0.921 0.922 0.935 1.945 | **0.922 ms** | 9 | 39 blocks, 0.18 ms | <100 ms | pass |

**All three medians pass; `/analytics/volatility`'s first run did not.** It was the first statement
on a fresh connection and read **108.17 ms of execution** — above the 100 ms target, at **423
execution blocks** against the 414 its median settles at — plus **4,875 planning blocks in
153.19 ms**, where its planning then settled at 571 blocks. `/analytics/gaps`'s first run, on a
connection that had already run other statements, reads **428 execution blocks** against its own
418-block median, in 31.84 ms. The gate is the five-run median, as it is for every Class A row.
Like `/daily`'s, **these are warm-connection numbers**, and volatility's first run on a fresh
connection was not inside the target.

#### `/analytics/largest-moves` — the page caps

`AGG_PAGE_DEFAULT` and `AGG_PAGE_MAX` govern `/daily` and all three analytics endpoints. `/daily`
cannot fill a page at either (above), and volatility and gaps take no page at all, so the pair is
set from `/analytics/largest-moves`. It was measured on page 1 over two windows. W-HOT's children
carry the hot-window partial index. W-COLD, the same calendar window a year earlier, is on children
that carry none, so the planner reads each child's `_ts_symbol_idx` with an `Index Scan` and visits
the heap for `open` and `close`.

| window | fetch = 101 | fetch = 1,001 | planning |
| --- | ---: | ---: | ---: |
| W-HOT, 2026-04-01 → 2026-06-30 | **9** blocks, 0.922 ms | **14** blocks, 5.811 ms | 39 blocks |
| W-COLD, 2025-04-01 → 2025-06-30 | **121** blocks, 0.666 ms | **1,024** blocks, 5.624 ms | 36 blocks |

Every cell above is a `Nested Loop` over the ordered per-partition scans, and none carries a `Sort`.
On W-HOT, whose text plans were saved, the loop's inner side is the window's 62 sessions, read once
from `market_days_pkey` and materialised.

**Verdict: `AGG_PAGE_DEFAULT = 100` and `AGG_PAGE_MAX = 1000` are confirmed, not moved.** At
`min_move_pct = 0`, page 1's worst reading at the cap is **W-COLD at 1,024 execution blocks** —
8.00 MiB — in 5.62 ms, and **1,060 whole-page**. At the default, page 1's worst is W-COLD at 121
execution blocks and 157 whole-page. On W-HOT those read 14 and 53 at the cap, and 9 and 48 at the
default. **On W-COLD, a cursor at a session's last minute reads more than page 1 does; on W-HOT the
same kind of cursor does not.** Measured 2026-09-15 at the last
minute of two sessions early in each window — the window's own first session, and the session
before the longest gap to the next one (2025-04-01 and 2025-04-17 on W-COLD; 2026-04-01 and
2026-04-02 on W-HOT) — neither cursor deep into either window. Eight cells, three runs each, with
one text plan taken per cell and its block count compared against all three of that cell's JSON
runs: 24 comparisons, all agreeing. On W-COLD such a cursor reads 1,048
or 1,053 execution blocks at the cap and 145 or 150 at the default; on W-HOT the same two picks
read 9 and 14, identical to page 1. The extra blocks on W-COLD are the out-of-session run the scan
crosses before it reaches a qualifying row. **How deep the cursor sits was not varied**: every
cursor here is early in its window, so what a deep W-COLD cursor costs is unmeasured. Two deep
cursors are measured elsewhere in this subsection and neither is on W-COLD's window: the analytics
deep page at `DEEP_PAGE_DEPTH`, whose cursor sits at 2026-05-15 and which reads **8 execution
blocks** on each of five runs ("The deep page" below), and `/symbols/AAPL/bars`' own deepest page,
which reads **20 execution blocks**, against 8 planning blocks over its one surviving child ("The
single-symbol deep page" below). That does
not move the verdict — the four page-1 readings above stay the numbers a
cap is sized from, and the caps bound rows returned rather than rows read (below), so a cursor
reading more blocks at the same page size is not a reason to move either cap. Neither the
page-1 nor the mid-session readings are near a limit worth lowering a cap for, and raising the cap
has no measurement asking for it. One pair, four endpoints, confirmed at these numbers.

**The caps bound the rows a page returns, not the rows it reads, and a positive threshold separates
the two.** At `min_move_pct = 0` every bar passes the threshold, so the loop stops once `limit + 1`
bars inside a session have joined. A rarer threshold makes the same loop read on until it has found
that many. On W-HOT page 1 at the default limit, one first run and three runs each: at `min_move_pct
= 1` the page read **3,208 blocks in 128.3 ms** to return its 101 rows, and at `min_move_pct = 100`,
which no bar in the window meets, it read every bar in the window through the hot-window index —
**12,039 blocks in 518.1 ms** (three `Index Only Scan`s on `hot_idx`, 3,976 + 3,890 + 4,173 blocks,
0 heap fetches; the same three children's heaps hold **24,889 blocks** in total — 8,629 + 8,219 +
8,041, read off the "as first shipped" reading's parallel sequential scans over these same children
below, not measured on this index-only statement) — and returned none. Timed from
the client, three runs each as above, their medians read 130.3 and 493.8 ms. The 1% page is above the Class A target, whose pinned
threshold is 0. A smaller limit shortens that read only by stopping at fewer qualifying rows, and
not at all for a threshold nothing meets.

**That is W-HOT's cost, and 67 of the 71 partitions carry no hot-window index.** Three of those 67
were measured — `bars_2025_04`, `bars_2025_05` and `bars_2025_06`, the children W-COLD opens — and
on those three the same threshold reads far more. The other 64 are unmeasured. On W-COLD, page 1 at
the default limit, measured 2026-09-15, read-only, against a warm operating-system page cache: at
`min_move_pct = 100` the plan is a plain `Index Scan` on each child's `_ts_symbol_idx` — the
index every partition has — visiting the heap on every row because neither `open` nor `close` is
in it, and it reads **1,737,160 execution blocks**, in 1,508.8 ms on one single run and 943.6 ms on
a second single run half a minute later, to return 0 rows; at `min_move_pct = 1` it reads
**126,182 blocks**, in 80.9 ms on the first of two runs and 47.1 ms on the second. None of those
four timings is a median.

The caps bound only what a page returns, and `min_move_pct` is unbounded. In place of a bound on
the parameter a request is bounded by the timeouts below — and a threshold scan over a window
without the hot-window index is the one statement published here that can reach them.

#### The bounds a request runs under

Four bounds sit between a client and the database:

| bound | constant | value | what the client sees when it is reached |
| --- | --- | ---: | --- |
| waiting for a pooled connection | `POOL_CHECKOUT_TIMEOUT_SECONDS` | 5 s | 500 `internal` |
| one statement on a pooled connection | `STATEMENT_TIMEOUT_SECONDS` | 5 s | 500 `internal` |
| the check that connection answers before it is handed out | `POOL_CHECK_TIMEOUT_SECONDS` | 1 s | that connection is discarded and replaced |
| `/health`'s own probe | `HEALTH_TIMEOUT_SECONDS` | 2 s | 500 `internal` |

**The first and third compose, so a checkout is bounded at about six seconds and not at five.** The
pool waits for a connection and then checks the connection it took, and only the wait is bounded by
the checkout deadline: in `psycopg_pool` the wait is given whatever is left of that deadline and
the check that follows it is given nothing, which is why the check carries a deadline of its own
here. A
request can therefore spend the full 5 s waiting and then up to 1 s more being handed a connection
that turns out to be dead, and one that meets several dead connections in a row also pays the
pool's own retry backoff between checks. **That six seconds is arithmetic over the two published
constants, read from the pool's source; no measurement drove the wait to its full 5 s and then
failed a check on top of it, and nothing in the suite bounds the two composed.** What the suite
holds is each of the four constants by value, and a bound of `POOL_CHECK_TIMEOUT_SECONDS` plus a
second on a request that meets a silent pooled connection and still answers 200. What was measured
is the case below, where the wait is short and the check is what costs: 6.03-6.16 s.

**Where each figure below was taken.** The plan and block measurements everywhere else in this
document are read-only against the loaded database. Everything in this subsection is measured
against scratch Postgres containers instead — one of them a replica built to W-COLD's shape —
because what is under test is a database that is slow, silent, full or gone, which is not a state
to put a loaded one in.

**The two 5 s bounds, over HTTP.** With a pool of one connection, a statement cancelled by the
statement timeout answers 500 `internal` in 5.024-5.094 s on each of four routes — two that read
bars, `/symbols/{symbol}/bars` and `/symbols/{symbol}/daily`, and two analytics,
`/analytics/volatility` and `/analytics/largest-moves`; `/symbols` and `/analytics/gaps` were not
measured this way — and the connection comes back to the pool clean, idle and pinned to UTC. A
request that waits out a busy pool answers 500 at 5.005-5.019 s, measured over the forty requests
that queued rather than the ones holding a connection. Each bound is **per phase, not per request**:
a route that runs two statements holds its one checkout across both, so `/symbols/{symbol}/bars`
with `symbols` locked for 4.5 s and then `bars` locked answered after 9.503 s, and end to end a
request can wait a worker thread plus one checkout plus one statement timeout per statement —
measured maxima 14.26 s at 41 concurrent requests and 34.27 s at 200.

**The 1 s check, and what it recovers.** The pool checks every connection before handing it out, by
asking it to answer an empty query, and that check is given a second of its own, so a pooled
connection that has gone silent is discarded and replaced inside that second rather than holding the
request for as long as TCP takes to notice. Measured with all ten pooled connections silenced and
their sockets left open — the shape of a failover, an expired NAT entry or a hung peer — 12
concurrent `/symbols` requests answer **200 in 1.20-1.22 s**. Against a check with no deadline of
its own, ten of the same twelve hang **66.29-66.37 s, median 66.35 s**, until the peer is released,
and the other two answer 500 at about 5.05 s. A serial client meets the same silence differently,
because it walks the silent connections one at a time: the first three requests answer 500 at
6.03-6.16 s, the fourth answers 200 in 1.04 s, and the rest answer in 14-25 ms — about eighteen
seconds of refusals while the pool replaces itself, against a client whose every request hangs, with
no recovery, when the check has no deadline.

**Keepalives, and the case they do not cover.** A pooled connection is opened with a 5 s connect
timeout and carries TCP keepalives — probes after 10 s idle, three of them 5 s apart — plus
`tcp_user_timeout` at 25 s, which libpq applies on Linux and ignores elsewhere. Those bound an
*idle* connection whose peer has vanished. They do not bound a connection that goes silent in the
middle of a statement: a request already in flight when its peer stopped answering was held until
the peer was released, 47.3 s, against 56.1 s for the same case without the deadline. That case is
bounded by neither the keepalives nor `tcp_user_timeout`, and not by the statement timeout either,
which Postgres applies on the server and cannot reach a client whose transport has gone silent; what
ends it is the peer.

**With a small enough pool, that case takes the whole pool while `/health` keeps answering 200.**
Every pooled connection can be held by a peer that stopped answering in the middle of a statement,
and `/health`, on its own connection, answers on a database it can still reach, so an orchestrator
reads a healthy target with no data route working. Measured with `DB_POOL_MAX=2` and both of the two
requests swallowed mid-statement: neither had returned 3 s, 15 s or 35 s after it was sent, and
`/health` answered **200 in 0.05-0.11 s** at each of those three points. Keepalives cannot end it,
because the peer's kernel keeps acknowledging what is sent to it, so the connection is alive at the
TCP level and there is no unacknowledged data for `tcp_user_timeout` to bound.

**`/health`.** It runs on its own connection, opened per check, outside the shared pool, under a 2 s
bound. Against a server that completes its startup exchange and then freezes from the first query
message, every probe answers 500 at **2.015-2.021 s** server-side in process, and 2.00-2.03 s
server-side over uvicorn — at the bound, not when the server is released. Across those uvicorn
probes the process held at 3 asyncio tasks and 10 open descriptors throughout and shut down in 0.246
s, with no traceback logged — a shutdown with no check in flight. A check that is in flight at
shutdown is cancelled rather than waited out, so shutdown does not carry the remainder of that
check's 2 s deadline either; the suite bounds that case at under two seconds. A probe still waiting
in flight when the **server** cancels it receives a **`text/plain` 500 from the HTTP server itself**,
not this service's `{"error": {...}}` — measured on a real uvicorn: the cancellation arrives as
`CancelledError`, a `BaseException`, so it passes through the route's own `except Exception` and
through the middleware that would otherwise build the body, and uvicorn's protocol layer answers
instead.

**The agent is uvicorn, not `checks.close()`.** The lifespan's own cancellation cannot produce this,
because `lifespan.shutdown()` runs only after uvicorn has finished waiting for in-flight requests —
measured by neutering `close()` entirely, which gives the identical `text/plain` 500. What produces
it is uvicorn cancelling the request task, and there are **two** ways to reach that, not one: a
`--timeout-graceful-shutdown` shorter than the probe's remaining deadline, and a second `SIGINT`,
which sets `force_exit` and skips the wait (a second `SIGTERM` does not). So making `close()`
gentler, or removing the route's `asyncio.shield`, changes none of it.

**A monitor must not read either "no response" or a `text/plain` 500 as the signature of a
shutdown, because a shutdown produces both.** Two measured outcomes give no bytes at all, and the
first needs no graceful-shutdown timeout and so is what the deployed configuration does: uvicorn's
first act on `SIGTERM` is to close every connection with no request in flight, so a monitor holding a
kept-alive socket — which is the default, and what an ALB, a kubelet probe and a blackbox exporter
all do — gets **zero bytes on its next probe**, measured at 0.000 s. The second is `SIGTERM` followed
by `SIGKILL`, which is what `docker compose stop` does once `stop_grace_period` expires, and the
compose file leaves that at the 10 s default. Treating silence as "the service is down" therefore
pages someone on every rolling restart.

Two further outcomes escape the single error shape and are not shutdown-specific: a request the HTTP
server refuses before the app sees it answers 400 as plain text, and one whose headers it cannot read
is closed with no response. This list is the set measured, not a proof there is no other.

The service as deployed sets no graceful-shutdown timeout, so uvicorn waits — for **every** in-flight
request, with no bound, not merely for the probe — and a probe finishes at its own 2 s bound with a
correctly shaped JSON 500.
Probes that overlap share one check rather than each opening a connection of their own, so the log carries one line per check and not per probe — 13 lines for 14 probes, the second
having joined a check already in flight and answered in 0.76 s, server-side like the two ranges
above. The sharing is what holds under a flood: 60 concurrent probes against a container configured
with `max_connections=16` answered **200 on all 60** in each of three runs, an administrative
connection connected during every run, and the service held at most 6 client backends — where a
connection per probe leaves 45, 45 and 47 of the 60 answering 500 and refuses the administrative
connection on all three.

**Sharing a check means a success can be as stale as the check that produced it.** A probe that arrives
while a check is in flight waits on that check instead of opening a connection of its own, so what it
is told is the state that check found when it opened its connection and not the state at the moment
the probe arrived. Measured against a peer that held the check's answer for 1.5 s: a probe that
arrived 1.0 s into a check was answered **200** by that check, although the database had stopped
accepting connections 0.5 s into it, and the probe after it answered 500. A success is therefore
stale by at most `HEALTH_TIMEOUT_SECONDS`, 2 s, and the first probe to arrive after a check has
finished opens a connection of its own.

**A pool that cannot produce a usable connection is invisible to `/health`.** `/health` opens its
own connection outside the pool, by design, so it answers on a database the pool cannot use. A peer
that completes a connection and then refuses the session `SET` the pool's configure step runs is
that case: the pool discards the connection it has just made and retries with a widening gap, and
each chain of attempts that gives up is replaced by a fresh one at the next checkout that finds the
pool short — so the retrying continues for as long as both the cause and the traffic do, every data
route reaches the checkout bound, and `/health` keeps answering 200. Against such a peer `/health`
answers 200 while every data route answers 500 at the checkout bound; the readings behind that
sentence were not captured, and what is measured for the shape of it is the wedged-pool case below.
An orchestrator reading `/health` alone keeps that target in service. What an operator reads instead
is the service log: the configure step logs one line at ERROR per failed connection attempt, naming
the exception type and its message, with any password in the DSN that pool was built with replaced
by `***`. A refused `SET` carries no connection string, so nothing in that line can hold one. A
refused *connection* is the case where it can — libpq quotes a connection string it cannot parse
back in its own message — and there the pool library logs the same exception under its own logger,
where this service does not mask it: an operator who must keep a password out of the log keeps it
out of the DSN, in a password file or the environment, where no message can echo it.

**A refusal does not wait for a worker thread, with one exception.** Under 45 concurrent slow
requests holding the thread pool, a 400 for a malformed parameter answers in 0.039 s and a
`/health` 500 in 0.024-0.038 s. The exception is a refusal computed inside the endpoint rather than
before it — a page limit outside its permitted range — which runs on the endpoint's own thread and
so waits with everything else: **2.662 s** under that same burst.

**A threshold scan can reach the statement bound, and it takes a cold cache to do it.** The two
W-COLD readings above, 1,508.8 ms and 943.6 ms for a 1,737,160-block scan, were taken against a
warm operating-system page cache and finish well inside 5 s. Against a cold one they do not. On a
scratch replica of W-COLD's shape — 1,740,960 bars at 81.0 rows per page against the loaded
database's 81.06, the same plan, 1,749,943 execution blocks against its 1,737,160 — restarted and
with its own data files evicted from the host's cache before each reading, `EXPLAIN ANALYZE` read
**4,862.7, 5,257.9 and 5,371.9 ms**, of which 3.84-4.16 s was disk; the warm second run of each
read 1,748.5, 3,311.3 and 1,732.1 ms. Through the service at `min_move_pct = 100`, the first
request after each of three cold starts answered **200 in 3.12 s, 500 `internal` in 5.215 s, and
200 in 3.47 s**, and each of those was followed by a warm 200. Ten concurrent cold requests all
answered **500, at 5.07-5.19 s**; the same ten warm all answered 200 at about 3.30 s. So the
statement bound is not comfortably clear of this statement: it is what decides whether a cold
threshold scan is served or refused. What a cold request against the loaded database costs was not
measured — the replica is the evidence, and it was taken on a machine carrying other work.

**Read the threshold figures as what they are.** The slowest threshold **median** published anywhere
in this document is **518.1 ms**, W-HOT at `min_move_pct = 100` over three runs. Three single
readings against the loaded database are slower — W-HOT's first run at that threshold, 1,590.699 ms
("First runs" below), and W-COLD's two 1,737,160-block readings, 1,508.8 ms and 943.6 ms — and not
one of the three is a median. **The cold-cache readings in the paragraph above are slower again, and
they are the scratch replica rather than the loaded database**: 4,862.7, 5,257.9 and 5,371.9 ms over
SQL against 1,748.5, 3,311.3 and 1,732.1 ms warm, a 500 `internal` at 5.215 s through the service,
and ten concurrent cold requests refused together at 5.07-5.19 s. Nor does the checkout bound follow
from any threshold reading here: it is reached by concurrency rather than by one slow statement,
when all `DB_POOL_MAX` connections are busy at once and a caller waits out the whole 5 s behind
them. At the default maximum of ten connections, a burst of simultaneous
callers each holding one for 1.5 s drains in waves of ten every 1.5 s, so the wait first passes 5 s
at the forty-first caller; eleven such callers leave the eleventh waiting about 1.5 s, and it is
served. Eleven simultaneous callers were measured, against a table locked for 8 s, which is longer
than the statement bound: the ten holding connections were refused by the statement timeout at
5.019-5.041 s each, and the eleventh was served **200 in 7.025 s** — by the statement bound, not the
checkout bound.

**What that leaves a client.** `min_move_pct` takes no bound: the caps bound rows returned and this
parameter decides rows read, and bounding it would refuse a legitimate question rather than answer
it. Three things follow, and they are the honest reading. A 500 from `/analytics/largest-moves` at a
high threshold is a timeout and not a bad request, so it carries no information about the request's
validity. A retry has a good chance, because the refused attempt leaves the window in cache — on the
one cold start of three whose first request was refused, the retry answered 200 in 3.137 s, and that
is a single observation. And concurrency is the case retrying does not fix — ten simultaneous cold
requests were refused together — so a client that needs the answer reliably narrows the window
rather than raising the threshold. `bars` and its children are 6,998,122,496 bytes with their
indexes, the 6,674 MB above, against a machine that cannot cache all of it beside everything else,
so a cold window is not only a post-restart state.

#### Why a page is usually an index descent, and where it still is not

**The verdict above rests on a change to the statement, made in this feature and measured both
ways, at `min_move_pct = 0`.** As first shipped, the endpoint joined `bars` to `market_days` the way `06_daily_rollup.sql`
does: `m.day = (b.ts AT TIME ZONE 'America/New_York')::date AND b.ts >= m.open_ts AND b.ts <
m.close_ts`. The planner estimated that join badly. At page 1 with `fetch = 1,001` it put the join
at **1,280 rows per process**, where the three processes returned **669,829** each on average. The
day equality also made the join hashable. So past a threshold the planner gave up the ordered nested
loop, which stops after one page, for a **hash join that reads every row left in the window and
sorts them to return one page**. At page 1 and `fetch = 1,001` that was a parallel plan over
sequential scans of all three children. At a cursor on 2026-06-25 and the default `fetch = 101`, it
read all 136,357 rows the index-only scan of the last child returned, probed each against a hash of
the window's 62 sessions, and sorted the 136,038 the join kept, to return 101.

The threshold moved with the cursor. Planning-only probes of the first-shipped statement, at eleven
values of `fetch` from 101 to 1,001, put it between 501 and 751 on page 1, and between 251 and 301
at the 1,000,000-deep cursor below. At `fetch = 101`, a cursor at 13:30Z held the index plan on
2026-04-01, 2026-04-15, 2026-05-01, 2026-05-15, 2026-06-01 and 2026-06-10, and hashed on every
probed date from 2026-06-20 to 2026-06-30.

`06_daily_rollup.sql` keeps the equality on purpose — its own comment says the equality is what
gives the planner a hash — because it aggregates the whole window; a keyset page never does. Every
one of the 1,484 sessions in `market_days` opens and closes (exclusive) on its own New York date,
checked read-only on 2026-09-14. So the bounds alone imply the equality and match at most one
session per bar. The statement now joins on them alone, and with no equality clause neither a hash
join nor a merge join is available to the planner. The same planning probes on the changed statement
found no hash join and no sort at any of the eleven probed values of `fetch`, on page 1 and at the
deep cursor, or at any cursor position probed on W-HOT (see the exception below). The integration
suite holds that in two ways, because one is not enough. A plan-shape test asserts the plan carries
no hash join on a fixture where the rollup's own New York date equality already produces one; that
test catches the equality in that spelling and does not catch a UTC date cast of the day column,
which plans like the shipped statement on a small unanalysed fixture: on such a fixture a plan-shape
probe reads no hash join for that spelling at `fetch = 101` or 1,001, where the rollup's own New
York date equality reads one at both. What a UTC-cast spelling does at the loaded database's shape
was not measured. So the join is pinned by its **text** as well: the statement is read the way
Postgres reads identifiers in it — comments dropped, unquoted names folded to lower case,
unnecessary quoting removed — and the join has to be the half-open pair alone followed by the two
day bounds, with no second reference to a `market_days` column anywhere in the statement. A
respelling is refused as the reference it is rather than as the characters it is written with.

| reading | as first shipped | joined on the session bounds alone |
| --- | ---: | ---: |
| page 1, fetch = 101 | 524 blocks, 0.252 ms | 9 blocks, 0.922 ms |
| page 1, fetch = 1,001 | 25,035 blocks, 840.11 ms | 14 blocks, 5.811 ms |
| 1,000,000 rows in, fetch = 101 | 307 blocks, 0.175 ms | 8 blocks, 0.581 ms |
| cursor 2026-06-25 13:30Z, fetch = 101 | 822 blocks, 139.19 ms | 9 blocks, 0.627 ms |

The 2026-06-25 row is three runs after a warm-up, not five. Over HTTP at the default limit, that
page answered in **139.14 ms at the median, 28.97× page 1**, before the change, and in 3.02 ms after
it (table below).

The change trades index probes for an in-memory filter. The equality let the loop probe
`market_days_pkey` once per bar — 519 of the first-shipped page 1's 524 blocks were those probes.
Without it the loop filters each bar against the 62 materialised sessions, and the plan reports
**10,564 rows removed by the join filter** on page 1. **That counts rejected (bar, session)
comparisons, not bars read.** 172 bars against 62 sessions is 10,664 comparisons, of which the page
keeps 101, leaving 10,563 — one short of the reported figure. The bars-read reading is impossible
beside the same page's 9 blocks, and the first-shipped page puts the bar count in the same place
from the other side: 519 probe blocks at three blocks per primary-key descent is about 173 bars. So
blocks fall in every row of the table, while the `EXPLAIN ANALYZE` times in the first and third rows
rise. Timed from the client, without `EXPLAIN`, the rise is smaller: page 1's median runs 1.20 ms
where it ran 1.12, and the deep page's median 1.14 ms where it ran 0.95. Over HTTP the p50s were
4.80 ms before and 3.33 ms after for page 1, and 4.18 and 3.74 ms for the deep page. Every one of
those times is far inside the Class A target. **The 4.80 and the 4.18 are medians taken in a single
session whose individual runs this document does not publish**, where 3.33 and 3.74 each carry an
eleven-run series in the deep-page table below — so only the after side of that pair can be read
run by run.

**The fix is not universal, and the exception found is a window's own last session, when that
session also ends a partition without the hot index.** Such a cursor leaves the ordered plan
for a `Bitmap Heap Scan` with a top-N `Sort`, and only over the closing
slice of that session. **The condition is an inference, not a measured rule**, and it is worth
reading as one: five windows were swept, two flip and three do not, and only one of the three
non-flipping windows tests the partition-end half of the condition. Nothing was measured that
isolates that half from the missing index. The flip is **not confined to `min_move_pct = 0`**: the
sweep ran at 0 and at 1 and the two thresholds flip identically, cell for cell — 84 flipped cells
of 84 on W-COLD and 67 of 67 on the 2020 window, each count being half of that window's flipped
total. On W-COLD (2025-04-01..06-30, last session 2025-06-30), swept every 5
minutes: at `fetch = 101` the flip covers 11 of 79 five-minute positions checked (2025-06-30
19:00Z-19:50Z), at `fetch = 251` 22 of 79 (18:05Z-19:50Z), at `fetch = 1,001` 51 of 79
(15:40Z-19:50Z); the session's last two positions, 19:55Z and 20:00Z, do not flip at any fetch.
`EXPLAIN ANALYZE` at 2025-06-30 19:00Z, `fetch = 101`, reads 28.4, 30.4 and 28.3 ms against
0.62–0.76 ms on the index path (bitmap scans disabled), and client-timed five-run medians read
13.6 ms against page 1's 1.22 ms; at 15:40Z, `fetch = 1,001`, 194.1, 104.7 and 101.4 ms against
5.8–6.2 ms, client-timed five-run medians 48.7 ms against 5.4 ms — three runs behind each
`EXPLAIN ANALYZE` figure, five behind each client median. The 2020-08-01..10-30 window flips the
same way and into the same plan — a bitmap scan and a sort, with no hash join, which is what every
one of its 134 flipped cells shows. It was planned, not executed and timed: at `fetch = 101` the
flip covers 8 of 79 positions (2020-10-30 19:05Z-19:40Z), at `fetch = 251` 17 of 79
(18:20Z-19:40Z), at `fetch = 1,001` 42 of 79 (16:15Z-19:40Z). Swept and found not to flip: W-HOT itself,
whose own last session sits in a hot partition; 2025-10-15..2026-01-12, whose last session
(2026-01-12) is not its partition's last day; 2026-02-01..04-30, whose last session sits in the hot
`bars_2026_04`; and every month-end session that is not a window's own last session — 0 of 1,551
cells swept over seven such sessions in three cold windows. The cause is a second estimate the
join-bounds change did not touch: the planner multiplies the redundant `b.ts >= after_ts` bound's
selectivity by the row comparison's, so at 2025-06-30 19:00Z the bitmap index scan is estimated at
43 rows where the bitmap heap scan returns 4,731, and a bitmap scan feeding a sort whose input the
planner estimates at 98 rows looks cheaper than the ordered scan. The sort
this produces stays bounded — it sees only the rows left in the window's own final session. W-HOT
never shows it: the same estimate collapses there too, but its own index-only descent already wins
on cost before a bitmap plan enters the comparison — an inference from the estimate collapsing the
same way, since no bitmap plan was ever forced or costed on W-HOT itself. Section 4's join
predicate is kept as written; this tail is documented rather than designed around, and it is why
6.3 #13's condition (d) — no Sort at the deep cursor — is checked at its one pinned window, W-HOT,
rather than asserted for every window a client could request.

#### The deep page — keyset at `DEEP_PAGE_DEPTH`

The cursor was derived by `OFFSET 1000000 LIMIT 1` over the endpoint's own statement with its inner
`fetch` raised past the offset, and all five runs of that statement named the same row,
`2026-05-15T15:35:00+00:00` and `CVX`. That row has exactly 1,000,000 rows before it in the
endpoint's own `(ts, symbol)` order. Every deep-page reading below uses that cursor, encoded by the
endpoint's own encoder.

| page | HTTP runs (ms), 11 interleaved rounds | HTTP p50 |
| --- | --- | ---: |
| page 1 | 59.3 3.6 21.7 3.3 3.3 3.8 3.1 3.6 2.8 3.3 3.0 | **3.33 ms** |
| 1,000,000 rows in | 8.0 4.9 3.7 2.9 4.9 4.2 3.1 3.8 2.6 3.1 3.2 | **3.74 ms** |
| cursor 2026-06-25 13:30Z | 3.8 3.8 4.6 2.7 6.8 2.9 2.9 2.9 3.0 3.0 3.3 | **3.02 ms** |

**6.3 #13 passes.**

(a) The cursor is 1,000,000 rows in by construction of the `OFFSET`.

(b) Every deep response held 100 rows and a non-null `next_cursor`, asserted on all eleven.

(c) **Read as p50 against p50, both over at least five runs — the reading the owner fixed on
2026-09-15.** The deep page's HTTP p50 is **1.125×** page 1's, against a target of under 1.5×;
re-measured 2026-09-15, page 1's p50 is **3.343 ms** and the deep page's is **3.511 ms**, a ratio
of **1.050×**. The two readings are not equivalent, and which one the gate takes was settled by the
owner on 2026-09-15 rather than derived from the measurement. Read per request instead, the first
deep run of each eleven-round series is its slowest — 8.015 ms recorded, 10.382 ms re-measured —
and exceeds 1.5× the page-1 p50 on its own (a bound of 4.990 ms recorded, 5.014 ms re-measured).
Under the reading the owner fixed, that run sits inside the p50 statistic rather than being checked
against the bound by itself; under the per-request reading the gate would not pass. The document
publishes both so the verdict can be read against either.

(d) At the deep cursor the plan has **no Sort**. A plain `Append` over **2 children** —
`bars_2026_05` and `bars_2026_06` — is planned to read each through an `Index Only Scan` on its
own `hot_idx` with 0 heap fetches, as the outer side of the nested loop, but only the first
actually runs: the page fills from `bars_2026_05` alone, so `bars_2026_06` is listed in the plan as
never executed, and it is still counted as one of the two children. Execution is 8 blocks and
0.581 ms; planning is 19 blocks and 0.141 ms. With the redundant `b.ts >= after_ts` bound deleted,
the same statement at the same cursor plans **all 71 children**, and page 1 goes from 3 to 71 the
same way. Neither form prints a `Subplans Removed` line, and the cause is the bound's **type under a custom
plan**, not psycopg bindings in general and not the type on its own: `_instant_bounds` binds a
`timestamptz`, and while the statement is planned for the values in hand the planner can fold that
bound into a range and prune by it at plan time, so a pruned child never enters the plan for the
line to count. Under a generic plan the same `timestamptz` bound is not known at planning time and
the pruning moves to executor init, where it is counted — which is why the generic `/bars` plan
further down prints `Subplans Removed: 70`. `01_volatility.sql` and `03_gaps.sql` bind date-typed bounds through the same psycopg
call on the same connection and print `Subplans Removed: 68` — pruning that happens at executor
init instead, because a `date` bound is not foldable the same way. On a connection shaped like the
pool's, `_MOVES_SQL` itself stays on a custom plan — a probe found it generic on 0 of 41 executions after
psycopg's default five-execution threshold — so the plan measured here is the plan this endpoint
actually serves, unlike `/bars` above. Counting the children that survived is the measurement.
Planning is a second witness on a warm connection: 39 blocks on page 1 and 19 at the deep cursor.
That is not directly comparable to the 852 above, both because 852 is a different statement's
figure and because 19 is not the deep cursor's own number on a fresh connection: opened cold, the
same 2-child plan reads **595** planning blocks, measured 2026-09-15. A connection that has
already planned `_MOVES_SQL` is what the 19 describes.

Page 1's first **HTTP** request read 59.3 ms and one later page-1 HTTP request 21.7 ms. Each p50
is over all eleven runs, with nothing discarded. The 2026-06-25 row is not part of the gate: it is
the page that, as first shipped, cost 28.97× page 1.

#### The offset number

All three timed from the client over SQL, on one connection:

| statement | runs (ms, client) | median | blocks |
| --- | --- | ---: | ---: |
| keyset page 1, fetch = 101 | 1.397 1.199 1.195 1.200 1.199 | **1.199 ms** | 9 |
| keyset page 1,000,000 rows in, fetch = 101 | 3.095 1.136 1.025 1.996 1.134 | **1.136 ms** | 8 |
| `OFFSET 1000000 LIMIT 1` over the endpoint's statement | 5,224 2,243 2,061 2,020 2,175 | **2,175 ms** | 5,996 |

**The same depth reached by `OFFSET` costs 1,915 times the time of the keyset page at that depth and
750 times its blocks.** The `OFFSET` statement returns one row where the keyset page returns 101, so
the comparison favours it. Its blocks come from one `EXPLAIN (ANALYZE, BUFFERS)` run, whose own
execution read 5,716 ms. The times are five runs of the statement without `EXPLAIN`, and their
median includes the first of them, 5,224 ms — but the two keyset rows above are not on the same
footing: by the time their five runs were taken, the statement had already run seven more times at
the same cursor, so their medians carry no cold first execution the way the `OFFSET` row's does.
Dropping every series' first run instead, the ratio is **1,866×** rather than 1,915×. Numerically,
though, it is the keyset row's own first entry that stands out more relative to its series' median —
**2.725×** that median (3.095 ms against 1.136 ms) — against the `OFFSET` row's **2.402×** (5,224 ms
against 2,175 ms); the ratio still barely moves because the `OFFSET` series' first-run excess is the
larger one in absolute milliseconds.

As first shipped, the same `OFFSET` statement named the same row. It ran on three processes as a
hash join over sequential scans of the three children, with an external merge sort that spilled to
disk — 14,512 kB in the leader's share alone, the two workers' shares not recorded — at a median of
991 ms over 25,035 blocks, 1,040 times that version's keyset page at the same depth. The join change
made the `OFFSET` form 2.2× slower and 4.2× cheaper in blocks. It now walks the million rows in one
process, through index-only scans and the in-memory session filter, where the first-shipped plan
scanned the three children whole in three processes and hashed the join.

#### The single-symbol deep page, at its real maximum

`/symbols/AAPL/bars` over the whole ingested span holds **576,962** raw bars. That is the raw count,
because `/bars` carries no session join. 575,961 rows precede the cursor, so the deepest page holds
exactly the final 1,000 rows and has no successor, which the harness asserts.

| page | execution | planning | children | HTTP p50, 7 interleaved rounds |
| --- | ---: | ---: | ---: | ---: |
| page 1 | 19 blocks, 0.366 ms | 852 blocks | 71 | 22.12 ms |
| deepest | 20 blocks, 0.361 ms | 8 blocks | 1 | 21.30 ms |

Flat: the deepest page's HTTP p50 is **0.963×** page 1's. Page 1 is an ordered `Index Scan` on each
child's primary key under a plain `Append`. The deepest page's **custom** plan — what a fresh or
lightly used connection serves — is a bitmap scan of the one surviving child's primary key and a
small sort. Planning is the axis that moves, and in the deep page's favour: page 1 plans all 71
children in 852 blocks and the deepest page plans 1 in 8. Most of each HTTP figure is spent outside
the statement: page 1's execution and planning come to 2.11 ms of its 22.12. **The planning time in
that 2.11 is not in the table** — the table gives page 1's planning in blocks only, so the 0.366 ms
execution is the one half of the sum a reader can check against it. Re-measured 2026-09-15 over
seven interleaved rounds again: page 1's HTTP p50 is **19.913 ms**, the deepest page's is
**18.427 ms**, a ratio of **0.925×** — consistent with the figures above.

**On a connection shaped like the pool's, the deepest page's plan changes — because `api/deps.py`
sets no `prepare_threshold` and psycopg's default is 5.** The custom plan above is what the statement runs
for its first ten executions on a connection; from the eleventh it goes generic — `Index Scan` on
the surviving child's primary key, `Subplans Removed: 70`, no sort, 20 execution blocks and 0
planning blocks, measured 2026-09-15 on a connection shaped like the pool's. Page 1's client time
falls with it: the five custom-plan executions measured 15.531, 6.025, 9.967, 5.142 and 5.363 ms
(median 6.025 ms), and the three generic-plan executions measured after them read 4.492, 2.772 and
2.759 ms. A connection serving this traffic pattern switched to the generic plan at its eleventh
execution, and from there serves that plan rather than the one above. The threshold is psycopg's;
which plan Postgres then keeps is decided by estimated cost against the average custom cost, so a
connection whose `/bars` traffic is all narrow windows was not measured and may not switch.

#### First runs

Each `EXPLAIN` median above excludes a recorded first run, listed here with the median it was left
out of. "Fresh" is the first statement on a new connection; "used" is a connection that had already
run other statements.

| cell | connection | first run | median | first-run planning | median planning |
| --- | --- | ---: | ---: | ---: | ---: |
| `/analytics/volatility` | fresh | 108.174 ms | 48.107 ms | 4,875 | 571 |
| `/analytics/gaps` | used | 31.835 ms | 26.165 ms | 821 | 574 |
| largest-moves, W-HOT, fetch = 101 | used | 0.949 ms | 0.922 ms | 58 | 39 |
| largest-moves, W-HOT, fetch = 1,001 | used | 13.375 ms | 5.811 ms | 39 | 39 |
| largest-moves, 1,000,000 rows in | used | 1.117 ms | 0.581 ms | 19 | 19 |
| largest-moves, W-COLD, fetch = 101 | fresh | 50.787 ms | 0.666 ms | not recorded | 36 |
| largest-moves, W-COLD, fetch = 1,001 | used | 14.682 ms | 5.624 ms | not recorded | 36 |
| largest-moves, cursor 2026-06-25 13:30Z | fresh | 0.812 ms | 0.627 ms | not recorded | not recorded |
| `/symbols/AAPL/bars`, page 1 | used | 0.853 ms | 0.366 ms | 1,920 | 852 |
| `/symbols/AAPL/bars`, deepest page | used | 0.466 ms | 0.361 ms | 8 | 8 |
| as first shipped, page 1, fetch = 101 | used | 0.653 ms | 0.252 ms | 58 | 39 |
| as first shipped, page 1, fetch = 1,001 | used | 2,265.724 ms | 840.113 ms | 39 | 39 |
| as first shipped, 1,000,000 rows in | used | 2.091 ms | 0.175 ms | 19 | 19 |
| as first shipped, cursor 2026-06-25 13:30Z | fresh | 162.381 ms | 139.186 ms | not recorded | not recorded |
| largest-moves, W-HOT, `min_move_pct = 1` | fresh | 402.414 ms | 128.316 ms | 693 | 39 |
| largest-moves, W-HOT, `min_move_pct = 100` | used | 1,590.699 ms | 518.112 ms | 39 | 39 |

#### What this subsection hands on

It answers what the `/bars` subsection left for this feature. The hot-window index's verdict is
settled below, on `/analytics/largest-moves` itself. That endpoint's window cap keeps planning
small: a legal window opens at most five children, and five only for a window running from 31
January to 1 May of a non-leap year.

It hands three things to the step that copies the hot window to the deployed database. First, plans
on RDS can differ, so condition (d) has to be re-established there by the relative comparison: the
statement as written against the same statement with the redundant bound deleted. A child count
under 71 is not the test, because a statement missing the bound also passes it on any window ending
before 2026-06. Second, the hot-window index has to be recreated by hand. Third, the session join
stays on the bounds alone.

## Class B — query 2, the one query with a selective filter

Query 2 touches 2 symbols of 100. That is the only query in the set where an index can make it
**read fewer rows**, which is why it is the only one asked for an order of magnitude.

**The "before" is constructed, and is labelled as constructed.** Query 2's mechanism is the PK,
which has existed since Feature 1, so there is no untuned state to measure. Section 4 mandates
`enable_indexscan=off; enable_bitmapscan=off; enable_indexonlyscan=off` in the same session
rather than dropping and rebuilding a primary key over 41.7M rows to arrive at the same number.

`:symbol_a = AAPL`, `:symbol_b = MSFT`, `:start = 2026-04-01`, `:end = 2026-06-30` — the same
window Class A binds.

Five runs each, the same sweep Class A uses, median reported:

| | root blocks | median exec (5 runs) | root node |
| --- | ---: | ---: | --- |
| simulated before | 25,125 | 561.8 ms | `WindowAgg` |
| after | **830** | 80.3 ms | `WindowAgg` |
| **ratio** | **30.27×** | 7.00× | |

**Blocks 30.27× against a ≥10× target — passes**, and two later five-run sweeps read the same
25,125 and 830 to the block while their wall-clock ratios came out at 7.19× and 7.87× (see Class
A above) — which is this section's argument demonstrating itself rather than asserting itself.
Wall-clock moved 7.00× over the same pair, and the gap between those two numbers is the entire
argument for gating on blocks: a 10× target read off the stopwatch **fails** this query at 7.00×
on the same runs where its block ratio passes three times over, and a colder cache would have
moved that verdict again without a single block changing.

This number is only meaningful because of the heap it was measured on. On a day-major heap — a
fixture measurement from the design work, not one taken on this database, since reproducing it
here would mean rewriting 41.7M rows — the same query and the same index measure 1.13–1.42×, and
the gate fails. See
[The heap the numbers rest on](#the-heap-the-numbers-rest-on) — 0 inversions, 0 split runs,
every symbol on ~1% of the pages.

## The other two index decisions

### The hot-window partial index — kept

Created on the four recent **child** partitions directly, never on the parent. The cutoff is
`date_trunc('month', INGEST_END − (HOT_WINDOW_MONTHS − 1) months)` with `HOT_WINDOW_MONTHS = 4`
— the four most recent **whole** months, 2026-03 through 2026-06. The naive reading,
`INGEST_END` − 4 months with no truncation to month start, gives `2026-02-28` and would pull
`bars_2026_02` into the set instead; the RDS copy has to re-derive this cutoff by hand and needs
the whole-month rule, not the naive one, to land on the same four partitions:

```sql
CREATE INDEX bars_2026_0N_hot_idx ON bars_2026_0N (ts, symbol) INCLUDE (open, close)
  WHERE ts >= TIMESTAMPTZ '2026-03-01 00:00:00+00';
```

Measured against the plain `(ts, symbol)` index the children inherit from the parent, on the
access path `/analytics/largest-moves` uses — universe-wide, ordered `(ts, symbol)`,
keyset-paginated, reading only `open` and `close`:

| | root blocks | index-only scans | plain index scans |
| --- | ---: | ---: | ---: |
| inherited `(ts, symbol)` | 1,012 | 0 | 4 |
| with the partial index | **16** | **4** | 0 |

**63.25× fewer blocks, and the planner chose it unprompted.** **128 MB across four partitions**,
built in 8 s — a rounded megabyte figure and not a byte reading, since 128 MB here is 2^27 bytes to
the byte, which four real index sizes do not sum to. Read it as about 32 MB of index per partition.
That table measures the `.sql` form of the access path, taken before the endpoint existed.

**Kept, and the verdict is now read off the endpoint.** On `/analytics/largest-moves` the planner
chose each child's `bars_2026_0N_hot_idx` as an `Index Only Scan` with 0 heap fetches, on page 1 at
both page sizes and at the 1,000,000-deep cursor, all at `min_move_pct = 0`: **9 execution blocks at
the default page and 14 at the cap**, and 8 at the deep cursor. A positive threshold reads further
through the same index — 3,208 blocks for a default page at 1%, and all **12,039** of the window's
for a threshold no bar meets (three `Index Only Scan`s on the hot index, 3,976 + 3,890 + 4,173
blocks, 0 heap fetches; the same three children's heaps hold **24,889 blocks** in total — 8,629 +
8,219 + 8,041, read off the as-first-shipped statement's parallel sequential scans over these same
children (see "Why a page is usually an index descent" above), not measured on this index-only
statement). That is W-HOT's cost.
**On the one window without the hot index that was measured the same threshold reads far more —
126,182 blocks at 1% and 1,737,160 at a threshold no bar meets, on W-COLD** — because on its three
partitions the planner has only the plain
`(ts, symbol)` index and must visit the heap for `open` and `close` (see the threshold paragraph in
"The analytics endpoint forms" above). The same statement a year earlier, at `min_move_pct = 0` on
children that carry no hot-window index, reads each child's `_ts_symbol_idx` with an `Index Scan`
and visits the heap: **121 blocks at the default and 1,024 at the cap** (see "The analytics
endpoint forms" above). The saving is in blocks, not time: warm, the hot-window page's median ran
0.922 ms against 0.666 ms at the default and 5.811 ms against 5.624 ms at the cap. W-COLD is a
different quarter's data as well as a different index set, so the table above stays the index's
like-for-like measurement, and the endpoint agrees with it in direction. No `/bars` page measured
has used the index — the planner chose each child's primary key every time — so this endpoint is
its whole case.

**It holds only while the session join stays on the bounds alone.** With the day equality back in
the join, the page at the cap leaves this index for sequential scans, and a page late in a window
reads the rest of the window through it and sorts. An index on four children is not inherited by
a partition created later, so keeping it means recreating it by hand whenever the window moves,
here as on RDS. No `DROP INDEX` was run; whichever feature drops it says so here.

Two things about the placement are rules rather than preferences, both verified: creating this
on the *parent* propagates the predicate to every child, so every pre-2026 partition gets a
partial index no row can satisfy — real catalog entries, real maintenance, zero rows — and the
propagated copies then **cannot be dropped individually** (`SQLSTATE 2BP01`; a partition index
attached to a partitioned index is a dependent object). The parent mistake is all-or-nothing,
not prune-later. It also has a consequence at `v2`: because the index lives on children,
`CREATE TABLE ... (LIKE bars INCLUDING ALL)` on RDS copies **no** partial index, so the step that
copies the hot window onto RDS recreates it by hand over the same `HOT_WINDOW_MONTHS`.

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
**367.89 ms, 11× slower** (one run each; the blocks are the durable half), because a BRIN range
scan rechecks every tuple in every block its summary admits. Dropped.

That is the documented negative result the design asked for, and it is the right answer: the
partition key already does BRIN's job here, and a 24 kB index that the planner never picks is
worth knowing about rather than carrying.

### What is deliberately not indexed

Nothing on `volume`, and the row math above is why: queries 4 and 8 are the only two that filter
or rank on it, both read 100% of the universe, and each was measured with its own volume index on
disk — `(volume)` for query 4, `(ts, volume)` for query 8 — and read the same blocks with it as
without it. An index that is never chosen is not free — it is maintained on every one of 41.7M
inserts.
