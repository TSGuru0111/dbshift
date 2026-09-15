# DBShift AI — Project Context

> This file is read automatically by Claude Code at the start of every session.
> Keep it short and current. Detail lives in `docs/`; this is the map.

## What this project is

An AWS-native, human-in-the-loop AI agent that migrates an on-premises Oracle
database to Amazon RDS for Oracle. Built as a company accelerator — the
deliverable is a demonstrable capability, not a one-off migration.

**Scope is deliberately narrow.** One source engine, **two targets**, ten phases.

| Path | Target | State |
|---|---|---|
| Homogeneous | Amazon RDS for **Oracle** | Built, proven, cut over 2026-09-12 |
| Heterogeneous | Amazon RDS for **PostgreSQL** | **Phases 1–10 complete, 2026-09-14.** Run end to end on `DBMIG_APP`: every phase produced its record, console drive 25/25. Nothing that bills was executed — Phase 6 renders, Phase 7 plans |

**The PostgreSQL path, end to end.** Target chosen from evidence (3) → PL/SQL
converted and compiled (4b) → schema DDL generated and compiled (4c) → converted
code applied for real (4b apply) → gate (5) → RDS for PostgreSQL rendered (6) →
DMS planned with its residue routed rule/model/person (7) → cross-engine
validation (8) → cutover with a measured CDC lag requirement (9) → report that
names the right target (10).

**Decided 2026-09-14:** the client chooses the target in Phase 3, from evidence.
This reversed the earlier "capability, not a target" position on PostgreSQL.

**Aurora is not RDS for PostgreSQL** — different products, and Aurora stays out
of scope. Also out: Redshift (analytical only, never for OLTP), Oracle
Database@AWS, and SQL Server as a source. See `docs/02-architecture.md`, and do
not re-add them.

## Current state

| Item | Status |
|---|---|
| Source Oracle estate | ✅ Built, 1.03 GB, 90 objects, 51 constraints, verified |
| **Second estate** | ✅ `DBMIG_TELCO`, **5.67 GB**, 32.7M rows, 68 objects, 14 defects. Telecom billing, own bigfile tablespace. `docs/12-telco-estate.md` |
| Seeded defects | ✅ 8 defects in place, documented |
| Golden snapshot | ✅ `data/dbmig_golden.dmp` |
| Discovery collector | ✅ `collector/`, 47 datasets, local JSON, 5/5 verify checks |
| Assessment engine | ✅ `assess/`, 50 rules as data, **7/7 on DBMIG_APP, 14/14 on DBMIG_TELCO**, HTML report |
| **Target & Sizing (3)** | ✅ `sizing/`, **two paths since 2026-09-14**. `target.py` separates blockers from effort and refuses a blocked path; PostgreSQL has no edition or licence arithmetic. On `DBMIG_APP`: both paths open, PostgreSQL 8 effort points, 86% of stored code automatic. Self-test 41/41 |
| AWS account | ✅ Granted 2026-09-09, SSO + `DBA_permissions`. See `docs/05-aws-services.md` |
| Bedrock access | ⛔ **Blocked** — re-tested 2026-09-12: every invoke now fails with `INVALID_PAYMENT_INSTRUMENT` (the account has no valid payment method for the Marketplace subscription). A Billing-console fix by an admin, not IAM. `docs/05-aws-services.md`; the enablement plan is `docs/14-bedrock-enablement.md` |
| Rehearsal copy | ✅ `DBMIG_REHEARSAL`, 85/90 objects, same XE instance. `scripts/oracle-source/06_*` |
| Detect & Remediate | 🟡 `remediate/`, all 5 gates live. 2 fixes proven apply+rollback on the copy; the other 61 need Bedrock |
| **Apply converted code** | ✅ `convert/apply.py`, built 2026-09-14. Creates APPROVED objects on PostgreSQL in one transaction, all or nothing, recorded against a person. **All 6 objects applied for real.** Self-test 35/35. The only writer in `convert/` |
| **Convert PL/SQL (4b)** | ✅ `convert/`, built 2026-09-12. PL/SQL → PL/pgSQL, 5 gates, **compiled for real on PostgreSQL 16 in Docker and rolled back**. 6/6 convertible objects ready on both estates, model tier needed by 0. `docs/phases/phase-04b-convert.md` |
| **Schema DDL (4c)** | ✅ `convert/ddl.py`, built 2026-09-14. Tables, keys, checks and indexes for a PostgreSQL target, from discovery. **Every statement run on PostgreSQL 16 and rolled back**: 30/30 on DBMIG_APP, 52/52 on DBMIG_TELCO. Self-test 39/39. `docs/phases/phase-04c-schema-ddl.md` |
| Provision (6) | ✅ **Both engines since 2026-09-14.** `dbshift-target-dbmig-app` deployed and verified 2026-09-11 (RDS Oracle 19c EE). The PostgreSQL path renders as `dbshift-target-<estate>-pg` and was validated against the live account on 2026-09-14 (PostgreSQL 16.9, template accepted, every check PASS) — **not deployed** |
| Migrate (7) | ✅ **Two engines. Run against AWS 2026-09-14:** replication instance and both endpoints created for real; **target endpoint connected**, source refused with `ORA-12170` because AWS cannot reach an on-premises listener without a VPN — which is the network requirement Phase 1 states. Two bugs found and fixed (endpoint SSL mode, security group port). All DMS resources deleted. |
| Migrate (7) detail | Oracle: Data Pump full load run 2026-09-11, 11/11 tables match. PostgreSQL: `dms/` built 2026-09-14 — the **residue** (sequences by rule, views by model, external tables by a person). Self-test 85/85. **No data migrated**: the source is unreachable from AWS. `docs/phases/phase-07b-dms.md` |
| Validate (8) | ✅ **Both engines.** Oracle: `validated` 2026-09-12, 5 levels, 5.4M rows a side, 0 mismatches. PostgreSQL: `validate/crossengine.py` built 2026-09-14 — a canonical text form on both sides, same MD5, **proven against the real PostgreSQL**. Self-test 32/32. Levels 2 and 5 report not-comparable by design |
| Cutover (9) | ✅ **Cut over 2026-09-12** — records re-run for the target's run `6e48d16a`, OPS-001/OPS-002 accepted by `guru.ts@ganitinc.com`, 1 step applied, 0 failed. Applications are not repointed; the source stays authoritative |
| Kill switch | ✅ `killswitch/` — stop, empty and destroy every `dbshift-*` resource. **The instance was stopped again after the cutover; RDS auto-restarts a stopped instance after 7 days** |
| **Report (10)** | ✅ `report/`, built 2026-09-12. The "DMS and SCT report": SCT-style conversion assessment + DMS pre-migration assessment from records. Console stage **10 · Report** (rail, unlocked by the assessment) plus the header **Report ↗** link. `docs/phases/phase-10-report.md` |

