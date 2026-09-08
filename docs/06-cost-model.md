# Cost model

**Budget: $100 initially, top-up available later.** Ask for the top-up at
~$70 spent, not at $100 — a mid-build funding gap stalls work at the worst point.

## Account setup — do this before creating any resource

Choose the **Paid Plan**, not the Free Plan. Both give the same $100 signup
credit plus up to $100 more from onboarding tasks ($20 each: launch and
terminate EC2, configure RDS, deploy a Lambda, test a Bedrock prompt, set a
budget). The difference: **a Free Plan account closes automatically after six
months or when credits run out, and you lose access to your resources and
data.** Credits expire 12 months from account opening either way.

Then: set AWS Budgets alerts at **$25 / $50 / $75**. Pick one region and stay
in it. Set CloudWatch log retention before writing any logs.

## Beta budget

| Line | Assumption | Low | High |
|---|---|---|---|
| Bedrock inference | 6 full runs | $36 | $60 |
| RDS Oracle SE2 | 60 instance-hours | $15 | $28 |
| RDS storage | 20 GB gp3, engine minimum | $2 | $2 |
| DMS | 15 hrs, dms.t3.micro | $0.27 | $0.54 |
| Aurora PostgreSQL Serverless | Free plan allowance (4 ACU / 1 GiB) | $0 | $0 |
| S3 | under 2 GB | $1 | $2 |
| CloudWatch Logs | 7-day retention | $1 | $2 |
| Everything else | SNS, EventBridge, SSM, IAM, API GW, CloudTrail | $1 | $3 |
| **Total** | | **~$56** | **~$98** |

## The line that decides the budget

**Bedrock.** Roughly $6-10 per full run over the estate.

**Build the SHA-256 unchanged-object skip on day one, not later.** A re-run
against an unchanged estate drops from ~220 model calls to under 20, turning an
$8 run into $0.30. In a beta where you re-run constantly while the rules engine
is wrong, that single feature decides whether the budget holds.

Also: use the fast/cheap model tier for narration of already-computed facts.
Reserve the reasoning tier for genuine judgement — code dependency analysis,
root cause, remediation SQL, edition rationale.

## Free-tier reality checks

- **RDS for Oracle is not free-tier eligible in any form.** Free plan RDS covers
  db.t3.micro/t4g.micro for MySQL, PostgreSQL, MariaDB and SQL Server Express
  only.
- **Bedrock has no free tier.**
- Lambda (1M requests + 400,000 GB-s/month) and Step Functions (4,000 state
  transitions/month) are always-free. The pipeline uses ~2,000 transitions per
  run, so two full runs a month are free.

## Beta shortcuts taken (security debt — see 05-aws-services.md)

Lambdas outside the VPC · public RDS endpoint locked to one IP · Parameter
Store instead of Secrets Manager · AWS-managed KMS keys · deploy and validate
workers run locally instead of on Fargate. None survive into v1.
