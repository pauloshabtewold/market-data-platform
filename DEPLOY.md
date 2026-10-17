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

Teardown is 14 days after opening, inside the credit window on either the local or the UTC
reading of the opening date.
## The credit model, and why there is a teardown date

The $200 credit expires six months after the account was opened, not when it is spent, so the
expiry ends this deployment rather than the spend does. That is why the teardown date is fixed
before anything bills.
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

**`EstimatedCharges` is reported NET of credits. Settled by measurement 2026-10-07.** A
net-of-credits alarm is a bound on nothing for as long as the balance lasts, so the two alarms on
this account bound nothing today, and the budgets above — with `IncludeCredit` `false` — are the
bound that actually holds. That is the case this section was written against, and it is the one
that obtained.

**The budgets see through it, which is the whole reason they exist.** Read 2026-10-07 against
limits of $5 and $20, `IncludeCredit` `false`:

```
market-data-5    ActualSpend 0.948    limit 5.0
market-data-20   ActualSpend 0.948    limit 20.0
```

Measured rate: **~$17-18 a month** against the $25-30 estimated, about **$7** over the
twelve days to teardown, 3.4% of the $200 credit.
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
ADMIN_USER=$PROJECT-admin
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

The repository was created after the alarms, so the billing bound predates the first thing
that bills.
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
Public address: http://100.58.98.13:8000/docs   (the bare address has no route and answers 404)
Five temporary `/32` ingress windows were opened between 2026-10-06 and 2026-10-07, each
revoked inside the step that needed it.
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

`shared_buffers` differs between the two instances and cannot be pinned — it has
`postmaster` context. It moves which counter a block increments and not how many blocks are
touched, and `Shared Hit + Shared Read` is the total either way.
### Why the database is publicly accessible

`PubliclyAccessible` is **true**, deliberately. Two steps run `psql` from a laptop, and with it
off the instance has no public DNS or IP to resolve at all — so a temporary firewall rule does not
help and the remedy becomes a bastion host or a VPN, which are more billable resources on a finite
balance. It also cannot be flipped later without a modify-and-reboot.

No `/32` window is left open: each is revoked inside the step that needed it.
The service's group allows 8000 from `0.0.0.0/0` and carries **no rule for any single
address** — nothing ever connects to the task directly.

The service has no authentication and no rate limit, accepted for a time-boxed demo over
public read-only data; `README.md` publishes the same caveat.
### One address is allocated that no step asked for

A `PubliclyAccessible` RDS instance is given a service-managed Elastic IP — `54.208.233.222`,
on `eni-076036e91032399c3`, `RequesterManaged true`. Addresses this project allocated read 0;
the managed one reads 1. **Teardown item 3 must re-read `length(Addresses)` as 0 *after* the
instance goes**: an address left allocated and unassociated bills by the hour, and nothing else
on the checklist would notice.
### What is deliberately absent

No load balancer, no NAT gateway, and no task-level health check. The first two are ruled out on
cost. The health check is omitted because without a load balancer the public address belongs to
the task's own network interface and **changes every time the task is replaced** — so a
health-check-driven replacement would quietly invalidate any address written down. The task's
address is auto-assigned and is not an elastic address, which is why it survives nothing.

No TLS: it goes with the load balancer, and the payload is public read-only data.
`StorageEncrypted` is `false` by choice though it is free — a deployment holding anything
private would set it, and it cannot be turned on later without a snapshot and restore.
`assignPublicIp` is `ENABLED`, which is what lets the task pull its image with no NAT gateway.
`assignPublicIp` is `ENABLED`. That single setting is what lets a task in a public subnet pull
its image with no NAT gateway; without it the task dies before the application runs. The subnets
are public by route, not merely by flag — the route table carries a default route to an internet
gateway, which is the thing that actually has to be true.

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

The cutoff is `2026-03-01`, read from the application's own `_hot_window_cutoff` rather
than derived by hand.
Row counts, as four addends rather than one total, each equal to the development database's:

| partition | rows | bounds |
| --- | --- | --- |
| `bars_2026_03` | 715,229 | `['2026-03-01', '2026-04-01')` |
| `bars_2026_04` | 666,146 | `['2026-04-01', '2026-05-01')` |
| `bars_2026_05` | 651,616 | `['2026-05-01', '2026-06-01')` |
| `bars_2026_06` | 699,245 | `['2026-06-01', '2026-07-01')` |

### The arithmetic, and why the window is a choice rather than a limit