## The AWS account, and the tag every resource must carry

**Account 280646578374**, profile `dbshift-bedrock`, region ap-south-1. The
earlier account (106325261146) is being decommissioned.

**This account is shared.** A read-only inventory on 2026-09-14 found **19
resources belonging to other teams** — Databricks storage, Elasticsearch,
customer buckets. The kill switch reports them and acts on none: "ours" means a
name starting `dbshift` or a `project=dbshift` tag, and that filter is the only
thing protecting them, because the permission set does not.

**Every resource this project creates carries `Purpose=DMA`.** It is set once,
in `provision.policy.REQUIRED_TAGS`, and applied through `policy.as_tag_list()`
by every path that creates something: the CloudFormation stack, each resource
in the template, the SSM parameter holding the master password, and every DMS
resource. Overridable with `DBSHIFT_TAGS="Key=Value,Key=Value"`.

Verified on the rendered plan: instance, security group, subnet group, bucket
and IAM role all carry it, as does the stack itself.

```powershell
$env:AWS_PROFILE          = 'dbshift-bedrock'   # the migration account
$env:DBSHIFT_AWS_PROFILE  = 'dbshift-bedrock'
$env:DBSHIFT_BEDROCK_PROFILE = 'dbshift-bedrock'  # only if the model tier is elsewhere
```

**The model tier is live.** Both tiers are bound to
`global.anthropic.claude-sonnet-4-6`, verified by real invocation.

**Catalogue presence is not access, and that distinction cost an evening.**
`global.anthropic.claude-haiku-4-5-*` and `global.anthropic.claude-sonnet-4-5-*`
are both listed **ACTIVE** as inference profiles in ap-south-1 and both refuse
to invoke with `INVALID_PAYMENT_INSTRUMENT`. Earlier runs looked like "Bedrock is
blocked" because `bedrock.verify` tests every tier and the fast one was denied
while Sonnet worked. **Bind a model only after it has answered.**

No Haiku anywhere: both tiers run Sonnet 4.6.

```powershell
.\scripts\demo-postgres\run_demo_live.ps1      # phases 1-10, model tier live
```

Phase 0 of that script verifies the model invokes *before* anything else, so a
live run cannot silently fall back at each phase and claim more than it did.

**What the model does, and what still decides:**

| Phase | The model proposes | What decides |
|---|---|---|
| 3 Sizing | edition, instance class, storage | `sizing/validate.py` recomputes every value and records each disagreement as an OVERRIDE |
| 4 Remediate | fix SQL and rollback SQL | the same five gates a template passes |
| 4b Convert | PL/SQL the deterministic rules decline | the same five gates, including a real compile |

