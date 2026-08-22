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
remaps renamed symbols, and META, RTX, ELV and XYZ each return bars from 2020-08-03 — before their
renames. A rename therefore yields a *complete* series, no completeness check can see it, and the
constituent table carries the predecessor's index date (RTX shows 1957, META shows 2013). The
exclusions were made from recall and are recorded as such rather than described as checked. The
risk they guard is semantic — a series whose meaning changes mid-window — not missing data.

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

A second pass over the same five units took 2.9 s. It fetched rather than skipped because
`ingest_progress` was cleared first; the identical command left unchanged issues no bars requests
at all. The published rate is that pair, and the spread between them is network latency rather
than anything in the pipeline.

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
re-fetched all five and inserted zero bars, leaving the total at 41,723 — which exercises the
`ON CONFLICT` path rather than only the skip path.
