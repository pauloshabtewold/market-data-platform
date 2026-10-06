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
