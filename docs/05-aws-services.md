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

The access portal offers three options. **`aws configure sso` is still the
right answer if you have it** — the SSO profile stores no secrets, only the
start URL, region, account ID and role name, all of which are already in this
file. But it needs **AWS CLI v2**, and v2 only ships as an MSI on Windows,
which needs **admin rights**. This account does not have them.

### What actually works on this machine

`winget install Amazon.AWSCLI` fails silently without admin (exit 1602,
elevation refused) — do not assume it succeeded just because winget reported no
error early on. **`pip install --user awscli`** installs **CLI v1** instead, no
admin required. Trade-off: this v1 build has no `aws sso login` /
`aws configure sso` — only the low-level `aws sso` API client
(`get-role-credentials`, `list-accounts`, …), not the login convenience
wrapper.

So until someone with admin installs CLI v2, the working path is a **static
session-credential profile**, refreshed by hand from the access portal:

```powershell
python -m pip install --user awscli
$env:Path += ";$env:APPDATA\Python\Python314\Scripts"    # or python313, matching your install
```

Portal → **Option 2** → click the **copy icon** (not manual selection — the
session token is 300–1000+ characters and gets visibly truncated on screen).
Paste into `%USERPROFILE%\.aws\credentials`:

```ini
[dbshift-static]
aws_access_key_id=...
aws_secret_access_key=...
aws_session_token=...
```

**Use a profile name that is not also an SSO profile in `config`.** A profile
that declares `sso_session` makes botocore attempt SSO token resolution first
and it will not fall back to static keys under the same name — this cost real
debugging time. Keep `dbshift` for the SSO profile (below, for whoever has CLI
v2) and `dbshift-static` for this one.

```powershell
aws sts get-caller-identity --profile dbshift-static
```

These tokens are **session-scoped and expire in hours** — that is by design,
not a workaround to fix. Re-copy from the portal each session; never write them
into a script, `.env`, or memory file. `.gitignore` already blocks
`credentials*` and `*secrets*`, `.aws/` and `aws_session*`, but the rule is the
point, not the safety net.

### One-time setup, once CLI v2 / admin is available

```powershell
winget install -e --id Amazon.AWSCLI
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

### Daily use, with CLI v2

```powershell
aws sso login --profile dbshift
$env:AWS_PROFILE = "dbshift"
aws sts get-caller-identity
```

`aws sso login` opens a browser, and the CLI refreshes short-lived credentials
itself from then on. Nothing in this repo ever reads a secret key.

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

### Tested 2026-09-12 — the cause has changed: **no payment instrument**

Re-tested with fresh session credentials (`DBA_permissions`, account
`106325261146`). Both tiers and the Claude 3 Haiku alternate return:

> Model access is denied due to **INVALID_PAYMENT_INSTRUMENT: A valid payment
> instrument must be provided.** Your AWS Marketplace subscription for this
> model cannot be completed at this time. If you recently fixed this issue, try
> again after 2 minutes.

This is not the Marketplace-permission error of 2026-09-09. The subscription
is now being attempted and refused because **the AWS account has no valid
payment method on file** for Marketplace charges. The fix is in the Billing
console, by an account administrator: add or fix the payment method, then
retry after about two minutes and run `python -m bedrock.verify`. Neither
IAM change below helps until that is done. `bedrock/client.py` now names this
cause explicitly instead of the generic "check model access" text.

### Tested 2026-09-09 — `ap-south-1` — **invoke is currently BLOCKED**

| Call | Result |
|---|---|
| `sts get-caller-identity` | OK — assumed-role `DBA_permissions` |
| `bedrock:ListFoundationModels` | OK — **75 models** returned |
| `bedrock-runtime:InvokeModel` (every model tried) | **AccessDeniedException** |

**Catalogue listing works; invoking does not.** The full error is the important
part, because it names a cause that is neither IAM `bedrock:*` nor console model
access:

> Model access is denied due to IAM user or service role is not authorized to
> perform the required AWS Marketplace actions
> (`aws-marketplace:ViewSubscriptions`, `aws-marketplace:Subscribe`) to enable
> access to this model. … Your AWS Marketplace subscription for this model
> cannot be completed at this time.

Bedrock validates a Marketplace subscription for the model at invoke time. This
account has no active subscription for these models, and the `DBA_permissions`
role cannot create one because the policy grants no `aws-marketplace:*` actions.
So Bedrock tries to self-subscribe, fails, and reports it as an access denial.

**Two ways to fix it — either is sufficient:**

1. **An account admin enables model access in the Bedrock console** for the
   wanted models in `ap-south-1`. This performs the subscription once,
   centrally, and is the cleaner option — the role then needs no Marketplace
   permissions at all. This is the step listed under "Do these first" that has
   not yet been done.
2. **Add the Marketplace actions to the permission set**, letting the role
   subscribe on first use:

   ```json
   {
     "Sid": "BedrockMarketplaceSubscription",
     "Effect": "Allow",
     "Action": [
       "aws-marketplace:ViewSubscriptions",
       "aws-marketplace:Subscribe"
     ],
     "Resource": "*"
   }
   ```

   Requires whoever administers the Identity Center permission set — it cannot
   be self-granted, since `iam:*` here is scoped to `dbshift-*` and `cdk-*`.

Re-run `python -m bedrock.verify` after either fix. The error message itself
advises waiting ~2 minutes after the change before retrying.

**A separate, unrelated failure mode worth knowing** (seen while testing, and
easy to mistake for a permissions problem): calling a current-generation model
by its bare `anthropic.*` / `amazon.*` id in `ap-south-1` returns

> `ValidationException: Invocation of model ID ... with on-demand throughput
> isn't supported. Retry your request with the ID or ARN of an inference profile
> that contains this model.`

That is a **routing** error, not an access error. `ap-south-1` carries no
on-demand throughput for those models directly; they route through a
cross-region inference profile (`apac.*` APAC-scoped, `global.*` global). Use
`aws bedrock list-inference-profiles` and bind the profile id. `bedrock/models.json`
already records which tier uses which form.

**Resolved — the earlier "unverified claim" was real but transient.** A session
on 2026-09-09 invoked Claude Sonnet 4 (`apac.*` profile), Sonnet 4.5 (`global.*`
profile) and Claude 3 Haiku (bare id) successfully — actual response bodies with
message ids, not a misread. `claude-sonnet-5` denied with a *different* error
(`is not available for this account`, no Marketplace text) — a real entitlement
gap, not this bug.

Roughly 10–15 minutes later, in the same session, the identical calls started
failing with the Marketplace subscription error — reproduced twice, once with a
freshly pasted session token and once with the separate `dbshift-static` static
profile already on disk, both resolving to the same role ARN
(`AROARRQL2XNNOVNVDSGNI`). `python -m bedrock.verify --include-alternates` also
returned 0/4.

**Working theory:** on a brand-new account, Bedrock's first invoke of a model
rides through before its self-subscribe attempt is authoritatively evaluated,
then later calls hit the real (denied) Marketplace state once it settles. Do
not rely on an early success surviving — re-run `bedrock.verify` immediately
before depending on it, and treat a pass as good only for that moment. This
does not change the fix: an admin still needs to do one of the two things
above.

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
