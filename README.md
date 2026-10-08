[![tests](https://github.com/pauloshabtewold/market-data-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/pauloshabtewold/market-data-platform/actions/workflows/ci.yml)

# Market Data Platform

Minute-bar ingestion into a month-partitioned PostgreSQL database. 100 large-cap US equities
over 2020-08-01 to 2026-06-30, from Alpaca's IEX feed.

## Status

- Live: http://100.58.98.13:8000/docs (until 2026-10-19)
- OpenAPI document: http://100.58.98.13:8000/openapi.json

The address above is the task's own, with no load balancer in front of it, so it changes
if the task is replaced and it stops answering on the date given — the deployment is a
demonstration on a finite credit balance, not a service. It is plain HTTP with no
authentication and no rate limit, which is acceptable only because every endpoint is a
read over public market data. There is no route at `/`, so the interactive page is the first
link above — and that page is a 1,019-byte shell that loads Swagger UI from a third-party CDN,
so the second link is the one that answers with no CDN in the path.

**The deployed database holds four months, 2026-03-01 to 2026-06-30, not the full span
below.** It is the hot window: 2,732,236 bars over four monthly partitions, copied from
the loaded database rather than re-ingested. A request outside those four months is
refused with a 422 naming the bounds it does serve, so the served range is discoverable
from the service itself. That range is **121 days, wider than the 90-day cap below**, so no
single request to `/symbols/{symbol}/daily` or to an analytics endpoint can span it — ask those
for at most 90 days at a time, and a longer window is refused with its own 422 naming the cap.
`/symbols/{symbol}/bars` has no cap and spans the whole four months in one request. One
consequence is worth stating because it looks like a contradiction: `/symbols` reports each
symbol's `first_bar_ts` as its first bar in the **full** history, 2020-08-03, because that column
is copied as it stands to keep the deployed answers identical to the local ones — so the service
names a date it will then refuse as a window bound.

The ingest pipeline, the database layer, the read endpoints and the analytics endpoints are
built and tested. The API serves `/health` plus three keyset-paginated data endpoints:
`GET /symbols`, `GET /symbols/{symbol}/bars` and `GET /symbols/{symbol}/daily`. It also serves
three analytics endpoints: `GET /analytics/volatility` (realized volatility by half-hour
bucket, minutes since that day's own open, for one symbol), `GET /analytics/gaps` (the
overnight gap distribution, prior close to next open, for one symbol as a single aggregate
row) and `GET /analytics/largest-moves` (every regular-session minute bar in the window whose
absolute percentage move is at least `min_move_pct`, across the full universe,
keyset-paginated on `(ts, symbol)` — excluding a bar whose open or close is a value the
percentage has no answer for, which `docs/QUERY_PERFORMANCE.md` states exactly and no row in
the loaded data is). **Four endpoints cap the window at 90 days**, refusing a longer one with a 422
that names the bound and the value which failed it: those three and `GET /symbols/{symbol}/daily`,
which aggregates and carries the same bound. `GET /symbols/{symbol}/bars` takes a window and does
not cap it, deliberately: the per-page cost published for `/bars` is measured over the whole
history, which a 90-day cap would forbid. The cap is a difference and not an inclusive count, so
2026-04-01 to 2026-06-30 is 90 days and is served while 2026-03-31 to 2026-06-30 is 91 and is not.
Of the three analytics endpoints the first two take no `limit` or `cursor` and answer with
`next_cursor` always null. The OpenAPI document is generated from the routes and served at
`/openapi.json`, with an interactive page at `/docs`. That page brings one more path with it,
`/docs/oauth2-redirect`, which the framework mounts beside it and nothing here calls — so the
paths named above are the ones this project declares, not every path that answers.

**Every refusal has one shape**, whatever the status: an `error` object carrying `code`, `message`
and `detail`. All three keys are present on every error body, `detail` being nullable rather than
optional, so a generated client can read it without a presence check. `code` is a closed vocabulary
of five — `invalid_params`, `invalid_cursor`, `invalid_range`, `unknown_symbol` and `internal` — and
the published document names those five rather than a bare string. Each route documents the statuses
its own handler answers — across the six data and analytics routes, 400 and 500 everywhere, 404 on
the four that take a symbol, 422 on the five that take a window: `/symbols` takes no window and so
cannot answer a 422, and documents a catch-all entry in place of one. `/health` takes no parameter
and documents 500 alone. A wrong method answers 405 with code `invalid_params` in the same shape,
except for `HEAD`, which carries the status and no body because HTTP forbids one; no route documents
the 405, and `/symbols`'s catch-all entry is the only place the document covers it. A
500 is the service refusing to complete the request, not the client's fault, and the bounds in
[Query performance](docs/QUERY_PERFORMANCE.md) say when one is a timeout.

**One wire detail worth knowing before you hand-write a client.** Prices are stored as unqualified
`numeric`, so the scale is whatever the insert produced, and a whole-dollar price serialises as a
JSON **integer** — `"close": 255`, not `255.0`. It is rare and it does occur: 46 of 7,191 price
values across a sample of real pages, 0.64%. The published document types these fields as `number`,
which in JSON Schema admits integers, so a generated client handles both; code that tests
`isinstance(x, float)` or its equivalent does not.

Loaded: **41,668,537 bars** across the full universe, in **42.4 minutes**. That is measured
on the loaded data, not projected onto it.

## Quickstart

Needs Docker, and Python 3.11 or later; CI tests 3.11, 3.13 and 3.14, so 3.12 is permitted and
unexercised. The database publishes 5432 and the end-to-end service publishes 8000, so both ports
must be free.

```bash
cp -n .env.example .env                   # runs as copied; see below before editing it
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker compose up -d --wait db
.venv/bin/python -m db.migrate            # separate operator action; ingest never migrates
.venv/bin/python -m pytest                # unit and integration, what CI runs
```

The suite needs Docker but neither of the two steps above it. The integration half starts its own
throwaway Postgres containers and migrates them itself, connecting to those rather than to
`DATABASE_URL`, so `pytest` passes on a machine where `docker compose up` was never run and
`db.migrate` failed against an unreachable address. Those two steps are here for the real
database the ingest and the end-to-end suite work against.

**If your Docker socket is not at `/var/run/docker.sock`** — Colima, Podman and Rancher Desktop all
put it elsewhere — the integration half cannot start its containers, and the failure looks like a
broken machine rather than a missing setting: roughly 280 errors, each one a Docker 500 about
creating a directory at a socket path. Set the override below to that literal value and not to
your own socket path: it names the path the test containers bind-mount into their reaper container,
which is the **daemon's** socket inside the Docker machine rather than the client socket your
`docker` command talks to. On a Colima or Rancher Desktop host those two are different paths, and
the client's is the one that fails.

```bash
TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock .venv/bin/python -m pytest
```

`.env.example` runs as copied: every key the code requires at import carries a working
placeholder, and the database URL it names is the one `docker compose` publishes. The four
measured constants are left empty on purpose, and **nothing in the service reads them** —
they are recorded so that a number behind a sizing or partitioning decision has one published
value instead of a default nobody chose. [Methodology](docs/METHODOLOGY.md) carries each value
and the arithmetic behind it.

Two more steps load real data and exercise the service end to end, and the test suite needs
neither. Loading bars needs real Alpaca credentials in `.env` and costs a long paid run — the full
universe is 7,100 requests over about 42 minutes — so it is deliberately not part of getting the
suite green:

```bash
.venv/bin/python -m ingest --tickers-file tickers.txt    # ~42 min, 7,100 vendor requests
docker compose up -d --wait db app
.venv/bin/python -m pytest tests/e2e                     # end-to-end; runs only when named
```

The end-to-end suite needs the `app` service running against the loaded database. The `app`
service loads the code once, at startup, so after changing code run `docker compose restart app`
before running the end-to-end suite again. That service installs the project with `pip install -e .`
on every start, so its dependency versions are whatever resolve at that moment, while the deployed
image installs from `requirements.lock` and is pinned: local and deployed behaviour can differ on a
dependency, and the local side is the one that moves.

## What's interesting here

**Ingestion resumes after a hard kill.** Progress is recorded per `(symbol, month)` unit in
the same transaction as the bars it covers, so a `kill -9` mid-run loses no committed work and
re-running skips what already landed. `tests/integration/test_crash_resume.py` kills the
process and asserts it.

**A token-bucket limiter holds the request rate under its own cap.** The heavier of the two loads
averaged 185 requests a minute against a 200/minute client-side limit — the cap is ours, not a
published vendor ceiling. That is a mean over the whole run, 3,552 requests in 19.2 minutes, and
not an observed instantaneous peak: nothing recorded a per-minute series, so the closest the
limiter came to its own cap is unmeasured. The clock is injected, so the tests don't sleep.

**Missing minutes stay missing.** A gap is never synthesized or forward-filled, so gaps in
`bars` are gaps in the tape as this feed saw it. That matters more than it sounds: IEX
coverage pools to **72.16%** of regular-session minutes across the full universe, and
individual symbol-months run as low as **3.02%**. A well-covered month reads **85.04%** over the
universe and 99.32% over the five sample tickers — an upper bound, not a rate.

**The ten analytical queries are gated on blocks read, not on a stopwatch.** Blocks touched are
a property of the plan — the same number on a warm cache, a cold one, or RDS — while wall-clock
is mostly a measure of what the page cache happened to hold. The one query with a selective
filter reads **30.27× fewer blocks** after tuning while the clock moves only 7×, and the six
that must read every row are held to evidence of optimality instead: the parallel plan the
planner should pick and did, a candidate index built and shown not to be chosen, the row math,
and — for three of the six — a byte ratio used as a prediction that had to survive being
tested.

**Deep pages are keyset, not `OFFSET`:** at `min_move_pct = 0`, 1,000,000 rows into a 90-day
window, a `/analytics/largest-moves` page answers over HTTP in 3.74 ms at the median against page
1's 3.33 ms. Reaching that row with `OFFSET` costs 2,175 ms at the median over SQL against the
keyset page's 1.136 ms at the same cursor, a published ratio of 1,915 times. The two series are
not symmetric, and the asymmetry has a cause: the `OFFSET` statement was timed from its first
execution, so its median carries a cold one, 5,224 ms against a 2,175 ms median, while the keyset
runs followed seven executions at the same cursor and so carry none. The keyset series has a
first-run outlier of its own, 3.095 ms against a 1.136 ms median — proportionally the larger of the
two, though far smaller in absolute terms. Dropping each series' first run leaves 1,866 times.

**There is no incremental ingest.** A run re-walks every requested unit and skips what is
already recorded. Widening the window and re-running is the supported path.

## Hardest bug

An endpoint that pages by keyset never once served a keyset page, and every test said it was fine.

`GET /analytics/largest-moves` pages the whole universe on `(ts, symbol)`. As first shipped, its
statement joined the minute bars to the trading calendar the way the daily rollup does — a day
equality plus the half-open session bounds — because that is where the join was copied from. In the
rollup the equality is right, and its own comment says why: it is what gives the planner a hash,
which is what a query aggregating a whole window wants. A keyset page wants the opposite. It needs
an ordered walk that stops after one page.

The planner had no statistics for that expression. At page 1 and a 1,001-row fetch it put the
join at **1,280 rows per process** where the three processes returned **669,829** each on average.
Having decided the join was nearly free, it concluded that returning a hundred rows meant reading
most of the window anyway — so it gave up the ordered nested loop for a **hash join that read
every remaining row in the window and sorted them to return one page**. Page 1 at the cap read
**25,035 blocks and 840 ms**, which is the same 25,035 the million-row `OFFSET` query read at
that point — not a coincidence, since both were running the same hashed join over the same three
children. Keyset pagination was buying nothing. With the join fixed the `OFFSET` form reads
5,996 blocks, so the gap it exists to open is against that number, not against 25,035.

What made it survive review is that the threshold moved with the cursor's *position*, and every
acceptance check pinned one cursor's *depth*. The deep-page test, which exists precisely to catch
a page that degrades with distance, sat on the safe side: page 1 at **4.80 ms**, the millionth-row
page at **4.18 ms** — both medians of a single measurement pass — a ratio of 0.87, passing
comfortably. Meanwhile a cursor five days from the window's end answered in **139 ms, 29× page 1**,
on the default limit — a plan that probed 136,357 rows against a hash and sorted the 136,038 it
kept, to return 101.

The fix was to **delete** a join condition. Joining on the half-open session pair alone removes
the planner's only hashable predicate, so neither a hash nor a merge join remains available and
the ordered index-only walk is the only plan left. Deleting a predicate so that page 1 at the cap
reads **14 blocks** where it read **25,035** is the wrong shape for an optimisation, which is why it
took measurement rather than reading to find. It is safe because the equality is implied by the
bounds: all **1,484** sessions open and close inside their own New York date, checked rather than
assumed. The 139 ms page fell the same way, **822 → 9 blocks** and **3.02 milliseconds** over HTTP.

Two things are worth more than the fix. The first is that a guard which reads a SQL constant by
name does not pin the statement a request actually sends — pointing the route at a second constant
carrying the old join left the entire suite green, so the test now records what each handler sends
on the connection it used. The second is that this is not fully closed, and saying so is the
honest ending: on a window whose final session also ends a partition that carries no covering
index, a cursor in that closing slice still leaves the ordered plan for a bitmap scan and a sort.
A different estimate causes it, the join change did not touch it, and
[Query performance](docs/QUERY_PERFORMANCE.md) documents where it happens rather than claiming it
does not.

## Layout

```
ingest/     feed client, retry/throttle, calendar, symbol resolution, validation, pipeline
db/         schema, migrations, session
api/        app factory, connection pool, keyset cursors, the one error shape, endpoints
tests/      unit and integration suites, run in CI; e2e/, run by name against the app service
```

[Query performance](docs/QUERY_PERFORMANCE.md) has the before/after plans for all ten queries,
the index decisions including two negative results, and the measurement conditions every number
depends on. [Methodology](docs/METHODOLOGY.md) covers sizing, partitioning, provenance and
survivorship, including two earlier projections kept rather than deleted so the size of each
error stays visible. [INGEST_LOG.md](INGEST_LOG.md) is the run-by-run record.
[DEPLOY.md](DEPLOY.md) is the deployment: every resource by name, the billing bound and its
thresholds, the teardown date, and the checklist that removes all of it.

Licensed under the [MIT License](LICENSE).

## Contributing

Commits follow Conventional Commits with six types — `feat`, `fix`, `test`, `refactor`, `chore`,
`docs` — as `type(scope): imperative summary`. Development is trunk-based: commits land on `main`
and there are no feature branches. Tests ship in the same commit as the code they cover, and
comments say why rather than what. These are enforced locally by hooks that are not part of the
repository, so a clone will not reject a commit that breaks them; the convention is published here
so it can be followed without them.