The four copied partitions hold **658,350,080 bytes**, which is **627.852 MiB**, measured on the
deployed instance:

| | bytes |
| --- | --- |
| `bars_2026_03` | 172,081,152 |
| `bars_2026_04` | 161,480,704 |
| `bars_2026_05` | 157,286,400 |
| `bars_2026_06` | 167,501,824 |
| sum | **658,350,080** |

**The heap copied byte-for-byte and the indexes did not.** The indexes were rebuilt by
`CREATE INDEX` and are **24.12%** larger, which accounts for the whole 12.71% total difference.
| | heap | indexes | total |
| --- | ---: | ---: | ---: |
| deployed | 276,185,088 | 381,943,808 | **658,350,080** |
| development | 276,185,088 | 307,724,288 | **584,130,560** |

**The heap is identical on both sides, to the byte and partition by partition** — `COPY` wrote the
rows in the order it read them, so the copy's page layout reproduces the source's exactly. The
indexes are **24.12% larger** on the copy, because those were rebuilt by `CREATE INDEX` rather than
copied, and a bulk build packs pages differently from the incremental inserts that produced the
originals. **That accounts for the whole of the size difference**: the copy's total is 12.71%
larger, equivalently the development figure is **11.27% smaller**, and the row counts are identical
either side.

So a size measured locally understates the copy, but only through the indexes. Take the total from
the deployed instance; the heap is the one component where either side answers.

The split above cost a fifth temporary `/32`, opened and revoked inside the step that needed it and
recorded with the pair above like the four before it.

The copy is **627.852 MiB** against a 10 GiB ceiling, **16.3× under**. It holds **2,009,487**
session rows against the 1,001,001 the deep page needs, **2.007×**. The whole development
database is 6.52 GiB and also under the ceiling, so the hot window is a design choice rather
than a storage constraint.
### Three ways this copy fails silently, all three checked

**The partial index is not copied.** `LIKE bars INCLUDING ALL` copies the parent's indexes
only, so each partition arrives with two indexes and **zero** hot-window indexes — the copy is
missing exactly the index the hot window exists for. The four were created by hand and compared
against the development catalog.
**The two small tables go first, in full.** `symbols` and `market_days` are copied before
any bar, and the check is one reading of all three at one moment: `bars` **0**, `symbols`
**100**, `market_days` **1484**.
**Analysing is not the same as vacuuming.** The copy is followed by `VACUUM (ANALYZE)`
and not `ANALYZE` alone: analysing fixes the statistics and not the visibility map, and the
published result is an index-only scan with no heap fetches.
Autovacuum may already have run by the time the map is read, so a reading of 99%
rather than 0% before the manual pass is expected and not a sign the statement was wrong.
### Do not move a partition with a table-level dump

```
Do not pg_dump -t a child partition. The restored table is not attached to bars, so every
query through the parent returns nothing, correctly, forever.
```

Use `--table-and-children` with `--load-via-partition-root`; selecting a child by name
is unsupported for a standalone restore.
### What was deliberately not copied, and what was

`ingest_progress` is not copied: it records what the ingest did, and the copy did not ingest.
`first_bar_ts` on `symbols` **is** copied as it stands, so the deployed service reports each
symbol's first bar in the full history — a date it will then refuse as a window bound.
### Reading a plan difference, in this order

A plan that differs from the published figures is attributed in a fixed order, because the
explanations are not interchangeable and the cheapest one is also the most common:

Measured here on the statement the page-cost comparison uses, at the pinned parameters: an
**index-only scan on each planned partition's hot-window index, with no heap fetches**, no sort
node above the scans, and three of the four partitions planned — the fourth is excluded by the
window's own start, before execution begins. Eight blocks per page, identical across five runs,
and the structured plan's root cross-checked against the text plan's.

## Teardown

**Items 1-7 ran 2026-10-16 and every billable class reads zero.** Items 8-10 are open by design:
item 8 confirms the FOLLOWING day's cost, and item 9 must not run before it.

Run this on the teardown date, in this order, and only after the demo capture has been played back
and confirmed usable — the capture is what outlives the URL, and nothing below is reversible.

The order carries two dependencies that are not obvious, and two rules that are not dependencies at
all. A service must go before its cluster, or
the cluster delete fails. The RDS instance must go before the security groups, because a group still
referenced by the instance or its network interface cannot be deleted. The billing alarms come after
the $0 confirmation: deleting the alarm before the bill is confirmed zero removes the only thing that
would say it is not. And the admin access key comes after all of it, because deleting it is what
stops every command on this list from working.

