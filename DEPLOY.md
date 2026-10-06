# Deployment

This service runs on AWS Fargate against an RDS Postgres instance holding a four-month hot window
of the ingested history. It is a time-boxed demonstration, not a production deployment: it is funded
from account signup credits, it has no load balancer and no redundancy, and it is torn down on the
date below.

Account created: 2026-10-05
Teardown date: 2026-10-19
Region: us-east-1
Billing alarm thresholds: $5 and $20
Alarm created: 2026-10-06T01:16:12.680000+00:00

The opening date above is the local calendar date. AWS's own `AccountCreatedDate` for this account
reads `2026-10-06T00:49:24+00:00`, which is the same instant one day later in UTC. The earlier of
the two is the one recorded, because the credit window runs from the opening and an opening date
typed later than the true one lets the credits lapse before a teardown this document calls valid.
Teardown is 14 days after opening, well inside the window on either reading.

## The credit model, and why there is a teardown date

A new AWS account carries up to $200 of signup credits valid for up to six months, and there is no
longer a perpetual free tier to fall back on. This deployment costs roughly $25–30 a month while it
runs, so it burns about 15% of the balance every month, and credits reaching their expiry closes the
account rather than warning about it — which is why the teardown date is chosen before any billable
resource exists and is recorded here rather than remembered.

## The bound

Two independent bounds, because each one fails silently on its own.

Two CloudWatch alarms on `AWS/Billing` / `EstimatedCharges`, thresholds $5 and $20, with the
`Currency=USD` dimension and `us-east-1` as the region — that metric is published in no other
region, and an alarm missing the dimension or created elsewhere sits at `INSUFFICIENT_DATA`
indefinitely, which reads the same as healthy. The metric requires "Receive Billing Alerts" to be
enabled in Billing preferences, which is a console setting available only to the account root.
That setting has no read API, but its consequence does: before the first charge landed,
`aws cloudwatch list-metrics --namespace AWS/Billing --metric-name EstimatedCharges --region
us-east-1` already returned a metric carrying exactly the `Currency=USD` dimension these alarms
watch. So both alarms reading `INSUFFICIENT_DATA` at creation means only that no datapoint has
arrived yet, which is a different state from the silent failure described above.

Two AWS Budgets at the same thresholds, each with an email subscriber, and each with
`CostTypes.IncludeCredit` set to `false`. A budget does not depend on the Billing preferences
setting, which is the reason for having both. The `IncludeCredit` flag is not optional: with credits
applied, a default budget measures net spend, reads $0 for the whole credit period, and never fires
while every resource bills against the balance.

One thing is not yet known and belongs here when it is: whether the `EstimatedCharges` metric is
reported gross or net of credits. A net-of-credits alarm is a bound on nothing for as long as the
balance lasts, and the budgets above are the bound that does not depend on the answer. Record it
here once the first charge lands; this is the only place the distinction gets written down.

## Names

Every identifier below is derived from one stem. None is a secret. The account id is deliberately
absent from this file; export it before running any command that takes `--account-id`.

```
PROJECT=market-data
REGION=us-east-1
ECR_REPO=$PROJECT
TAG=0.1.0
CLUSTER=$PROJECT
TASK_FAMILY=$PROJECT-api
SERVICE=$PROJECT-api
SG_SERVICE=$PROJECT-svc-sg
SG_RDS=$PROJECT-rds-sg
RDS_ID=$PROJECT-db
PG_GROUP=$PROJECT-pg16
SSM_DSN=/$PROJECT/DATABASE_URL
LOG_GROUP=/ecs/$PROJECT-api
SNS_TOPIC=$PROJECT-billing
ALARM_5=$PROJECT-billing-5
ALARM_20=$PROJECT-billing-20
BUDGET_5=$PROJECT-5
BUDGET_20=$PROJECT-20
EXEC_ROLE=$PROJECT-task-execution
PORT=8000

ACCOUNT=                  # not recorded here; supply it before the teardown's budget commands
TOPIC=                    # the SNS topic ARN, from `aws sns create-topic`
SVC_SG=                   # filled in under Resources
RDS_SG=                   #   "
```

