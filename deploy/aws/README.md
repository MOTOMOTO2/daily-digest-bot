# Running on AWS

The same bot, with no server to keep running: **Lambda** runs it, **EventBridge
Scheduler** triggers it, **DynamoDB** holds its record, and a **CloudWatch**
alarm emails when something fails. GitHub Actions deploys it on every push to
`main`, signing in to AWS with OIDC, so no AWS keys are stored anywhere.

```
EventBridge Scheduler ──hourly {"mode":"run"}──────────┐
                     ──09:30 {"mode":"healthcheck"}──┐ │
                                                     ▼ ▼
   SSM Parameter Store ──webhook URL──▶ Lambda (Python 3.13, arm64) ──▶ #your-channel
                                           │      │
                                           ▼      ▼
                                   DynamoDB    CloudWatch Logs
                                 (one table)        │
                                                    ▼
                         Errors metric ──▶ alarm ──▶ SNS ──▶ email
```

| File | What it is |
|---|---|
| [`template.yaml`](template.yaml) | The application: table, function, two schedules, log group, alarm |
| [`bootstrap.yaml`](bootstrap.yaml) | One-time: GitHub's OIDC provider, the deploy role, an artifact bucket |
| [`config.toml`](config.toml) | The deployed bot's config; secrets come from SSM, not this file |
| [`build.sh`](build.sh) | Builds the Lambda bundle with Linux arm64 wheels, from any machine |
| [`../../.github/workflows/deploy-aws.yml`](../../.github/workflows/deploy-aws.yml) | Test, build, deploy, then invoke the function as a smoke test |

## What it costs

Nothing, at this size. Hourly runs are about 750 Lambda invocations a month at
256 MB, a few thousand DynamoDB writes on demand, two schedules and one alarm:
all well inside the AWS free tier.

## How failures surface

The CLI signals trouble with its exit code. On Lambda the equivalent is a
failed invocation: `digestbot/aws_lambda.py` raises when a delivery fails or
the health check reports DOWN, Lambda counts that in its `Errors` metric, and
the alarm emails on the first one. Source failures still appear in the digest
itself, exactly as they do locally.

## The DynamoDB design

The record is one table, in a single-table design; `digestbot/dynamo.py`
explains each key. The short version is how the SQLite guarantees carry over:

| SQLite | DynamoDB |
|---|---|
| `UNIQUE(fingerprint)` | A `FP#<fingerprint>` marker written in the same transaction as the item, on `attribute_not_exists` |
| `UNIQUE(digest_date)`: one digest a day | A `DAY#<date>` marker claimed in a transaction with the digest |
| `AUTOINCREMENT` ids | An atomic `ADD` on a counter record |
| Index on unsent items | A sparse index: only pending items carry the `PENDING` key |
| `COUNT(*)`, `SUM()` | Running totals updated with atomic `ADD` |
| `ON DELETE CASCADE`, `prune` | A run's results share its partition; runs and events get a TTL |

The test suite runs every store, pipeline and health test against both
backends, with DynamoDB simulated by [moto](https://github.com/getmoto/moto),
plus tests for what only exists on this side: two writers racing for the same
item or the same day, the sparse index, TTL and the cascade.

## One-time setup

You need an AWS account and the AWS CLI. Everything below uses `ca-central-1`;
any region with Lambda arm64 and EventBridge Scheduler works.

**1. Sign in from the terminal**

```bash
aws login            # opens the browser; or `aws configure sso`
```

**2. Create the deploy role** (once per account)

```bash
aws cloudformation deploy \
  --region ca-central-1 \
  --stack-name daily-digest-bot-bootstrap \
  --template-file deploy/aws/bootstrap.yaml \
  --capabilities CAPABILITY_IAM

aws cloudformation describe-stacks --region ca-central-1 \
  --stack-name daily-digest-bot-bootstrap --query "Stacks[0].Outputs"
```

If the account already has GitHub's OIDC provider, add
`--parameter-overrides CreateOidcProvider=false`.

**3. Tell the workflow where to deploy**

```bash
gh variable set AWS_REGION --body ca-central-1
gh variable set AWS_DEPLOY_ROLE_ARN --body "<DeployRoleArn from step 2>"
gh variable set AWS_ARTIFACT_BUCKET --body "<ArtifactBucket from step 2>"
gh variable set DIGEST_ALARM_EMAIL --body "you@example.com"   # optional
```

Push to `main` (or run the workflow by hand) and it deploys. Until a webhook
is set, the digest is written to the function's log in CloudWatch.

**4. Post to a real channel** (optional)

```bash
aws ssm put-parameter --region ca-central-1 --type SecureString \
  --name /daily-digest-bot/webhook-url --value "https://discord.com/api/webhooks/..."
gh variable set DIGEST_DELIVERY_KIND --body discord     # or slack, webhook
gh variable set DIGEST_WEBHOOK_PARAMETER --body /daily-digest-bot/webhook-url
```

The URL stays encrypted in SSM; the function reads it at start-up and the
template grants it read access to that one parameter only.

## Try it without AWS

The bundle runs in AWS's own Lambda image against DynamoDB Local:

```bash
deploy/aws/build.sh
docker network create digest-net
docker run -d --name ddb --network digest-net -p 8000:8000 amazon/dynamodb-local
docker run -d --name bot --network digest-net -p 9000:8080 --platform linux/arm64 \
  -v "$PWD/build/lambda:/var/task:ro" \
  -e DIGEST_CONFIG=config.toml -e DIGEST_DATABASE=dynamodb://digest-local \
  -e DIGEST_DYNAMODB_ENDPOINT=http://ddb:8000 -e AWS_REGION=ca-central-1 \
  -e AWS_ACCESS_KEY_ID=local -e AWS_SECRET_ACCESS_KEY=local \
  public.ecr.aws/lambda/python:3.13 digestbot.aws_lambda.handler
AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local python -c "from digestbot.dynamo import DynamoStore; \
  DynamoStore.create_table('digest-local', region='ca-central-1', endpoint_url='http://localhost:8000')"
curl -d '{"mode": "run"}' localhost:9000/2015-03-31/functions/function/invocations
```

The CLI works against the table too:
`DIGEST_DYNAMODB_ENDPOINT=http://localhost:8000 digest-bot --config ... status`
with `database = "dynamodb://digest-local"`.