- [x] 1. ECS service

```
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --desired-count 0
aws ecs delete-service --cluster "$CLUSTER" --service "$SERVICE" --force
# check
aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --query 'services[0].status'
#   -> INACTIVE, or an empty services[]
aws ecs list-clusters --query 'length(clusterArns)'        # -> 1, the control: the cluster is still there
```

- [x] 2. ECS cluster

```
aws ecs delete-cluster --cluster "$CLUSTER"
# check
aws ecs list-clusters --query 'length(clusterArns)'                 # -> 0
aws ecr describe-repositories --query 'length(repositories)'        # -> 1, the control
```

- [x] 3. RDS instance, with no final snapshot — a retained snapshot keeps billing

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

- [x] 4. ECR images, then the repository

```
aws ecr batch-delete-image --repository-name "$ECR_REPO" --image-ids imageTag="$TAG"
aws ecr delete-repository --repository-name "$ECR_REPO" --force
# check
aws ecr describe-repositories --query 'length(repositories)'   # -> 0
aws logs describe-log-groups --query 'length(logGroups)'       # -> 1, the control
```

- [x] 5. Security groups — after the RDS instance, or the delete fails on a dependency

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

- [x] 6. CloudWatch log group

```
aws logs delete-log-group --log-group-name "$LOG_GROUP"
# check
aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query 'length(logGroups)'   # -> 0
```

- [x] 7. The SSM parameter, the execution role and the DB parameter group

```
aws ssm delete-parameter --name "$SSM_DSN"
aws iam delete-role-policy --role-name "$EXEC_ROLE" --policy-name read-the-dsn
aws iam detach-role-policy --role-name "$EXEC_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam delete-role --role-name "$EXEC_ROLE"
aws rds delete-db-parameter-group --db-parameter-group-name "$PG_GROUP"
# check
aws ssm get-parameter --name "$SSM_DSN"                                         # -> ParameterNotFound, exit 254
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

**The control has now returned a non-zero figure, so this check is exercised.** Tried on
2026-10-06 the query answered `DataUnavailableException` — the account was 22 hours old against Cost
Explorer's roughly 24-hour first ingest, so the control was as unrunnable as the check it guards,
and both of the states it exists to separate printed the same thing. Run again on 2026-10-07 over a
day the service was up, the `RECORD_TYPE`-filtered query returns **`Usage` +0.1368083634 USD**. That
is the reading that turns item 8's $0 into evidence: a genuine $0 after teardown is now
distinguishable from a Cost Explorer that never populated. **Do not re-run it to confirm** — each
Cost Explorer API request is billable, three have been spent reaching this figure, and the one
remaining use for the API is item 8's own post-teardown query.

- [ ] 9. After the $0 confirmation: the SNS topic and subscription, the two alarms, the two budgets

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

- [ ] 10. Last of all: the administrator access key — after this, none of the above can run

The IAM user `market-data-admin` holds one `Active` access key, and it is the credential every
command on this list runs as. Items 1 to 9 delete every resource that bills, and item 9 deletes both
alarms and both budgets — so an account left finished at item 9 has a working administrator
credential on it and no spend bound of any kind watching what that credential could create. **That
is the state this item exists to prevent**, and it is the one item on the list whose subject is not a
resource.

It comes last because it is self-disabling: the moment the key is gone, every command above stops
working.

```
# read the id while there is still a credential to read it with
aws iam list-access-keys --user-name "$ADMIN_USER" \
  --query 'AccessKeyMetadata[].{Id:AccessKeyId,Status:Status}'
KEY_ID=                    # the id printed above
# optional, and free either way: a task definition bills nothing at any revision count
aws ecs deregister-task-definition --task-definition "$TASK_FAMILY":1
# terminal. Delete rather than deactivate -- deactivating this key is itself what would
# stop the delete, and a deleted key needs no second step
aws iam delete-access-key --user-name "$ADMIN_USER" --access-key-id "$KEY_ID"
# check -- this call must FAIL, and the failure is the pass
aws sts get-caller-identity
#   -> an authentication error. A call that succeeds here means the key is still live.
```

Confirm the key count is zero from the console rather than the CLI; there is no longer a credential
to ask with. The IAM user itself costs nothing and can be left. So can the task definition: the
`deregister` above is tidiness and not thrift, since `market-data-api:1` bills nothing, and it is
the one thing items 1 to 9 leave standing precisely because leaving it costs nothing.

A resource left behind is the most common way a finished project keeps costing money. A credential
left behind is the most expensive.