## The image

```
ECR image: $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/market-data:0.1.0
ECR digest: sha256:a459650b037efc1cd90419f266348f199e9e9afdb939543265dc4df88294ac0e
ECR repository created: 2026-10-06T01:41:32Z
```

The registry address carries the account id, so it is written above as the same `$ACCOUNT`
placeholder the Names block uses; export it and the address resolves. The digest is the identity
that matters, and the repository's tag policy is `IMMUTABLE`, so the tag `0.1.0` can never be moved
to different content — which is what lets a later step check that the deployed task runs this exact
image by digest rather than trusting a tag.

The image is `linux/arm64`, built on an Apple Silicon machine. Fargate's task-definition default is
`X86_64`, so the task definition must set `runtimePlatform.cpuArchitecture` to `ARM64` or the task
fails to pull with a manifest-platform error — and that failure would land after the database is
already running and charging. Fargate supports ARM64 and it is the cheaper of the two.

The image was built once, before the registry existed, and pushed unchanged. It is not rebuilt: a
rebuild resolves the base image and the dependency stack afresh, and pinning both is the whole
reason this file records a digest. The immutable tag makes that one-way in any case.

The repository is the first resource in this deployment that costs anything, which is why its
creation time is recorded at all. Compared against the `Alarm created:` line near the top of this
file, it lands 25 minutes and 20 seconds later: the bound existed before the first thing that
bills, which is a different and stronger statement than the bound existing.

Read both in UTC before comparing them. `aws ecr describe-repositories` returns `createdAt` in the
caller's local offset while `aws cloudwatch describe-alarms` returns UTC, so comparing the two
strings as printed reports the order backwards.

## Resources

```
Cluster: market-data
Task definition: market-data-api:1
Service: market-data-api
RDS instance: market-data-db
RDS endpoint: market-data-db.cudeym8881ns.us-east-1.rds.amazonaws.com:5432
Parameter group: market-data-pg16
Log group: /ecs/market-data-api
SSM parameter: /market-data/DATABASE_URL
Service SG: sg-0836f2ca88ccf54ee
RDS SG: sg-0e7293540c59bf35c
Public address: http://100.58.98.13:8000
RDS /32 opened: 2026-10-06T03:29:42Z
RDS /32 closed: 2026-10-06T03:30:47Z
RDS /32 opened: 2026-10-06T06:11:04Z
RDS /32 closed: 2026-10-06T06:18:28Z
```

The SSM parameter above is a **name**, not a value. The connection string it holds is a
`SecureString` and is written nowhere in this file. Read it with
`aws ssm get-parameter --name "$SSM_DSN" --with-decryption` when a step needs it, and do not
paste the result anywhere.

### The engine, and the three planner inputs that are pinned

`--engine-version` is **16.15**, discovered rather than demanded: it is the latest 16.x this
account is offered and it equals the development database's own `server_version`, so the
comparison behind every one of this project's published block counts is like-for-like down to the
minor. Pinning it mattered because the account's default is **18.3** — two majors on, a different
planner, and a confound that sits above window size in the attribution order with no way to
subtract it.

Be careful reading the version list: the available 16.x versions sort **lexically** to 16.9 and
**numerically** to 16.15, so a `sort | tail -1` picks the wrong minor.

The parameter group `market-data-pg16` (family `postgres16`) pins three values to the development
database's, each one read back from the server rather than from the API:

| setting | pinned | read from `pg_settings` |
| --- | --- | --- |
| `work_mem` | 4096 kB | 4096 |
| `effective_cache_size` | 524288 × 8 kB = 4 GB | 524288 |
| `max_parallel_workers_per_gather` | 2 | 2 |

All three are `dynamic`, so `ApplyMethod=immediate` needed no reboot and
`ParameterApplyStatus` reads `in-sync`. **`pending-reboot` would mean the values are set and
not active, and any figure taken in that state is measured on the defaults instead.**

