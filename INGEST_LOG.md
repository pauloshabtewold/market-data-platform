# Ingest log

One entry per load, oldest first. Records what was requested and what came back, so a coverage
finding can be traced to the request that produced it rather than re-argued from the data.

## The universe

`tickers.txt` holds 100 large-cap US equities and is an input to the build rather than a product
of it — no run in this repository authors it, because a universe a session invents would make the
seeding gate circular. The selection rule below, its source list and its selection date are
recorded because the survivorship disclosure depends on them.

- **selection rule:** large-cap names from the current S&P 500 constituent list, minus anything
  that listed inside the window or was renamed inside it.
- **source list:** S&P 500 constituents.
- **selection date:** 2026-08-20.

**The rename exclusion rests on recall and cannot be verified here.** The assumption when the rule
was written was that a ticker renamed mid-window would return a sparse series and so would show up
as a coverage finding. Tested on 2026-08-20, that is wrong in the direction that hides it: Alpaca
remaps renamed symbols, and META, ELV and XYZ each return bars from 2020-08-03 — before their
renames, FB→META and ANTM→ELV in 2022-06 and SQ→XYZ in 2025-01. A rename therefore yields a
*complete* series, no completeness check can see it, and the constituent table carries the
predecessor's index date (META shows 2013). The exclusions were made from recall and are recorded
as such rather than described as checked. The risk they guard is semantic — a series whose meaning
changes mid-window — not missing data.

RTX was probed in the same batch and is **not** evidence for that finding. It has traded under
this ticker since the UTX/Raytheon merger of 2020-04-03, four months before the window opens, so
bars from 2020-08-03 are the ordinary in-window case and show nothing about remapping. Four
symbols returning early bars read as four confirmations when three of them were confirmations and
the fourth was a control nobody had marked as one. RTX is nonetheless absent from `tickers.txt`,
and the rule above does not account for that absence: it was not renamed inside the window. The
exclusion stands as recorded, one name wider than the published rule describes.

The two survival filters also mean the universe is survivorship-biased by construction. Coverage
and distribution figures describe a continuously-listed large-cap universe, not the market.

## 2026-08-21 — sample load, 5 tickers × 1 month

The first five lines of `tickers.txt` — AAPL, MSFT, NVDA, AVGO, ORCL — over 2026-06, the most
recent fully-completed month inside 2020-08-01..2026-06-30. A partial month was avoided on
purpose: it makes the coverage check look broken and collides with the vendor's 15-minute `end`
rule.

- **Feed:** `iex`
- **Adjustment:** `split,spin-off`
- **Limit:** 10000, one page per ticker-month at this size
- **Window per unit:** `<M>-01T00:00:00Z` to `<last day>T23:59:59Z`, both bounds inclusive

Result: 41,723 bars over 5 units in 4.8 s and 5 requests, 40,673 of them inside the regular
session. `market_days` was loaded first — 1,484 days, 2020-08-03 to 2026-06-30 — and `symbols`
was seeded to 100 rows from `/v2/assets`, with no inactive constituents and nothing deleted.

That 4.8 s is the published rate, and it is one leg rather than a pair. A second pass over the
same five units took 2.9 s; it fetched rather than skipped because `ingest_progress` was cleared
first, but `bars` was not, so it inserted nothing and paid for no partition DDL, no index
maintenance and no WAL. It is idempotency evidence and is recorded under that heading below. The
two legs are different workloads, and the gap between them is the inserts rather than the network.

### Feed comparison

AAPL over 2026-06, same window and adjustment, both feeds followed to the last page:

| Feed | Bars | Pages | HTTP |
| --- | --- | --- | --- |
| `iex` | 8,547 | 1 | 200 |
| `sip` | 17,904 | 2 | 200 |

Both legs were served, which is what substantiates the claim that SIP was available and IEX is a
choice. The SIP leg needed the pagination loop: it carries the full 04:00–20:00 extended session
and truncates at `limit=10000`, so two plain requests would have recorded 17,904 as 10,000 from a
silently truncated first page. Only the `iex` result was inserted.

These are whole-month counts over different session windows and are **not** a coverage ratio.

### Corporate-action probes

The sample month contains no corporate action, so it cannot show whether the vendor honoured the
adjustment list — the check would pass vacuously. Five extra requests were spent instead, on a
spin-off-only name and a split-only name, and none of the five was inserted. The fifth is MMM's
`split` leg: without it, `raw` differing from `split,spin-off` proves only that *something* in the
list was honoured, not which element.

| Symbol | Minute | `raw` | `split` | `split,spin-off` |
| --- | --- | --- | --- | --- |
| MMM | 2024-03-01 14:30Z | 91.845 | 91.845 | 73.66 |
| NVDA | 2024-05-01 14:30Z | 838.28 | — | 83.83 |