**Proven on a live run, 2026-09-14** — 67 findings, 13 model-authored fixes:
**6 REJECTED outright, 7 BLOCKED** pending a rehearsal database and a named
approver. **Not one was auto-accepted.** On the Oracle path the model reasoned
*better* than the heuristic about which features force Enterprise Edition, so the
`edition_rationale` check now passes where it previously needed an override.

## The PostgreSQL demo

```powershell
$env:DBSHIFT_COLLECTOR_PASSWORD = '...'
.\scripts\demo-postgres\run_demo.ps1                       # phases 1-10, in demo order
.\scripts\demo-postgres\run_demo.ps1 -SkipDiscover         # reuse the last collector run
.\scripts\demo-postgres\run_demo.ps1 -PriceFile <offer>    # ...and price the target
```

Runs every phase that does not bill and **leaves the target populated**: 9
tables, 18 constraints, 12 indexes, 11 types and the 6 applied objects. Phase
4b runs before Phase 3 on purpose, because the target decision is only honest
once the stored code has actually been converted and compiled.

Phases 6 and 7 render and plan only. Phases 8 and 9 need a provisioned target
and migrated data; the price file for a real deploy is the public AWS offer
file, not the pricing API, which this permission set cannot call.

## Running it

```
$env:DBSHIFT_COLLECTOR_PASSWORD='...'      # read-only dbmig_collector
.\.venv\Scripts\python.exe -m collector.run       # discover  -> collector/output/<run_id>/
.\.venv\Scripts\python.exe -m collector.verify    # reconcile against ground truth
.\.venv\Scripts\python.exe -m assess.run          # assess    -> assess/output/assessment.json
.\.venv\Scripts\python.exe -m assess.report       # render    -> assess/output/report.html
.\.venv\Scripts\python.exe -m sizing.run          # target+sizing -> sizing/output/sizing.json
#   --engine postgresql --conversion convert/output/conversion_plan.json   # the heterogeneous path
.\scripts\postgres-target\run_pg.ps1              # local PostgreSQL 16 (Docker) for Phase 4b
.\.venv\Scripts\python.exe -m convert.run         # PL/SQL -> PL/pgSQL -> convert/output/conversion_plan.json
.\.venv\Scripts\python.exe -m report.run          # SCT/DMS-shaped report -> report/output/migration_report.html
```

## The console

```
.\.venv\Scripts\python.exe -m web.server          # http://127.0.0.1:8765
```

Connect, then **Phases 1–10** gated in order — Discover, Assess, Target & Sizing,
Remediate, **Convert PL/SQL (4b, optional)**, Blocker gate, Provision, Migrate,
Validate, Cutover, Report — with a stage rail across the top that advances as
each finishes, and Next/Back navigation beneath each screen.

**Browser testing:** `.mcp.json` registers the Playwright MCP server
(`@playwright/mcp`, Edge), and `scripts/console-test/` holds the same drive as
a script. A drive of the console through Phases 1–4b and 10
in headless Edge (connect, discover, assess, size, register PostgreSQL,
convert, build the report, reload, phone width) passed 21/21 on 2026-09-12; it
found two real bugs on the way — see the 4b and 10 change logs.

**The rail is numbered by architecture phase, not by screen order.** Connect is a
prerequisite, not a phase, so it carries no number. Numbering it would shift every
phase by one and contradict `docs/02-architecture.md` in front of a client. The browser never talks to
Oracle — the server does, calling the same `collector` and `assess` modules the
CLI uses, so the UI cannot show a result the CLI would not.

- **Connect** runs a six-check preflight (reachability, auth, container,
  catalogue access, row-data access, CDC readiness) and states what a client
  network would need. The password lives in process memory only.
- **Discovery** streams probe-by-probe progress over SSE, then shows a summary
  and every dataset in an expandable list.
- **Assessment** streams rule-by-rule progress, then scores and grouped issues.
- **Rules &amp; probes** switches any check off, or adds a custom one. Custom
  probes are a `SELECT` against the data dictionary; custom rules are a `SELECT`
  against the loaded discovery data. Read-only is enforced in `web/settings.py`
  as well as by the database account.

Toggles and custom checks live in `web/settings.json` (gitignored, per-machine).
**The CLI deliberately ignores them** and always runs the shipped catalogue.

Both output directories are gitignored. **No AWS is required for Discover or
Assess** — SQLite stands in for Aurora and the rules are plain SQL, so they port
to the metadata repository later with a dialect change. Bedrock does not enter
until Phase 4 remediation.

## Both estates, model tier live — 2026-09-14