**Two differences cannot be pinned, and both are recorded rather than worked around.**
`shared_buffers` has `postmaster` context, so it is fixed at instance start: it reads **23081
pages (≈180 MB)** here against **16384 pages (128 MB)** on the development database. That one is
benign for block counts — `shared_buffers` moves the hit/read split and not the sum — which is
why the pinned three are the ones that enter the cost model. The second is total instance RAM:
`db.t4g.micro` is 1 GiB against the development host's 8 GiB. `TimeZone` is `UTC` on both.

### Why the database is publicly accessible

`PubliclyAccessible` is **true**, deliberately. Two steps run `psql` from a laptop, and with it
off the instance has no public DNS or IP to resolve at all — so a temporary firewall rule does not
help and the remedy becomes a bastion host or a VPN, which are more billable resources on a finite
balance. It also cannot be flipped later without a modify-and-reboot.

**The safety property is the security group, not privacy.** The database's group allows 5432 from
the service's group and from nothing else; it holds exactly one rule. A laptop `/32` is added for
one narrow window, used, and removed, and both timestamps are recorded above. A publicly
addressable database with an open-to-the-world 5432 rule is the actual mistake, and it is a
different mistake from this one.

The service's group allows 8000 from `0.0.0.0/0` and carries **no rule for any single
address** — nothing ever connects to the task directly.

### One address is allocated that no step asked for

A publicly accessible database is given a **service-managed elastic address** — here
`54.208.233.222`, which is what the endpoint name resolves to, on an interface owned by the
database service rather than by this account's user. So a check that expects **zero** elastic
addresses cannot pass while the database is reachable from a laptop, and the two decisions are in
tension by construction. The check that means what was intended is *zero addresses this project
allocated*: filter to the ones with no managing service, which reads **0**, and record the
managed one at **1**.

It matters at teardown. The managed address is released when the instance is deleted, so
**after** the database goes, the total must fall back to zero — and an address left allocated and
unattached bills by the hour indefinitely. Item 3's checklist carries that check.

### What is deliberately absent

No load balancer, no NAT gateway, and no task-level health check. The first two are ruled out on
cost. The health check is omitted because without a load balancer the public address belongs to
the task's own network interface and **changes every time the task is replaced** — so a
health-check-driven replacement would quietly invalidate any address written down. The task's
address is auto-assigned and is not an elastic address, which is why it survives nothing.

`assignPublicIp` is `ENABLED`. That single setting is what lets a task in a public subnet pull
its image with no NAT gateway; without it the task dies before the application runs. The subnets
are public by route, not merely by flag — the route table carries a default route to an internet
gateway, which is the thing that actually has to be true.

The image is pinned **by digest**, not by tag, so the running task is tied to one build rather
than to a name that could be moved.

## The hot window

```
Copy finished: 2026-10-06T06:15:12Z
```

The four months copied, which are **read** from the hot-window cutoff and never chosen:

```
- bars_2026_03
- bars_2026_04
- bars_2026_05
- bars_2026_06
```

The cutoff is `2026-03-01`. The naive reading — the end of the ingest window minus four months,
with no truncation to a month start — gives `2026-02-28` and pulls `bars_2026_02` in instead, so
the month list comes from the same function the application uses rather than from arithmetic done
by hand.

Row counts, as four addends rather than one total, each equal to the development database's:

| partition | rows | bounds |
| --- | --- | --- |
| `bars_2026_03` | 715,229 | `['2026-03-01', '2026-04-01')` |
| `bars_2026_04` | 666,146 | `['2026-04-01', '2026-05-01')` |
| `bars_2026_05` | 651,616 | `['2026-05-01', '2026-06-01')` |
| `bars_2026_06` | 699,245 | `['2026-06-01', '2026-07-01')` |

Rows outside `['2026-03-01', '2026-07-01')`: **0**. Span `2026-03-02 13:01:00+00` to
`2026-06-30 20:54:00+00`. `symbols` 100 and `market_days` 1,484, both copied **in full and
first** — see below. Grouping the parent's rows by their source table returns four rows matching
the four counts above, which is what distinguishes a copy that was attached from one that was
not.

