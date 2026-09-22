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

### Model bindings and what they cost — updated 2026-09-09

Both tiers in `bedrock/models.json` now bind current-generation models:

| Tier | Model | Why |
|---|---|---|
| fast | Claude Haiku 4.5 | narration only |
| reasoning | Claude Sonnet 4.5 | **all judgement, including anything Opus-class** |

**There is no Opus tier, by decision.** Sonnet 4.5 handles every reasoning task
here. Opus profiles are available on the account but binding one is a cost
choice that needs justifying, not a default.

**The fast tier got more expensive.** Haiku 4.5 is roughly **4× the token cost**
of the Claude 3 Haiku it replaced. Since the fast tier carries the high-volume
narration calls, that is the line most likely to move the $6–10 per-run figure
above. Two mitigations, in order:

1. **The SHA-256 unchanged-object skip matters more now, not less** — it removes
   most fast-tier calls on a re-run, which is where the volume is.
2. If fast-tier spend still binds the budget, `bedrock/models.json` keeps
   `alternates.fast_legacy` pointing at Claude 3 Haiku. Swapping the fast tier
   back is a one-line config edit and touches no phase code.

Re-derive the per-run figure once a real run is measurable — the numbers above
are planning approximations, and no full run has executed yet.

## Free-tier reality checks

- **RDS for Oracle is not free-tier eligible in any form.** Free plan RDS covers
  db.t3.micro/t4g.micro for MySQL, PostgreSQL, MariaDB and SQL Server Express
  only.
- **Bedrock has no free tier.**
- Lambda (1M requests + 400,000 GB-s/month) and Step Functions (4,000 state
  transitions/month) are always-free. The pipeline uses ~2,000 transitions per
  run, so two full runs a month are free.

## Kill switch — `python -m killswitch`

Built 2026-09-11, before the first billable resource, on purpose.

```powershell
python -m killswitch                                    # look only; changes nothing
python -m killswitch --stop    --confirm 106325261146   # pause
python -m killswitch --destroy --confirm 106325261146   # delete everything dbshift-owned
python -m killswitch --all-regions                      # scan every region, not just ap-south-1
```

**What it touches:** only resources named `dbshift*` or tagged `project=dbshift`.
The `DBA_permissions` role has `rds:*` and `dms:*`, so IAM protects nothing else
in the account — this filter does. Everything else that bills is still listed
(a kill switch that hides a running instance lies about the bill) but is never
acted on.

**Stop is a pause, not an off switch.** A stopped RDS instance **restarts itself
after 7 days** and bills again, and storage bills throughout. DMS replication
instances and NAT gateways cannot be stopped at all. Use `--destroy` to end
billing.

**Destroy** deletes `dbshift-` CloudFormation stacks (which deletes what they
created — a stack-owned resource is never deleted underneath its stack), then
any of ours not owned by a stack. **S3 buckets are emptied first** — Phase 6's
template creates an exchange bucket for Data Pump dumps, and CloudFormation cannot
delete a stack whose bucket still holds objects; it would stop at
`DELETE_FAILED`. No final snapshot is taken: the target is a
copy, the source is the system of record, and a snapshot would be a new
billable thing the kill switch created. Two things it will not do without an
explicit flag: delete a manual snapshot (`--include-snapshots` — a backup, and
deletion is permanent) and override deletion protection
(`--force-deletion-protection`).

**Safety:** `--confirm` must equal the account id the credentials resolve to,
or it refuses. Exit code is `0` when nothing of ours bills and `2` when
something does, so a scheduled check can alarm without parsing output.
`python -m killswitch.selftest` proves the targeting against a stubbed account
in which touching a foreign resource raises. It runs offline.

**First real run, 2026-09-11:** inventory only, `ap-south-1` —
nothing billable in the account. A `Monthly_budget` of $100 exists; the
$25 / $50 / $75 alerts above have not been set.

## Beta shortcuts taken (security debt — see 05-aws-services.md)

Lambdas outside the VPC · public RDS endpoint locked to one IP · Parameter
Store instead of Secrets Manager · AWS-managed KMS keys · deploy and validate
workers run locally instead of on Fargate. None survive into v1.