The whole PostgreSQL path ran on **both** estates with Bedrock live and **no code
changes between them**, driven only by `DBSHIFT_SCHEMAS` and friends:

| | DBMIG_APP | DBMIG_TELCO |
|---|---|---|
| Discovery verify | 5/5 | 5/5 |
| Schema DDL compiled | 30 statements, 0 failed | 52 statements, 0 failed |
| Target built | 9 tables, 18 constraints | 11 tables, 31 constraints |
| Converted code applied | 6 objects | 6 objects |
| Gate | HALT | HALT |
| Phase 7 preflight | blocked by RDS-004 | **ready** |

**Running the second estate found a real bug in `collector.verify`.** It compared
the two most recent runs *whatever estate they collected*, so a telco run judged
against a DBMIG_APP one reported every difference as drift — 2/5, looking like a
broken collector when it was two different databases. It now picks the two most
recent runs **of the same estate**, and the documented object counts are
reported as ground truth for DBMIG_APP only, overridable per estate.

Converted telco code was called on the target with the schema deliberately off
the caller's `search_path`: `fn_subscriber_balance` and
`pkg_billing_ops$period_revenue` both returned. A converted **foreign key**
rejected an invalid insert during the test, which is Phase 4c's referential
integrity enforcing itself.

## The phases are portable — proven, not assumed

Every local phase — 1, 2, 3, 4, **4b**, 5 and **10** — ran end to end against
`DBMIG_TELCO` **with no code changes**, driven only by environment variables
(`scripts/telco-source/run_phases.ps1`; last run 2026-09-12, see
`docs/12-telco-estate.md`). Result: verify 5/5, assessment 14/14 recall with
severity exact on all 14, sizing EE BYOL with a rule override, 6 of 7 PL/SQL
objects compiled on PostgreSQL, gate HALT with full load clear, report written.
Six rules fired for the first time ever on this estate, because no `DBMIG_APP`
defect had ever exercised them. Phases 6–9 are not in the script: they need AWS
credentials and would bill a second target.

**Getting there found four real bugs that one estate could never reveal.** Two
in the collector, two in assess — see the change logs in `docs/phases/`. The
pattern in all four: behaviour that is correct on a database where every dataset
is non-empty and every table is granted, and wrong anywhere else.

**Re-run it after any change to `collector/` or `assess/`.** A second estate is
the only thing that catches this class of bug, and `DBMIG_APP` regression
(still 7/7) is not a substitute.

## Two things that bite

- **`SELECT_CATALOG_ROLE` is not data access.** Row-level profiling needs the
  explicit grants in `scripts/oracle-source/05_grant_collector_read.sql`.
- **Seeded defect 7 is not in the database** — the seed script is broken. See
  `docs/04-defects.md`. Max recall is 7/8 until it is re-seeded.

## Where things are

- `docs/` — all reference material, numbered in reading order
- `scripts/oracle-source/` — the SQL that builds the source estate
- `data/` — golden dump (gitignored if large)
- `collector/` `assess/` `sizing/` `remediate/` `convert/` `blocker/` `provision/` `migrate/` `validate/` `cutover/` `report/` — one package per phase, all built
- `scripts/postgres-target/` — the Docker PostgreSQL that Phase 4b compiles against
- `killswitch/` — tears down every `dbshift-*` AWS resource
- `web/` — the console (`server.py` + one-page `static/index.html`)
- `bedrock/` — model seam; `static/` holds the labelled stand-in output used while invoke is blocked
- `docs/13-demo-script.md` — **the talk track for presenting this to a client**

## Phase documentation — read before changing, update after

`docs/phases/` holds one file per phase explaining **what actually happens** when
it runs, plus the decisions behind it and a dated change log.

**Before changing a phase, read its file. After changing a phase, update the
`Latest update` block and add a `Change log` entry — and if behaviour changed,
`What actually happens` too.**

This is not bookkeeping. Several decisions in this codebase look arbitrary until
you know what went wrong the first time: the silently-failing seed script, the
leaked SQLite handle that only bites a long-running process, the hardcoded schema
list that made the console collect nothing on anyone else's database. Re-deriving
those costs more than reading. A file one change out of date is worse than none,
because it will be trusted.

## Working agreements

- **Read `docs/04-defects.md` before touching assessment logic.** It is the
  answer key: 8 known defects with expected severities. Any assessment work is
  measured against it.
- **Don't widen scope.** If a change implies a second migration target, a new
  AWS service, or a new phase, flag it rather than building it.
- **Verify, don't assume.** This project has already lost time to silent
  failures (partial script runs, wrong container, disabled substitution
  variables). Prefer a check query over an assumption.
- Costs matter — this runs on ~$100 of AWS credit. See `docs/06-cost-model.md`
  before provisioning anything.
