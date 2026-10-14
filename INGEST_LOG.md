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

## 2026-08-21 — sample load, 5 tickers × 1 month

The first five lines of `tickers.txt` — AAPL, MSFT, NVDA, AVGO, ORCL — over 2026-06, the most

- **Feed:** `iex`
- **Adjustment:** `split,spin-off`
- **Limit:** 10000, one page per ticker-month at this size
- **Window per unit:** `<M>-01T00:00:00Z` to `<last day>T23:59:59Z`, both bounds inclusive

Result: 41,723 bars over 5 units in 4.8 s and 5 requests, 40,673 of them inside the regular
session. `market_days` was loaded first — 1,484 days, 2020-08-03 to 2026-06-30 — and `symbols`
was seeded to 100 rows from `/v2/assets`, with no inactive constituents and nothing deleted.

### Feed comparison

AAPL over 2026-06, same window and adjustment, both feeds followed to the last page:

| Feed | Bars | Pages | HTTP |
| --- | --- | --- | --- |
| `iex` | 8,547 | 1 | 200 |
| `sip` | 17,904 | 2 | 200 |

Both legs were served, so SIP was available and `iex` is a choice rather than a limit. The SIP
leg needed the pagination loop — without it 17,904 bars would have recorded as 10,000. Only
`iex` was inserted.

### Corporate-action probes

Five requests, the fifth being MMM's `split` leg:
| Symbol | Minute | `raw` | `split` | `split,spin-off` |
| --- | --- | --- | --- | --- |
| MMM | 2024-03-01 14:30Z | 91.845 | 91.845 | 73.66 |
| NVDA | 2024-05-01 14:30Z | 838.28 | — | 83.83 |

MMM is the load-bearing row. It has no split in that window, so only the Solventum spin-off of
2024-04-01 can move its price — and the `split` leg matching `raw` while `split,spin-off` differs
reads the **second** list element directly. **The vendor honoured both elements.** A split-only
probe could not have shown this: NVDA's 10-for-1 split of 2024-06-10 moves the price under either
expression, so it cannot distinguish "the list was honored" from "only the first element was."

### Idempotency

Proven both ways after the load. Re-running unchanged skipped all five units through
`ingest_progress` and issued zero bars requests. Clearing the progress rows only and re-running
re-fetched all five in **2.9 s** and inserted zero bars, leaving the total at 41,723 — which

## 2026-08-26 — first half of the universe, 50 tickers × 71 months

The five sample tickers are the first five of these. Their rows were deleted first — loaded
before validation existed — and came back as part of this load.

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

**23.2 minutes**, 0.392 s per unit, 157 requests/min against the 200/min limit — so the
throttle never blocked.
**This load was interrupted on purpose and finished in two legs.** The run was killed with `kill -9`
after 5 minutes 23 seconds, at which point 750 units had committed, then restarted against the same
window. It skipped exactly those 750 and completed the remaining 2,800 in 1,068.7 s. That is why the
run's own report line reads `units=2800 skipped=750 … rejected=18` — it describes the second leg
only, and the 24 rejections and 23.2 minutes above are the totals across both. Every completed unit
survived the kill because each is committed as its own transaction, and afterwards `ingest_progress`
agreed with the log to the row.

Over the same month the 50 symbols loaded here pool to **92.37%** against the five sample
tickers' 99.32% — the sample is not a sample of the fifty. The hundred-symbol figures are in
the 2026-08-27 entry below and supersede these.

## 2026-08-27 — second half of the universe, 50 tickers × 71 months

- **Feed:** `iex`
- **Adjustment:** `split,spin-off`
- **Limit:** 10000, still one page per ticker-month — the largest unit in the whole universe holds
  9,760 rows and no unit reached the limit
- **Window per unit:** `<M>-01T00:00:00Z` to `<last day>T23:59:59Z`, both bounds inclusive
- **Units:** 3,550 — 50 tickers × 71 months, every one completed, none skipped
- **Requests:** 3,550 bars requests plus one calendar and one assets, at **184.9 per minute**
  against a 200/min bucket — the closest this pipeline has run to its own rate limit

Result: **19,232,857 bars**, **2 rejected**, **no failed units**, in **1,152.9 s (19.2 minutes)** —
**0.325 s per unit**, against the first half's 0.392 s, which carried a deliberate kill and restart.

### The universe, both halves together

- **41,668,537 bars** over **7,100 units**, spanning 2020-08-03 12:05 to 2026-06-30 20:54 UTC
- **71 monthly partitions**, unchanged — the second half adds symbols, not months
- **26 bars rejected** in total by validation, 24 in the first half and 2 in this one
- **41,605,369 rows** inside the regular session, so the extended-hours share is **63,168 rows,
  0.15%** — lower than the first half's 0.26%, because the second fifty trade less outside the
  session rather than because anything changed in the pipeline
- **100 symbols**, all with `first_bar_ts` set; `bars` reconciles against
  `sum(ingest_progress.row_count)` at 41,668,537 on both sides
On-disk size 2026-08-27: **5,291 MB** (5,547,909,120 bytes), summed over the child
partitions. `docs/METHODOLOGY.md` carries the index reconciliation and the standing total.
- **Wall clock for the whole universe is 2,544.6 s, 42.4 minutes**, across the two runs

### Coverage by period, all 100 symbols

| Period | Pooled | Per-symbol range |
| --- | --- | --- |
| 2026-06 | 85.04% | 43.94–100.00% |
| 2022-06 | 77.04% | 13.25–99.90% |
| 2025-06 | 67.49% | 7.59–99.88% |
| 2020-08..2026-06, whole window | 72.16% | — |

`missing_units 0`, `coverage_pct 72.16`, `uningested_symbols 0`.

Every range here is a per-symbol floor inside one month; the lowest single symbol-month is
in `docs/METHODOLOGY.md`.

Two deviations, recorded rather than smoothed over. The run was launched by the session
rather than the operator, on the owner's instruction. And it was narrowed with `--symbol`, so
`skipped=0`; the skip was demonstrated straight after against the complete universe —
`units=0 skipped=7100 bars_requests=0`.

## 2026-10-06 — hot-window copy into the deployed database, not a feed load

This entry records a copy rather than an ingest: no vendor request of any kind was spent.

- **Source:** the loaded universe in this repository's development database
- **Copied first, in full, before any bar:** `symbols` (**100** rows) and `market_days`
  (**1,484** rows)
- **Partitions:** `bars_2026_03`, `bars_2026_04`, `bars_2026_05`, `bars_2026_06` — the hot window,
  read from the application's own cutoff of `2026-03-01` rather than chosen
- **Rows:** **2,732,236** bars, as four addends — 715,229 + 666,146 + 651,616 + 699,245 — each
  equal to its source partition's own count, and **6.56%** of the 41,668,537 the two loads above
  produced
- **Requests:** none, of any kind
- **Finished:** 2026-10-06T06:15:12Z. Elapsed time was not recorded, so no rate is published for
  this entry

side of it — the partition bounds, the byte sizing, the indexes the copy does **not** inherit, and
why four months is a deliberate scope rather than a storage limit.
