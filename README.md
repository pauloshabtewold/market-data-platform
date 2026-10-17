[![tests](https://github.com/pauloshabtewold/market-data-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/pauloshabtewold/market-data-platform/actions/workflows/ci.yml)

# Market Data Platform

Minute-bar ingestion into a month-partitioned PostgreSQL database, ten tuned analytical queries,
and a keyset-paginated read API. 100 large-cap US equities over 2020-08-01 to 2026-06-30, from
Alpaca's IEX feed. **41,668,537 bars loaded in 42.4 minutes**, measured rather than projected.

## Status

- Live: http://3.238.67.1:8000/docs (until 2026-10-19)
- OpenAPI document: http://3.238.67.1:8000/openapi.json
- Demo: https://youtu.be/2Q49aHoaP7I

The address belongs to the task itself, with no load balancer, so it changes if the task is
replaced and stops answering on the date given. It is a demonstration on a finite credit balance,
not a service: plain HTTP, no auth, no rate limit, acceptable only because every endpoint is a read
over public market data.

**The deployed database holds four months — 2026-03-01 to 2026-06-30, 2,732,236 bars** — copied
from the loaded database rather than re-ingested. That range is 121 days and four of the six routes
cap a window at 90, so no single request spans it; `/symbols/{symbol}/bars` has no cap. One
consequence looks like a contradiction and is deliberate: `/symbols` reports `first_bar_ts` from the
**full** history, 2020-08-03, a date the service will then refuse as a window bound.

The API serves `/health`, three keyset-paginated data endpoints (`/symbols`,
`/symbols/{symbol}/bars`, `/symbols/{symbol}/daily`) and three analytics endpoints
(`/analytics/volatility`, `/analytics/gaps`, `/analytics/largest-moves`).

The document is generated from the routes and served at `/openapi.json`, with an interactive page
at `/docs`. It carries the routes, the schemas and the statuses and nothing past them: `info` is a
title and a version, and none of its 24 parameter entries carries a `description` or an `example`
(one carries a `default`). Swagger UI's Try-it-out fields therefore open blank for the three values
that cannot be guessed — a symbol, which is upper-case, and the two window bounds, which have to
fall inside the served range. Those are described here and in the 422 a wrong one answers rather
than in the document, because the deployed image is pinned by digest and adding them would publish
a document the running service does not serve.

**Every refusal has one shape**, whatever the status: an `error` object with `code`, `message` and
`detail`, all three always present and `detail` nullable rather than optional. `code` is a closed
set of five — `invalid_params`, `invalid_cursor`, `invalid_range`, `unknown_symbol`, `internal` —
and `detail.reason` is what separates two refusals sharing one code — a closed set of thirteen:
`cursor_outside_window`, `invalid_parameter`, `limit_out_of_range`, `not_base64`, `not_json`,
`outside_ingested_range`, `start_after_end`, `unknown_route`, `unknown_symbol`, `unparsable_ts`,
`window_too_long`, `wrong_fields`, `wrong_types`. The published document names `code` and not
these, so branch on `code` and read `reason` for the diagnosis. The one exception sits below
the application: a request malformed at the HTTP level never reaches it, and the server answers
`400` as `text/plain`.

**One wire detail before you hand-write a client.** Prices are unqualified `numeric`, so a
whole-dollar price serialises as a JSON **integer** — `"close": 255`, not `255.0`. Measured over a
full population: **489 of 33,600 values, 1.46%**. The document types these as `number`, which admits
integers, so a generated client copes and `isinstance(x, float)` does not.

## Quickstart

Needs Docker and Python 3.11+. CI tests 3.11, 3.13 and 3.14.

```bash
cp -n .env.example .env                   # runs as copied
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest                # unit and integration, what CI runs
```

The suite needs Docker but no database setup: the integration half starts its own throwaway
Postgres containers and migrates them, so it passes on a machine where `docker compose` was never
run.

**If your Docker socket is not at `/var/run/docker.sock`** — Colima, Podman and Rancher Desktop put
it elsewhere — the integration half fails with ~280 container-startup errors that look like a broken
machine. Set the override to that **literal** value, not to your own socket path: it names the path
bind-mounted into the test containers' reaper, which is the daemon's socket rather than the client
socket your `docker` command uses.

```bash
TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock .venv/bin/python -m pytest
```

Loading real data needs Alpaca credentials and a long paid run, so it is deliberately not part of
getting the suite green:

```bash
docker compose up -d --wait db
.venv/bin/python -m db.migrate                           # the ingest never migrates
.venv/bin/python -m ingest --tickers-file tickers.txt    # ~42 min, 7,100 vendor requests
docker compose up -d --wait db app
.venv/bin/python -m pytest tests/e2e                     # runs only when named
```

`.env.example` runs as copied. Its four measured constants are left empty on purpose and nothing in
the service reads them — they are recorded so a number behind a sizing decision has one published
value instead of a default nobody chose. [Methodology](docs/METHODOLOGY.md) carries each with its
arithmetic.

## What's interesting here

**Ingestion resumes after a hard kill.** Progress is recorded per `(symbol, month)` in the same
transaction as the bars it covers, so a `kill -9` loses no committed work and a re-run skips what
landed. `tests/integration/test_crash_resume.py` kills the process and asserts it.

**A token-bucket limiter holds the request rate under its own cap.** The heavier load averaged
**185 requests a minute** against a 200/minute client-side limit — our cap, not a published vendor
ceiling — over 3,552 requests in 19.2 minutes. The clock is injected, so no test sleeps.

**Missing minutes stay missing.** Nothing is synthesized or forward-filled, so a gap in `bars` is a
gap in the tape as this feed saw it. IEX coverage pools to **72.16%** of regular-session minutes
across the universe, and the thinnest symbol-month reads **3.02%**.

**The ten queries are gated on blocks read, not on a stopwatch.** Blocks are a property of the plan
— the same on a warm cache, a cold one, or RDS — while wall-clock mostly measures what the page
cache held. The one query with a selective filter reads **30.27× fewer blocks** after tuning while
the clock moves only 7×. The six that must read every row are held to evidence of optimality
instead, including two candidate indexes built and shown **not** to be chosen.

**Deep pages are keyset, not `OFFSET`.** A million rows into a 90-day window, a page answers in
**1.136 ms** against `OFFSET`'s **2,175 ms** at the same cursor — **1,915×**. Dropping each series'
first run, which favours `OFFSET`, still leaves 1,866×.

**There is no incremental ingest.** A run re-walks every requested unit and skips what is recorded.
Widening the window and re-running is the supported path.

## Hardest bug

An endpoint that pages by keyset never once served a keyset page, and every test said it was fine.

`GET /analytics/largest-moves` pages the universe on `(ts, symbol)`. As first shipped its statement
joined minute bars to the trading calendar the way the daily rollup does — a day equality plus the
half-open session bounds — because that is where the join was copied from. In the rollup the
equality is right: it gives the planner a hash, which is what a query aggregating a whole window
wants. A keyset page wants the opposite, an ordered walk that stops after one page.

The planner had no statistics for that expression. At page 1 it estimated the join at **1,280 rows
per process** where each of three processes returned **669,829**. Having decided the join was nearly
free, it gave up the ordered nested loop for a **hash join that read every remaining row in the
window and sorted them to return one page**. Page 1 at the cap read **25,035 blocks and 840 ms** —
the same 25,035 the million-row `OFFSET` query read, because both ran the same hashed join. Keyset
pagination was buying nothing.

It survived review because the threshold moved with the cursor's *position* while every acceptance
check pinned one cursor's *depth*. The deep-page test sat on the safe side — page 1 at 4.80 ms, the
millionth-row page at 4.18 ms, a ratio of 0.87. Meanwhile a cursor five days from the window's end
answered in **139 ms, 29× page 1**, on the default limit.

The fix was to **delete** a join condition. Joining on the half-open session pair alone removes the
only hashable predicate, so the ordered index-only walk is the only plan left. Page 1 at the cap now
reads **14 blocks**. Deleting a predicate to make a query 1,788× cheaper is the wrong shape for an
optimisation, which is why it took measurement rather than reading to find. It is safe because the
equality is implied by the bounds: all **1,484** sessions open and close inside their own New York
date, checked rather than assumed.

Two things are worth more than the fix. A guard that reads a SQL constant *by name* does not pin the
statement a request actually sends — pointing the route at a second constant carrying the old join
left the whole suite green, so the tests now record what each handler sends on the connection it
used. And this is not fully closed: on a window whose final session also ends a partition carrying
no covering index, a cursor in that closing slice still leaves the ordered plan.
[Query performance](docs/QUERY_PERFORMANCE.md) documents where, rather than claiming otherwise.

## Layout

```
ingest/     feed client, retry/throttle, calendar, symbol resolution, validation, pipeline
db/         schema, migrations, session
api/        app factory, connection pool, keyset cursors, the one error shape, endpoints
tests/      unit and integration suites, run in CI; e2e/, run by name against the app service
```

[Query performance](docs/QUERY_PERFORMANCE.md) has the before/after plans for all ten queries, the
index decisions including two negative results, and the conditions every number depends on.
[Methodology](docs/METHODOLOGY.md) covers sizing, partitioning, provenance and survivorship.
[INGEST_LOG.md](INGEST_LOG.md) is the run-by-run record. [DEPLOY.md](DEPLOY.md) is the deployment:
every resource by name, the billing bound, the teardown date and the checklist that removes it all.

Licensed under the [MIT License](LICENSE).

## Contributing

Conventional Commits with six types — `feat`, `fix`, `test`, `refactor`, `chore`, `docs` — as
`type(scope): imperative summary`. Trunk-based: commits land on `main`, no feature branches. Tests
ship with the code they cover; comments say why, not what. Local hooks enforce this and are not part
of the repository, so the convention is published here to be followed without them.
