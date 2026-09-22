# Phase 6 — Provision

> **Latest update — 2026-09-21: the template downloads from the Plan pane.**
> **Download .json** sends the rendered CloudFormation as a file. It prefers
> what the live stack ran and falls back to the local render when no stack
> exists — which is the ordinary case both *before* a deploy and *after* a
> destroy, and exactly when someone wants to read it. The response header
> `X-Dbshift-Template-Origin` says which, and the console repeats it under the
> button, so a downloaded file is never ambiguous about whether it was live.
>
> **The `dbshift-target-dbmig-telco-pg` stack was destroyed on 2026-09-21** —
> instance, security group, subnet group, empty exchange bucket, IAM role and
> the one automated snapshot, all five resources, `DELETE_COMPLETE`, nothing
> billing. It had run 4.8 hours on 09-18 (~$0.51) and been stopped since; it
> was deleted to test a clean Phase 6 deploy from the console. The render is
> still on disk and the plan still says `ready`.
>
> Earlier — **the phase reads AWS SCT's records.** With the button unlocked, the render then refused: *records come
> from different collector runs*. The check was right and was reading the wrong
> three files. A run assessed, remediated and gated entirely through SCT leaves
> `assessment.json`, `remediation_plan.json` and `gate_decision.json` untouched
> — they are the **50-rule** path's files — so Phase 6 compared today's sizing
> against whatever that path had written last. On the reported run: sizing from
> `c1f08513` (today), the other three from `a62c7f5f` (three days earlier).
>
> `provision/records.py` now prefers `sct_gate_decision.json` and
> `sct_remediation_plan.json` where they exist, falls back to the 50-rule files
> where they do not, and **derives the assessment from the SCT gate** rather
> than reading a fourth file — there is no fixed-path SCT assessment (it is
> per-estate, per-target, and records the run rather than the findings), and one
> source means the two cannot drift. **The consistency rule itself is
> unchanged**: a genuine mismatch is still a failure, and the new
> `provision.selftest_records` asserts that with an SCT gate from another run.
> Two smaller fixes fell out: `sct_plan.build` never stamped a
> `collector_run_id`, so its own record could not identify its estate; and
> `preflight.gate_allows` read `gate['critical_findings']`, which an SCT gate
> does not carry — a `KeyError` in the branch whose job is to report a HALT
> clearly.
>
> **Rendered for real afterwards**: `dbshift-target-dbmig-telco-pg`, postgres
> **16.9**, `db.t3.medium`, 20 GB gp3, **11 preflight checks pass, 0 fail**,
> CloudFormation accepted the template, $0.106/hour. `provision.selftest_records`
> **16/16**; every other suite unchanged (provision 50/50 + 49/49, remediate
> 102/102, blocker 70/70, dms 113/113, cutover 51/51, sct 279/279).
>
> Earlier — **the SCT gate never unlocked this phase.** The gate said
> **PROCEED**, the rail lit Provision green, and
> **Render and check stayed disabled** — because lighting the rail and enabling
> the button were separate acts and only the old 50-rule path did both. The SCT
> gate has been the one the console leads with since 2026-09-17. Both gates now
> call one `openProvision()`; a *second* evaluation unlocks too (the old guard
> required `stageState.provision === 'idle'`); and a reload restores the SCT
> gate from `has_sct_gate`, which the server had always exposed and the console
> never read. Restoring it on load then exposed a real overlap on the gate
> screen that an empty panel had hidden — Phase 4c's `.scrollcap` fault exactly.
> `check_overlap.js` is now **17/17**, above the old 16/17 baseline.
>
> Earlier the same day — **the screen says where its actions are.** Three
> console faults, none in `provision/`, all found by someone who could not find
> the deploy button and reasonably concluded the CloudFormation template did not
> exist. It did — `provision/output/` is gitignored because templates are
> *generated*, so they appear only after **Render and check**, and the console
> deliberately reads the template back from the live stack rather than from that
> file. The button is disabled until Phase 5 clears provisioning and **never said
> so**; the deploy form sits behind the third of three identical-looking chips;
> and none of the chips reported state. The button now gives its reason, the
> empty state names the button and links to Phase 5, `Deploy` is marked as the
> billing action, and each chip carries its own state — `Plan · ready`,
> `Live · nothing yet`, `Deploy · $0.106/h`. `check_overlap.js` 16/17 at both
> 1440×900 and 1280×720, the baseline.
>
> Earlier — **2026-09-16.** **A person can choose the instance class and
> fill in the database configuration**, and the plan still says where every
> value came from. The instance class was taken straight from Phase 3 with no
> way to depart from it, and nine configuration values were module constants in
> `policy.py` — right for a beta on $100 of credit, wrong for a client whose
> standards differ.
>
> An override is not a free-form edit: it is a claim that the evidence was
> wrong, so it is recorded as one. **The derived value is never discarded** — it
> stays in the provenance row next to what was chosen and why, which is the only
> way the screen's promise survives contact with a human. A change needs a
> reason of real length; cost-affecting fields say so at the field; and four
> properties refuse to be overridden at all, each with the reason
> (`engine_version` would invalidate the Phase 4b compile gate;
> `character_set` cannot be altered after creation).
> `provision/overrides.py`, self-test 49/49, browser drive 21/21.
>
> Earlier — **2026-09-14 (two engines).** Provision follows the Phase 3
> target decision: **RDS for Oracle** or **RDS for PostgreSQL**. The renderer
> branches once, on `sizing.decision.engine`, and the PostgreSQL path drops
> everything Oracle-specific — no option group, no `S3_INTEGRATION` role
> association, no `CharacterSetName`, no edition-to-engine mapping, port 5432
> and a lower-case database name. **Stack names now carry the engine**
> (`dbshift-target-<estate>-pg`), because without it a second deploy would
> silently UPDATE the first and replace an Oracle instance with a PostgreSQL
> one. Rendered and validated against the live account on 2026-09-14:
> CloudFormation accepted the template, PostgreSQL **16.9** orderable for
> `db.t3.medium` `gp3`, every preflight check PASS. **Nothing was deployed.**
> The bundled price list covers Oracle only, so the PostgreSQL estimate is
> withheld rather than read from the wrong engine's SKUs.
>
> Earlier — **2026-09-11 (deployed and verified).** The approved deploy ran:
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