### The arithmetic, and why the window is a choice rather than a limit

The copy is **584,507,392 bytes**, which is 557 MB:

| | bytes |
| --- | --- |
| `bars_2026_03` | 152,854,528 |
| `bars_2026_04` | 142,417,920 |
| `bars_2026_05` | 139,345,920 |
| `bars_2026_06` | 149,512,192 |
| `market_days` | 303,104 |
| `symbols` | 73,728 |
| sum | **584,507,392** |

Against a 10 GB ceiling — half the 20 GB the instance is provisioned with — that is **18.4× under,
5.44% of the ceiling**. The depth floor runs the other way: the deep-pagination claim needs
1,000,000 + 1,000 + 1 = **1,001,001** rows inside the window a request can actually ask for, and
the window the deep page requests holds **2,009,487** session rows, clearing it by **2.007×**. The
widest window the cap permits inside these months holds 2,028,716, clearing by 2.027×. The two
bounds do not cross and neither is close, so the deep-page depth is not reduced.

**The whole development database is 6.52 GiB and would itself fit under that ceiling.** So the hot
window is a design choice, not a storage constraint: batch ingestion runs where disk is cheap and
the deployed service serves a recent window. A reader who divides 557 MB by 20 GB will ask, which
is why the margin is written down rather than left to look like a padded guess.

### Three ways this copy fails silently, all three checked

**The partial index is not copied.** Creating a partition with `LIKE bars INCLUDING ALL` copies
the indexes of the table named in the `LIKE` — the parent — and the hot-window partial index
deliberately lives on the children. Measured here: each new partition arrived with **two**
indexes, the primary key and the `(ts, symbol)` index, and **zero** hot-window indexes. So the
copy arrives with exactly the index the hot window exists to have, missing. The four were created
by hand afterwards and their definitions compared character for character against the development
database's live catalog, which is a stronger comparison than against the published template. The
listing is taken **twice**, before and after: one listing taken only afterwards cannot tell an
index that was copied from one that was created.

**The two small tables must go first, in full.** Both are populated by the ingestion pipeline, and
the pipeline never runs against this database, so replaying the migrations leaves them empty — and
nothing warns you. The service starts, the health check passes, every request for a named symbol
is a correct 404 by the existence rule, and every session-bounded query joins an empty calendar
and correctly returns no rows. The whole end-to-end suite then fails on a build that is working.
The check that makes "first" mean something is reading all three counts at the one moment both
small tables are full and `bars` is still empty: **0, 100, 1484**.

**Analysing is not the same as vacuuming.** Analysing fixes the statistics the planner costs with.
It does not set the visibility map, and the published hot-window result is an index-only scan with
no heap fetches at all — on an unset map such a scan silently falls back to fetching from the heap
and reads an order of magnitude more blocks, which a reader then attributes to a missing index or
to the window size. So the copy is followed by a vacuum **and** an analyse, not an analyse alone.

That third one has a trap of its own worth stating, because it tempts a reader into skipping the
step. Read 27 seconds after the copy finished, the four partitions were already at **99.42% to
99.94%** all-visible — autovacuum had reached all four within half a minute. None was complete, so
the explicit vacuum did real work: it took all four to **100.0000%** and truncated the empty
trailing pages, bringing the page counts to exactly the development database's. **The map does not
stay empty; it fills part-way, on its own, fast enough to look finished.** Read it immediately or
the reading argues for skipping the step that produced it.

### Do not move a partition with a table-level dump

```
Do not pg_dump -t a child partition. The restored table is not attached to bars, so every
query through the parent returns nothing, correctly, forever.
```

Selecting a child by name is unsupported for standalone restore: the dump makes no attempt to
include objects the selected table depends upon, and the parent, its partition key and the
statement that binds the child to it are all such dependencies. The failure is the quiet one — a
table that restores without error, carries the right name and the right rows, and is simply not
part of the hierarchy. The emitted definitions vary between dump versions, so a bad dump cannot
reliably be recognised by reading it. Creating the partitions and copying rows through the parent,
as above, sidesteps the question entirely. `--table-and-children` and `--load-via-partition-root`
are the right flags if this hierarchy is ever dumped; neither is needed here.

