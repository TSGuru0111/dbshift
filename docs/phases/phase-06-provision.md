# Phase 6 — Provision

> **Latest update — 2026-09-11 (deployed and verified).** The approved deploy ran:
> stack `dbshift-target-dbmig-app` `CREATE_COMPLETE`, instance available at
> 07:53 UTC (≈16 min after the stack started) — **billing from then at
> $0.098/hour**. `verify` passed every check: S3_INTEGRATION role `ACTIVE`, class,
> engine version and licence as rendered, login as `dbshiftadm`, version
> **19.32**, AL32UTF8 / AL16UTF16. It also settled both open questions:
> **Oracle Text (`CONTEXT`) is installed and VALID** on RDS 19c without an option,
> and the instance is **non-CDB** (`v$database.cdb = NO`). The latter contradicts
> the reason Phase 3 gives for dismissing Multitenant ("RDS runs a container
> database by default") — the verdict stands, the stated reason does not; see
> `phase-03-size-edition.md`. The deploy's PowerShell host crashed writing output
> after the stack completed (exit 5); CloudFormation and the audit record were
> unaffected.
>
> **Previous — 2026-09-11 (deploy step built, not run).**
> `python -m provision.deploy` re-runs render and preflight, then refuses — before
> anything is created — unless the account matches `--confirm`, the operator
> types back the computed hourly rate with `--accept-hourly`, and, while the gate
> says HALT, an acknowledgement is given. The acknowledgement is **provision-only**,
> needs a reason, waives no blocker, and records the approver from the
> credentials themselves. Decision recorded 2026-09-11: the approver chose this
> route over waiving the four blockers. Create uses `OnFailure=DELETE`, so a
> failed deploy removes itself. `python -m provision.verify` then logs in and
> compares the live database with the render. Self-test 14/14; the real
> no-acknowledgement run was refused and the kill switch confirmed nothing was
> created. **No deploy has been run.**
>
> **Previous — 2026-09-11. Built: render + read-only preflight. Nothing is
> deployed.** `python -m provision.run` reads the Phase 2–5 records, refuses them
> unless they all come from one collector run, renders the RDS target as a
> CloudFormation template with a provenance line for every estate-derived value,
> and runs read-only AWS checks — including CloudFormation's own
> `ValidateTemplate`, which **accepted** it. On `DBMIG_APP` (run `6e48d16a`):
> `dbshift-target-dbmig-app`, Oracle EE BYOL `19.0.0.0.ru-2026-07.mrp-2026-07.r1`,
> `db.t3.medium`, 20 GB gp3, AL32UTF8 / AL16UTF16. **$0.098/hour + $2.62/month
> storage**, from AWS's public price list. Two warnings stand: the gate's overall
> verdict is HALT, and **21c → 19c is a version downgrade** because RDS offers no
> 21c for EE in `ap-south-1`. The deploy step is not built; it needs an explicit
> yes per deploy.

## Purpose

Stand up the migration target — and be able to say, of every property on it,
which record it came from. The architecture line is "CDK renders the RDS
instance from the assessment record"; the point is *from the record*, not from
someone's judgement at deploy time.

This is the first phase that can cost money. So it is split: rendering and
checking are free and happen every run; deploying is a separate, deliberate act.

## What actually happens

`provision/run.py :: execute()`

1. **Load the four records** — `provision/records.py`: `assessment.json`,
   `sizing.json`, `remediation_plan.json`, `gate_decision.json`.
2. **Refuse a mixed set.** All four must carry the same `collector_run_id`, or
   the run stops with `records_consistent: fail` and says which record differs.
3. **Read the source facts** from that collector run: `NLS_CHARACTERSET`,
   `NLS_NCHAR_CHARACTERSET`, version.
4. **Name the estate from the data** — the most frequent owner in the findings —
   and derive the stack name `dbshift-target-<estate>`. Never assumed.
5. **Gate check** — `preflight.gate_allows()`. `provision` must be clear in the
   Phase 5 per-phase view. An overall **HALT** is a **warning** for rendering and
   a stop for deploying (see Design decisions).
6. **Version direction** — source major vs target major. A downgrade is a
   warning with its consequence for Phase 7.
7. **Read-only AWS checks** — `preflight.aws_checks()`: identity; the latest
   orderable engine version for this class, licence and storage type (the
   `.spb` Spatial Patch Bundle line excluded); default VPC with subnets in ≥2 AZs;
   DB-instance quota headroom; stack name free; budgets present.
8. **Operator address** — `checkip.amazonaws.com`, as the single `/32` the
   security group will admit. Supplied as a template parameter, never baked in.
9. **Render** — `provision/render.py :: render()`. Pure function, no AWS calls.
10. **Validate** — CloudFormation `ValidateTemplate` on the rendered body.
11. **Price** — `provision/pricing.py`, from AWS's public offer file, if one is
    supplied with `--price-file`.
12. **Write** `provision/output/<stack>.template.json` and `provision_plan.json`.

### Deploy — `provision/deploy.py :: deploy()`

1. **Re-run steps 1–12.** Nothing is trusted from an earlier plan.
2. **Refuse, before anything is created**, when: preflight is not clean; the
   stack already exists (a `WARN` for rendering, a refusal for deploying — the
   create would otherwise fail at CloudFormation after the request was recorded);
   `--confirm` ≠ the account the credentials resolve to; `--accept-hourly` ≠ the
   rate just computed from the price list (no price → no deploy); or the gate
   says HALT and `--acknowledge-halt` is missing or under 15 characters.
3. **Record the request** in `provision/output/deployments.jsonl` — before the
   first billable call, so an interrupted deploy still leaves a trace.
4. **Password** — `ensure_password()` creates a random SecureString in SSM once
   (letters, digits, `_#`; starts with a letter) or reuses an existing one. The
   value is never returned, logged, or written.
5. **Create the stack** with `CAPABILITY_NAMED_IAM`, `OnFailure=DELETE`, a
   90-minute timeout, and stack tags `dbshift-requested-by` and (under HALT)
   `dbshift-halt-acknowledged-by`. Their keys differ from the template's resource
   tags so nothing collides when CloudFormation propagates them.
6. **Follow it** — `wait()` streams every stack event once and keeps every
   `*_FAILED` reason. It stops when the stack stops moving, or at the timeout
   (reported as `still_creating`, never auto-deleted).
7. **Result** — `created`, `failed_and_removed`, `failed` or `still_creating`,
   appended to `deployments.jsonl`; `deployed.json` on success.

### Verify — `provision/verify.py`

Logs in as the master user (password read from SSM into memory only) and checks,
against the render's provenance: S3_INTEGRATION role `ACTIVE`, class, engine
version, licence model, both character sets. It also settles the two questions
the render left open — `v$database.cdb`, and whether the `CONTEXT` (Oracle Text)
component is installed, which `IX_COMM_NOTES_TEXT` needs.

### What the template contains

| Resource | Why |
|---|---|
| `DbInstance` | The target. Every estate property traced — see below |
| `DbSecurityGroup` | Port 1521 from one operator `/32`. No bastion is possible: `ec2:RunInstances` is denied |
| `DbSubnetGroup` | Default VPC subnets, ≥2 AZs, resolved at preflight |
| `OptionGroup` | `S3_INTEGRATION` — see below |
| `S3IntegrationRole` | Named `dbshift-…-s3-integration`; the permission set only admits `dbshift-*` roles |
| `ExchangeBucket` | Where the Data Pump dump lands. Public access blocked, SSE, 7-day expiry |

### Where each value comes from

| Property | Source | Note |
|---|---|---|
| `Engine`, `LicenseModel` | Phase 3 edition | EE forced by `Partitioning (user)`; BYOL, 1 processor licence |
| `EngineVersion` | preflight, latest orderable | 19c only — a downgrade from the 21.3 source |
| `DBInstanceClass` | Phase 3 | capacity floor, not measured load |
| `AllocatedStorage`, `StorageType` | Phase 3 | 1.03 GB of segments; RDS Oracle minimum is 20 GB |
| `CharacterSetName` | Phase 3, **cross-checked** against the source NLS and Phase 4's `RDS-015` advice | a mismatch is a render error — it cannot be changed after creation |
| `NcharCharacterSetName` | collector `NLS_NCHAR_CHARACTERSET` | |
| `S3_INTEGRATION` | findings `RDS-003`, `RDS-004`, and Phase 7's load path | a dump reaches RDS only through S3 |
| Oracle Text | finding `RDS-008` | **not rendered, deliberately** — checked after create rather than guessed |

## Inputs / Outputs

| | |
|---|---|
| Input | the four phase records (same collector run) |
| Input | collector datasets `nls_parameters`, `database` for that run |
| Input | AWS profile `dbshift-static` (read-only calls only) |
| Input | `--price-file` — AWS public RDS offer file for the region (optional, kept out of the repo) |
| Output | `provision/output/<stack>.template.json` |
| Output | `provision/output/provision_plan.json` — checks, provenance, parameters, cost, handoffs |
| Exit code | `0` when a deploy could be offered, `1` otherwise |

## Design decisions

**CloudFormation JSON, not CDK.** The architecture names CDK. Here CDK would
need a Node toolchain, a bootstrap stack, and an explicit `dbshift-` role name on
every construct — CDK's generated names (`Stack-ConstructRole-AB12`) are refused
by the permission set, and the failure arrives mid-deploy after resources exist.
What CDK synthesises is a CloudFormation template; rendering that directly keeps
the output identical and reviewable without the toolchain. Reversible if CDK is
wanted later.

**A mismatch between records is a failure, not a warning.** On 2026-09-11 the
records disagreed twice: once `sizing.json` was another estate's entirely, once
two records came from a console run and two from the CLI. A target provisioned
from that mix answers questions nobody asked together.

**HALT warns on render and stops a deploy.** Phase 5 reports `provision` clear —
nothing in `NOARCHIVELOG`, missing PKs or the external table stops standing up
a target. But the gate's overall verdict is still HALT. Rendering proceeds
because it is free and informative. A deploy under HALT will need either
waivers in Phase 5 or a named acknowledgement — never a silent bypass.

**Price from AWS, never from memory.** The permission set has no
`pricing:GetProducts`, so the public offer file is read instead. The lookup
returns *every* match and refuses to estimate if more than one instance price
remains. That caught a real trap on its first run: **RDS Custom** lists
`db.t3.medium` Oracle EE BYOL at the same $0.098/hour under a different product;
it is excluded by `deploymentModel`, not by price.

**`DeletionPolicy: Delete` on the instance.** RDS's CloudFormation default is
`Snapshot` — every stack delete would leave a billable snapshot behind that
nothing tracks. The source is the system of record.

**The password is never in the template.** `MasterUserPassword` is an
`{{resolve:ssm-secure:…}}` dynamic reference to `/dbshift/<stack>/master-password`
in SSM Parameter Store. Secrets Manager is denied by the permission set.

**Beta guardrails are policy, not estate facts** — `provision/policy.py`:
single-AZ, 1-day backups, no deletion protection (the kill switch must be able to
remove it), no enhanced monitoring or Performance Insights, pinned minor version,
public endpoint locked to one `/32`, and an `expires-at` tag 8 hours out.

## Known limits

- **The deploy has never been run.** Self-tested against a stubbed account, and
  its refusal path run once against the real one. `verify.py` is untested until
  a real instance exists.
- **Acknowledgements live in `provision/output/deployments.jsonl`**, which is
  local and gitignored, and on the stack's tags. Durable, shared audit belongs in
  the metadata repository once it exists.
- **The SSM password parameter survives a teardown.** Standard-tier parameters
  are free, but the kill switch does not yet delete it.
- **`{{resolve:ssm-secure:…}}` on `MasterUserPassword` is unverified** until the
  first change set. `ValidateTemplate` does not resolve dynamic references.
- **Oracle Text on RDS 19c is unverified** — whether it needs an option. Checked
  after create (`dba_registry`, `comp_id = 'CONTEXT'`), not guessed.
- **Single-tenant or non-CDB is RDS's default, not a choice made here.** Sizing
  dismissed Multitenant on the basis of one PDB; confirm with
  `SELECT cdb FROM v$database` after create.
- **The downgrade is only warned about.** The assessment has no rule for target
  version below source, because Phase 2 does not know the target. Phase 7 must
  export with `VERSION=19` and validate for anything 21c-only.
- **Cost excludes** Oracle licences (BYOL — held by the client), data transfer,
  S3, and backup storage beyond the free allowance.

## How to run it

```powershell
python -m provision.run                      # render + read-only checks
python -m provision.run --price-file <path>  # ...and price it
python -m provision.run --no-aws             # render nothing AWS-dependent; offline checks only
python -m provision.selftest                  # deploy refusals and happy path, stubbed, no AWS

# The one that bills. Every flag is required by design:
python -m provision.deploy --confirm <account-id> --accept-hourly <rate-from-provision.run> `
    --price-file <path> --acknowledge-halt "<why provisioning is acceptable while blockers are open>"
python -m provision.verify                   # after a successful create
```

The price list is read from `provision/output/rds_ap-south-1_prices.json`
(gitignored) or `DBSHIFT_PRICE_FILE`. Download it from AWS's public offer file
for the region; prices change, so it is never committed.

**Console: Phase 6 · Provision.** It opens once the gate reports `provision`
clear — even under an overall HALT. It renders and checks with a live stage
ticker, then shows four tiles (engine, instance, per hour, *if forgotten for 30
days*), every preflight check, and the full provenance table. Below that:

- **Target — live from CloudFormation.** Status, endpoint, the last stack events,
  and the provision-only acknowledgement word for word with the blockers it left
  open. Polls every 15 s while the stack is moving. It follows a deploy started
  from the CLI just as well as one started here.
- **Verify the database** — enabled only at `CREATE_COMPLETE`.
- **Deploy** — account id, hourly rate typed back, and the acknowledgement
  reason. Every refusal comes back from the server synchronously, with its
  reason, before anything is created. The form hides once a stack exists.
- **Kill switch** — what is billing, marked ours or not ours, and *Destroy
  everything dbshift owns*, which needs the account id typed.

Deploy and Destroy are the only two red buttons in the console.

To remove anything this project created: `python -m killswitch --destroy --confirm <account-id>`
(see `docs/06-cost-model.md`). It empties the exchange bucket before deleting
the stack, because a stack cannot delete a bucket that holds objects.

## Change log

**2026-09-11 — showing the CloudFormation work in a demo.** The target panel now
shows the stack's *whole* creation, oldest first, timed from the first event
(`+0:00` … `+23:05` — every supporting resource inside the first minute, the
database instance the other 22), a link that opens the stack in the AWS console,
and **the template CloudFormation actually ran**, read back from the live stack
with `GetTemplate` rather than from the local render, which may since have been
re-rendered. All read-only. Also corrects an earlier figure: the instance's
create timestamp was ≈16 min after the stack started, but CloudFormation marked
it complete at +23:03 — 23 minutes end to end is the number to quote.

**2026-09-11 — first real deploy, and a self-test that wrote to the real record.**
Deployed as above. Reading `deployments.jsonl` afterwards showed four fake
deploys under account `111122223333`: `provision.selftest` called `deploy()`
with the module's real `OUTPUT`, so every self-test run appended to the genuine
audit record and overwrote `deployed.json` (which held the real stack only
because the real deploy happened to finish last). The self-test now writes to a
temporary folder, and the fake entries were removed from the record — the two
real ones (requested 07:37:39 UTC, created `CREATE_COMPLETE`, acknowledged by
`guru.ts@ganitinc.com`) are intact.

**2026-09-11 — console stage.** Phase 6 added to the rail and a Provision screen
built (`/api/provision`, `/plan`, `/deploy`, `/status`, `/verify`,
`/api/killswitch`). Tested against the live deploy started from the CLI: status
showed `CREATE_IN_PROGRESS` and the acknowledgement, the kill switch listed the
stack, instance and bucket as ours, the deploy form hid itself because the stack
exists. Deploy now also refuses when the stack already exists. The console
deliberately was *not* driven through Discover and Assess for this test: that
would have written a new collector run's records to disk while a deploy built
from the previous run's records was still in flight.

**2026-09-11 — deploy and verify built, not run.** `provision/deploy.py`,
`provision/verify.py`, `provision/selftest.py`. The route past HALT was a
decision by the approver: a provision-only acknowledgement rather than waiving
`DQ-001`, `OPS-001`, `OPS-002` and `RDS-004`, because those findings stand in
front of CDC, cutover and one table's load — not in front of standing up the
target — and waiving them would have recorded those migration risks as
accepted. Self-test 14/14. Real refusal run (correct rate, no acknowledgement):
refused at HALT; kill switch and SSM confirmed nothing was created.

**2026-09-11 — built (render + preflight).** `provision/` created: `policy.py`,
`records.py`, `render.py`, `preflight.py`, `pricing.py`, `run.py`. First run on
`DBMIG_APP` run `6e48d16a`: every AWS check passed, `ValidateTemplate` accepted the
template (deploy needs `CAPABILITY_NAMED_IAM`), 14 Phase 4 artefacts handed on to
Phases 7–8. Found on the way: RDS offers no 21c EE in `ap-south-1` (downgrade);
the upstream records were mixed and had to be regenerated; RDS Custom shares a
price with RDS and had to be excluded by product. Kill switch extended to S3 so
it can tear this template down.