**2026-09-21 — the template downloads, and says which one it is.**
`GET /api/provision/template/download` sends the rendered CloudFormation as a
`.json` attachment, with a **Download .json** button on the Plan pane beside the
new `CloudFormation template` heading.

Until now the template could only be read inside a `<details>` on the **Live**
pane, via `/api/provision/template` — which reads it back from the live stack
and therefore exists only while a stack does. That is exactly backwards for the
two moments someone wants to read it: **before** a deploy, to review what will
be created, and **after** a destroy, to hand it to someone. So the download
prefers what CloudFormation actually ran and **falls back to the local render**
when no stack exists.

The response says which, in `X-Dbshift-Template-Origin` (`deployed` or
`render`), and the console reports it under the button — "the template
CloudFormation actually ran" versus "the local render. No stack exists, so
there is nothing deployed to read it back from." A file called
`<stack>.template.json` with no provenance is the ambiguity the rest of this
screen exists to avoid. That header is why the button **fetches** rather than
being an `<a download>`: a plain link cannot read it.

Verified both ways on 2026-09-21 against the real account: `deployed` while the
stack was still being deleted, `render` once it was gone, valid JSON carrying
all five resources each time.

**2026-09-21 (later still) — the four records are SCT's where SCT ran.**
Reported from a live run: the gate said PROCEED, the button was unlocked, and
the render then refused with `records come from different collector runs`.
Phase 6 reads four records; three of them had two implementations, and it was
reading the wrong one.

| Record | Phase 6 read | The SCT run wrote |
|---|---|---|
| assessment | `assess/output/assessment.json` | *(no fixed-path file — see below)* |
| sizing | `sizing/output/sizing.json` | the same file |
| remediation | `remediate/output/remediation_plan.json` | `sct_remediation_plan.json` |
| gate | `blocker/output/gate_decision.json` | `sct_gate_decision.json` |

`SCT_PATHS` holds the two SCT files, preferred where they exist; the 50-rule
files stay the fallback, and `load(prefer_sct=False)` forces them.

**The assessment is derived from the SCT gate, not read from a file.** There is
no fixed-path SCT equivalent: `sct/output/<estate>-<target>/sct_assessment.json`
is per estate and per target, and it records the *run* — host, exit code,
artefacts — not the findings. The findings are on the gate, already routed. Only
what downstream reads is synthesised: `collector_run_id`, and `findings`
carrying `owner` (for `estate_of`) and `rule_id`. An SCT item is keyed by
`issue_code`, so that is what `rule_id` carries — SCT's `5984` where the rules
engine had `RDS-004`. The Oracle-only branches in `render.py` that test for
specific `RDS-*` ids therefore do not match, **which is correct**: those are
Data Pump and option-group concerns that do not arise on the PostgreSQL path SCT
is assessing.