### What was deliberately not copied, and what was

`ingest_progress` is **not** copied. It is read by the coverage query, the ingestion pipeline and
the tests, and by no request handler — so an empty one here is correct, and saying so stops a
future reader treating it as a failed copy.

`first_bar_ts` **is** copied as it stands, which means it names an instant earlier than any row
this database holds, because it was derived from the whole history rather than from the copied
window. Recomputing it would make the same endpoint answer differently here than it does locally,
for no benefit to any check. Copied in full, and recorded.

### Reading a plan difference, in this order

A plan that differs from the published figures is attributed in a fixed order, because the
explanations are not interchangeable and the cheapest one is also the most common:

1. **Is the partial index there?** Four of them, one per partition, verified against the
   development catalog.
2. **Is the visibility map complete?** 100.0000% on all four, and the page counts match.
3. **Do the engine version and the planner settings agree?** Engine 16.15 both sides; the three
   pinned settings read back from the server; the parameter group in sync.
4. **Only then, the window size.** Four months here against seventy-one.

Getting this order wrong is how "it has four months instead of seventy-one" comes to explain a
difference that is really a missing index. With the first three confirmed,
**plans on RDS will differ on the smaller window** and that difference is the one measured.

Measured here on the statement the page-cost comparison uses, at the pinned parameters: an
**index-only scan on each planned partition's hot-window index, with no heap fetches**, no sort
node above the scans, and three of the four partitions planned — the fourth is excluded by the
window's own start, before execution begins. Eight blocks per page, identical across five runs,
and the structured plan's root cross-checked against the text plan's.

Two conditions of that comparison are recorded beside every figure taken from it. The pinned
settings are `work_mem` 4096 kB, `effective_cache_size` 524288 pages and
`max_parallel_workers_per_gather` 2. The unpinnable difference is `shared_buffers`, 23081 pages
here against 16384 locally, which moves the proportion of a read that comes from cache and not the
number of blocks read.

## Teardown

Run this on the teardown date, in this order, and only after the demo capture has been played back
and confirmed usable — the capture is what outlives the URL, and nothing below is reversible.

The order carries two dependencies that are not obvious. A service must go before its cluster, or
the cluster delete fails. The RDS instance must go before the security groups, because a group still
referenced by the instance or its network interface cannot be deleted. And the billing alarms come
last, after the $0 confirmation: deleting the alarm before the bill is confirmed zero removes the
only thing that would say it is not.

- [ ] 1. ECS service

```
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --desired-count 0
aws ecs delete-service --cluster "$CLUSTER" --service "$SERVICE" --force
# check
aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --query 'services[0].status'
#   -> INACTIVE, or an empty services[]
aws ecs list-clusters --query 'length(clusterArns)'        # -> 1, the control: the cluster is still there
```

- [ ] 2. ECS cluster

```
aws ecs delete-cluster --cluster "$CLUSTER"
# check
aws ecs list-clusters --query 'length(clusterArns)'                 # -> 0
aws ecr describe-repositories --query 'length(repositories)'        # -> 1, the control
```

- [ ] 3. RDS instance, with no final snapshot — a retained snapshot keeps billing

```
aws rds delete-db-instance --db-instance-identifier "$RDS_ID" \
  --skip-final-snapshot --delete-automated-backups
# check
aws rds describe-db-instances --db-instance-identifier "$RDS_ID"
#   -> exits 254 with DBInstanceNotFound. That exit code is the pass.
aws rds describe-db-snapshots --query 'length(DBSnapshots)'                 # -> 0
aws rds describe-db-parameter-groups --query 'length(DBParameterGroups)'    # -> 2 or more, the control
# the service-managed address goes with the instance. If it does not, it bills by the hour
# indefinitely, and nothing else on this list would notice.
aws ec2 describe-addresses --query 'length(Addresses)'                      # -> 0
```

