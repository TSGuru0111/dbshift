# AWS services and access

**Account granted 2026-09-09.** Access is via IAM Identity Center (SSO) with a
`DBA_permissions` permission set. Read `06-cost-model.md` before creating
anything billable.

## Account facts

| | |
|---|---|
| Account name | Database Migration Accelerator |
| Account ID | `106325261146` |
| Permission set | `DBA_permissions` |
| SSO start URL | `https://identitycenter.amazonaws.com/ssoins-6595cfc9d3921ce1` |
| SSO region | `ap-south-1` |
| Working region | `ap-south-1` (Mumbai) — pick one and stay in it |

Full permission set: `infra/dba-permissions-policy.json`.

## Credentials — never stored in this repo

The access portal offers three options. **Use `aws configure sso`, not the
pasted keys.** Portal keys are `ASIA…` session credentials that expire in hours;
copying them into a file or `.env` means re-pasting them several times a day and
leaving live secrets on disk in between.

The SSO profile stores **no secrets** — only the start URL, region, account ID
and role name, all of which are already in this file.

### One-time setup

```powershell
winget install -e --id Amazon.AWSCLI      # or the MSI from AWS
aws configure sso
```

Answer with:

```
SSO session name            : dbshift
SSO start URL               : https://identitycenter.amazonaws.com/ssoins-6595cfc9d3921ce1
SSO region                  : ap-south-1
SSO registration scopes     : sso:account:access
Account                     : 106325261146
Role                        : DBA_permissions
Default client Region       : ap-south-1
Default output format       : json
CLI profile name            : dbshift
```

That writes `%USERPROFILE%\.aws\config` — a config file, not a credentials file.

### Daily use

```powershell
aws sso login --profile dbshift
$env:AWS_PROFILE = "dbshift"
aws sts get-caller-identity
```

`aws sso login` opens a browser, and the CLI refreshes short-lived credentials
itself from then on. Nothing in this repo ever reads a secret key.

**Never** commit `.aws/credentials`, paste portal keys into a file, or add
`AWS_SECRET_ACCESS_KEY` to a `.env`. `.gitignore` already blocks
`credentials*` and `*secrets*`, but the rule is the point, not the safety net.

---

## What the permission set unblocks

Bedrock invoke · Lambda · Step Functions · API Gateway · EventBridge · RDS
(including Aurora) · DMS · S3 · SSM Parameter Store · SNS · CloudWatch · Logs ·
ECS/ECR · Application Auto Scaling · Budgets · Cost Explorer · Service Quotas ·
CloudTrail read · full VPC and security-group management.

**Phase 4 is unblocked from an IAM standpoint.** See the Bedrock caveat below
before assuming it works.

## Four constraints that change how we build

**1. EC2 instance launch is explicitly denied.**
`ec2:RunInstances`, `StartInstances`, `CreateImage` and both Spot actions are
`Deny`. RDS, DMS replication instances and Fargate do **not** need these, so the
pipeline is unaffected — but **a bastion host is impossible**.

That turns a beta shortcut into a hard requirement: reaching RDS means either a
public endpoint locked to one IP, or VPC endpoints plus Lambda-in-VPC. There is
no jump box option to fall back on. Plan the network accordingly.

**2. CloudFormation is scoped to `dbshift-*` and `CDKToolkit`.**
Every stack **must** be named with the `dbshift-` prefix. A stack named anything
else fails at create time, not at deploy time.

**3. IAM roles are scoped to `dbshift-*`, `cdk-*` and three named DMS roles.**
**This will break `cdk deploy` with default settings.** CDK auto-generates role
names like `MyStack-MyLambdaServiceRole-AB12CD34`, which match none of the
allowed prefixes. Every construct that creates a role needs an explicit
`roleName` starting `dbshift-`, or the deploy fails partway through with an
`AccessDenied` on `iam:CreateRole` — after some resources already exist.

`cdk bootstrap` itself is fine: it creates the `CDKToolkit` stack and
`cdk-hnb659fds-*` roles, both permitted.

**4. No `kms:CreateKey`, no `secretsmanager:*`.**
AWS-managed KMS keys and SSM Parameter Store are now **enforced**, not chosen.
The customer-managed key and Secrets Manager move to v1 as planned — the policy
simply makes that non-negotiable today.

## The Bedrock caveat

`bedrock:InvokeModel` in IAM is **not** model access. Model access is opt-in
**per model, per region**, granted in the Bedrock console. The policy permits
the call; a model that has not been enabled in `ap-south-1` still returns
`AccessDeniedException`.

Verify before building anything that depends on it:

```powershell
aws bedrock list-foundation-models --region ap-south-1 --profile dbshift
```

If the intended model is absent, request access in the Bedrock console first.
Also confirm whether the model is reachable directly or only through an
inference profile — `bedrock:ListInferenceProfiles` is granted, which suggests
cross-region inference profiles are expected.

## Do these first, before any billable resource

1. **Budget alerts at $25 / $50 / $75.** `budgets:*` is granted, so there is no
   excuse to skip it. `06-cost-model.md` explains why the $100 credit is tight.
2. **Verify Bedrock model access** in `ap-south-1` (above).
3. **Check the RDS Oracle quota** — `servicequotas:RequestServiceQuotaIncrease`
   is granted, and a new account can have a low default instance limit.
4. **Confirm outbound HTTPS** to `*.amazonaws.com` from the build machine. A
   corporate proxy that MITMs AWS API traffic breaks the collector ingest path
   entirely.
5. **Set CloudWatch log retention** to 7 days at log-group creation. The default
   is never-expire.

## Conditional

**NAT Gateway** — only if a public RDS endpoint is refused. With no bastion
available (constraint 1), the alternatives are a NAT gateway at ~$33/month or
VPC interface endpoints. Decide before writing the network stack.

## Deferred to v1

Secrets Manager · customer-managed KMS key · QuickSight · Athena · Bedrock
Knowledge Base. The first two are now blocked by policy, which is the correct
outcome for a beta.

## Non-AWS prerequisites

- Oracle 21c XE installed locally (done)
- ~60 GB local disk
- AWS CLI v2 (not yet installed — see setup above)