MMM is the load-bearing row. It has no split in that window, so only the Solventum spin-off of
2024-04-01 can move its price — and the `split` leg matching `raw` while `split,spin-off` differs
reads the **second** list element directly. **The vendor honoured both elements.** A split-only
probe could not have shown this: NVDA's 10-for-1 split of 2024-06-10 moves the price under either
expression, so it cannot distinguish "the list was honored" from "only the first element was."

The outgoing query string was logged once for the bars endpoint and carried `feed=iex`,
`adjustment=split%2Cspin-off` and `limit=10000`, so what the vendor received is on the record
rather than inferred from config.

### Idempotency

Proven both ways after the load. Re-running unchanged skipped all five units through
`ingest_progress` and issued zero bars requests. Clearing the progress rows only and re-running
re-fetched all five in **2.9 s** and inserted zero bars, leaving the total at 41,723 — which
exercises the `ON CONFLICT` path rather than only the skip path. That 2.9 s is the timing of this
leg and not of a load: it is faster than the 4.8 s above because it writes nothing, so it is not
extrapolated to anything.

## 2026-08-26 — first half of the universe, 50 tickers × 71 months

The first 50 lines of `tickers.txt` over the whole window, 2020-08-01..2026-06-30. The five tickers
of the sample load above are the first five of these, and their rows were deleted before this run
started: they were loaded before validation existed, so their `rejected_count` was a placeholder
rather than a measurement. They came straight back as part of this load.

- **Feed:** `iex`
- **Adjustment:** `split,spin-off`
- **Limit:** 10000, one page per ticker-month at this size
- **Window per unit:** `<M>-01T00:00:00Z` to `<last day>T23:59:59Z`, both bounds inclusive
- **Units:** 3,550 — 50 tickers × 71 months, every one completed

Result: **22,435,680 bars** across 71 monthly partitions, spanning 2020-08-03 to 2026-06-30, with
**24 bars rejected** by validation and **no failed units**. 22,378,005 rows fall inside the regular
session, so the extended-hours share is **57,675 rows, 0.26%** — small, but real, which is why the
session bounds are a join predicate rather than an assumption. `symbols` stayed at 100 rows and
`first_bar_ts` was recomputed for the 50 that now hold bars.

On-disk size is **2,852 MB**, measured as the sum of `pg_total_relation_size` over the child
partitions. Taken against the parent it reads `0 bytes`, because that function does not descend into
a partitioned table — worth stating here, because that zero looks like an empty database rather than
like the wrong question.

Wall clock was **23.2 minutes** for the 3,550 units, 0.392 s per unit. That supersedes the ≈57
minutes projected for a 50-ticker half: the projection came from timing five units at 4.8 s, and
five units carry the cost of opening the first partitions and the first connection, which then rides
along in every extrapolated unit. The request rate over the measured leg was 157 per minute against
a 200/minute client-side limit, so the throttle never blocked and the wall clock binds.

**This load was interrupted on purpose and finished in two legs.** The run was killed with `kill -9`
after 5 minutes 23 seconds, at which point 750 units had committed, then restarted against the same
window. It skipped exactly those 750 and completed the remaining 2,800 in 1,068.7 s. That is why the
run's own report line reads `units=2800 skipped=750 … rejected=18` — it describes the second leg
only, and the 24 rejections and 23.2 minutes above are the totals across both. Every completed unit
survived the kill because each is committed as its own transaction, and afterwards `ingest_progress`
agreed with the log to the row.

Coverage over the window reads **77.62%** with **zero missing units**, and 50 symbols not yet
ingested — the second half of the universe, which is a later load's work. The percentage is what IEX
carries rather than a defect; the missing-unit count is the one that has to be zero.

### Coverage by period

Recorded here because a single month is not a rate and the README used to publish one that could not
be re-derived. Measured over these 50 symbols against `market_days`, counting only bars inside
`[open_ts, close_ts)` and dividing by `SUM(session_minutes)` for the same days — the same membership
the coverage query uses:

| Period | Pooled | Per-symbol range |
| --- | --- | --- |
| 2026-06 (the sample month) | 99.32% | 96.73–100.00% |
| 2022-06 | 81.01% | 13.25–99.90% |
| 2025-06 | 74.77% | 7.59–99.88% |
| 2020-08..2026-06, whole window | 77.62% | — |

The sample month is near this feed's ceiling and is not representative. Coverage is a property of the
symbol and the period together, which is what queries 9 and 10 exist to break down; the whole-window
figure is superseded once the second fifty are loaded.