**The consistency rule is untouched.** It compares the same way; it now compares
the right files. `selftest_records` asserts a mismatch still fails, using an SCT
gate from a second run — the check exists because on 2026-09-11 the records
disagreed twice for real, and loosening it would throw that away.

Two faults fell out of fixing it:

- **`sct_plan.build` stamped no `collector_run_id`**, so its own record could
  not say which estate it was about — a plan carrying `None` read as a mismatch
  against three records that agreed. It takes it from the assessment it is
  passed; the console passes `STATE.run_id`.
- **`preflight.gate_allows` read `gate['critical_findings']`**, which the SCT
  gate does not carry — a `KeyError` in the one branch whose job is to report a
  HALT clearly. It falls back to `len(blockers)`, and the 50-rule gate still
  reports its own count.

`_source` on the returned records says which files were read, and the passing
detail line ends `(via AWS SCT: gate, remediation)` — the screen should never
have to be asked which assessment it judged. Keys beginning `_` are metadata and
are excluded from the comparison.

Rendered for real afterwards against the live account: 11 preflight checks pass,
0 fail, CloudFormation accepted the template. `provision.selftest_records`
**16/16**.

**2026-09-21 (later) — the SCT gate never unlocked Phase 6.** The gate returned
**PROCEED**, the rail lit Provision green, and **Render and check stayed
disabled**, with no way forward except re-running a gate that had already
passed. Reported from a live run: `has_gate: false`, `has_sct_gate: true`.

Three faults, one cause — *marking a stage active* and *unlocking its control*
were separate acts, and only one path did both:

- **The SCT gate lit the rail and stopped there.** It called
  `setStage('provision', 'active')`; `$('#btnProvision').disabled = false` lived
  only in `renderGate()`, the 50-rule path. Since 2026-09-17 the console leads
  with the SCT gate, so the button had no writer on the path people actually
  take. Both gates now call one `openProvision()`.
- **A second evaluation skipped the unlock.** The condition was
  `verdict !== 'HALT' && stageState.provision === 'idle'`, so re-running the
  gate — the ordinary case after fixing something — did nothing, because the
  stage was no longer idle. The verdict is the whole condition now; the idle
  check stays inside `openProvision()`, on the rail state alone.
- **A reload lost the gate entirely.** `restore()` read `has_gate` and never
  `has_sct_gate`, which the server had exposed all along, so refreshing the
  page after a PROCEED locked Phase 6 again. It re-evaluates
  `GET /api/sct/gate` for the assessed target — deterministic over records
  already held, and it creates nothing.

**And restoring the gate on load exposed a layout fault that an empty panel had
been hiding**, the same one Phase 4c had on 2026-09-20: `#sctGateGroups` was a
`.scrollcap` while the CDC panel, the downstream-phase list and the 50-rule
`<details>` below it were not, so those took the height, the cap collapsed to
its floor with 343px of content in 110px, and the phase list ran 27px past the
bottom of the view and painted over the `<details>` summary. The region scrolls
now and the lists inside it do not. `check_overlap.js` **17/17** at 1440×900
and 1280×720 — above the 16/17 that had been the baseline.

**2026-09-21 — the screen says where the phase's actions are.** Nothing in
`provision/` changed; all three faults were the console hiding its own
controls, and all three were found by a person who could not find the deploy
button and assumed the template did not exist.

- **A disabled button with no reason.** `#btnProvision` ships disabled and is
  enabled only where the gate reports `by_phase.provision === 'clear'`. Neither
  the button nor the empty state below it said so: the empty state read
  "Evaluate the gate first, then render the target", which names a step but not
  the control it unlocks and not where that step lives. The button now carries
  the reason as its `title`, the empty state names **Render and check** and
  states that rendering creates nothing, and it offers a button through to
  Phase 5. The gate's unlock rewrites both, so an enabled button never keeps a
  title claiming it is blocked.
- **The deploy form was behind a chip that looked like the other two.** The
  three panes are a switcher with `Plan` pre-selected, and `Deploy` — the one
  action in this phase that bills — rendered identical to the two free panes.
  It is marked `.chip.act` in `--bad` now, so the billing action is visible
  without opening anything.
