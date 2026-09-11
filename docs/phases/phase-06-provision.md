# Phase 6 — Provision

> **Latest update — 2026-09-11. Built: render + read-only preflight. Nothing is
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

- **Deploy is not built.** Rendering and checking only.
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
```

To remove anything this project created: `python -m killswitch --destroy --confirm <account-id>`
(see `docs/06-cost-model.md`). It empties the exchange bucket before deleting
the stack, because a stack cannot delete a bucket that holds objects.

## Change log

**2026-09-11 — built (render + preflight).** `provision/` created: `policy.py`,
`records.py`, `render.py`, `preflight.py`, `pricing.py`, `run.py`. First run on
`DBMIG_APP` run `6e48d16a`: every AWS check passed, `ValidateTemplate` accepted the
template (deploy needs `CAPABILITY_NAMED_IAM`), 14 Phase 4 artefacts handed on to
Phases 7–8. Found on the way: RDS offers no 21c EE in `ap-south-1` (downgrade);
the upstream records were mixed and had to be regenerated; RDS Custom shares a
price with RDS and had to be excluded by product. Kill switch extended to S3 so
it can tear this template down.