- [ ] 4. ECR images, then the repository

```
aws ecr batch-delete-image --repository-name "$ECR_REPO" --image-ids imageTag="$TAG"
aws ecr delete-repository --repository-name "$ECR_REPO" --force
# check
aws ecr describe-repositories --query 'length(repositories)'   # -> 0
aws logs describe-log-groups --query 'length(logGroups)'       # -> 1, the control
```

- [ ] 5. Security groups — after the RDS instance, or the delete fails on a dependency

```
aws ec2 revoke-security-group-ingress --group-id "$RDS_SG" --protocol tcp --port 5432 \
  --source-group "$SVC_SG"
aws ec2 delete-security-group --group-id "$RDS_SG"
aws ec2 delete-security-group --group-id "$SVC_SG"
# check
aws ec2 describe-security-groups --group-ids "$RDS_SG"              # -> InvalidGroup.NotFound, exit 254
aws ec2 describe-security-groups --query 'length(SecurityGroups)'   # -> 1 or more, the control: the
                                                                    #    VPC's default group remains
```

- [ ] 6. CloudWatch log group

```
aws logs delete-log-group --log-group-name "$LOG_GROUP"
# check
aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query 'length(logGroups)'   # -> 0
```

- [ ] 7. The SSM parameter, the execution role and the DB parameter group

```
aws ssm delete-parameter --name "$SSM_DSN"
aws iam delete-role-policy --role-name "$EXEC_ROLE" --policy-name read-the-dsn
aws iam detach-role-policy --role-name "$EXEC_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam delete-role --role-name "$EXEC_ROLE"
aws rds delete-db-parameter-group --db-parameter-group-name "$PG_GROUP"
# check
aws ssm get-parameter --name "$SSM_DSN"                                         # -> ParameterNotFound, exit 255
aws iam get-role --role-name "$EXEC_ROLE"                                       # -> NoSuchEntity, exit 254
aws rds describe-db-parameter-groups --db-parameter-group-name "$PG_GROUP"      # -> exit 254
aws sts get-caller-identity --query Account                                     # -> the account id, the control
```

- [ ] 8. Confirm the next day's cost is $0 — and the default view is wrong for this

```
aws ce get-cost-and-usage --time-period Start=<yesterday>,End=<today> \
  --granularity DAILY --metrics UnblendedCost \
  --filter '{"Not":{"Dimensions":{"Key":"RECORD_TYPE","Values":["Credit","Refund"]}}}'
#   -> 0 for the day after the teardown
```

On a credit-funded account, Cost Explorer's default view nets credits to $0 whether or not a
resource is still running, so the `RECORD_TYPE` filter is what makes this check mean anything. Cost
Explorer must have been enabled when the account was opened; its data takes roughly 24 hours to
populate. Run the same query over a day while the service was up as a control — it must return a
non-zero figure, because "$0" is also what an unenabled Cost Explorer and a mistyped time period
both print.

- [ ] 9. Last, after the $0 confirmation: the SNS topic and subscription, the two alarms, the two budgets

```
aws cloudwatch delete-alarms --alarm-names "$ALARM_5" "$ALARM_20" --region us-east-1
aws budgets delete-budget --account-id "$ACCOUNT" --budget-name "$BUDGET_5"
aws budgets delete-budget --account-id "$ACCOUNT" --budget-name "$BUDGET_20"
aws sns delete-topic --topic-arn "$TOPIC"
# check
aws cloudwatch describe-alarms --alarm-names "$ALARM_5" "$ALARM_20" --region us-east-1 \
  --query 'length(MetricAlarms)'                                     # -> 0
aws budgets describe-budgets --account-id "$ACCOUNT" --query 'length(Budgets)'   # -> 0
aws sns list-topics --query 'length(Topics)'                          # -> 0
aws sts get-caller-identity --query Account                           # -> the account id, the control
```

A resource left behind is the most common way a finished project keeps costing money.