- **The chips said nothing about state.** `provChipState()` writes each pane's
  state onto its own chip: `Plan · ready`, `Live · nothing yet`,
  `Deploy · $0.106/h` — and `Deploy · deployed` once a stack exists. The rate
  is the fact a person needs *before* opening the pane, not after. It is
  written from `refreshStatus`, the one place holding both the rendered plan
  (the rate) and the live status (whether anything is deployed), so the two
  halves cannot disagree.

The separator is a real `' · '` in `textContent`, not the `margin-left` it
started as: the margin spaced the words on screen while `innerText` still read
`Plan· ready` as one token, to a screen reader and to the drivers alike.
`check_overlap.js` **16/17** at 1440×900 and at 1280×720 — the baseline, with
the one failure the pre-existing 400 rather than anything from this change.

**2026-09-16** — **Manual instance choice and a database configuration form.**
Client feedback: "Provisioning -- give option to manual inputs to choose
instance" and "default configuration for the Database to be filled by user while
configuring." Both were gaps: `render.py` read `instance_class` from the Phase 3
decision with no override, and `policy.py` held nine values as module constants
a client could not reach.

`provision/overrides.py` added. The design question was not how to accept input
-- it was how to accept it **without turning the provenance table into
decoration**. The answer runs through every part of it:

- **13 instance classes as a picker, not free text.** A typo like
  `db.t3.medum` would otherwise fail minutes later, inside CloudFormation, after
  a stack had begun. The existing `preflight.aws_checks` still confirms the
  chosen class is orderable in the region, so an entry AWS will not sell is
  caught before anything is created.
- **The derived value survives.** The provenance row for an overridden class
  reads "a person, overriding sizing.decision.instance_class (Phase 3)" and its
  *why* carries what Phase 3 derived, what was chosen, and the reason given.
  "Manual" alone would have been a worse answer than the one it replaced.
- **A reason is required** (8 characters minimum). An override without one is
  indistinguishable from a mis-click three months later.
- **Only what differs is an override.** The form posts all nine fields; posting
  nine defaults records nothing. Without this a person who changed one setting
  would have found nine override rows in the plan.
- **Cost-affecting fields are marked at the field** — Multi-AZ doubles the
  instance bill, backup retention and Performance Insights add to it.
- **Four properties are locked, each with its reason.** `engine_version`
  (Phase 4b compiled against it), `character_set` (cannot be altered after
  creation, and Phase 8 compares against the source), `engine` (that is a
  different migration) and `region` (everything else is in one region).
- **`policy` is never mutated.** `effective_policy()` returns a dict, because
  `policy` is module state shared with the CLI and the kill switch -- a console
  override that leaked into a later CLI run in the same process would be a
  genuinely nasty bug.

The chosen class drives the **render, the preflight and the price lookup**
together. Quoting the derived class for an overridden instance would have shown
a client an hourly rate for a machine they were not buying.

Self-test `provision.selftest_overrides` **49/49**; browser drive
`scripts/console-test/drive_provision_options.js` **21/21**, including that an
invalid database name and a missing reason are both refused in the UI with a
message naming the field. `provision.selftest` still passes unchanged.

**2026-09-14 — RDS for PostgreSQL as a second target.** `provision/policy.py`
gained the PostgreSQL block (engine, licence `postgresql-license`, major 16 to
match the Phase 4b compile gate, port 5432, `dbshiftadm`, lower-case `dbshift`
database, UTF8). `render.render` branches on `sizing.decision.engine`; the
option group, S3 integration role association and character-set properties are
omitted on PostgreSQL, each with a provenance line saying *why* rather than
just disappearing. `preflight.version_direction` now takes the engine and
returns PASS across engines — Oracle 19c to PostgreSQL 16 is not a version
downgrade, it is a different engine, and Phase 4b's compile is the real check.
`preflight.aws_checks` resolves the orderable version against the right major.

**`stack_name_for` gained the engine.** Both paths can be provisioned for the
same estate, and the previous name would have collided: the second deploy would
have updated the first stack in place, swapping the engine under a running
instance. Oracle keeps the unsuffixed name so the deployed stack still
resolves.

Two smaller fixes found while proving it: `provision.run` mapped edition to
engine unconditionally and would have raised `KeyError` on a null PostgreSQL
edition; and `_report` printed `p['deploy']` unconditionally, so a *refused*
plan died with a traceback that hid the refusal. The kill switch needed no
change — it matches on the `dbshift` name prefix and already scans DMS
replication instances, which matters now that DMS is the data path.
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
