# DBShift AI — Project Context

> This file is read automatically by Claude Code at the start of every session.
> Keep it short and current. Detail lives in `docs/`; this is the map.

## What this project is

An AWS-native, human-in-the-loop AI agent that migrates an on-premises database
to Amazon RDS. Built as a company accelerator — the deliverable is a demonstrable
capability, not a one-off migration.

**Scope — two source engines, ten phases, four legal pairs.** MySQL was added as
a second source on **2026-09-28**; Oracle remains the default and the proven path.
`engines/spec.py` is the only place that knows the pairs, and it refuses the two
combinations this project does not perform (MySQL→RDS Oracle, Oracle→RDS MySQL).

| Source | Target | Kind | State |
|---|---|---|---|
| Oracle | RDS for **Oracle** | homogeneous | Built, proven, cut over 2026-09-12 |
| Oracle | RDS for **PostgreSQL** | heterogeneous | Phases 1–10 complete 2026-09-14 |
| MySQL | RDS for **MySQL** | homogeneous | **Phases 1–8 built 2026-09-30**; 1–5 proven; 6 renders (MySQL 8.4 — 8.0 is past RDS standard support); schema copy + Phase 8 proven on a MySQL 8.4.11 stand-in; not yet run on AWS |
| MySQL | RDS for **PostgreSQL** | heterogeneous | **Phases 1–8 proven for real 2026-09-30** — deployed RDS PostgreSQL 16.15, DMS 19/19 tables (196,544 rows, 0 errors), Phase 8: 19/19 counts, 18/19 identical checksums, 1 decided zero-date loss; torn down. Problems and fixes: `docs/20-mysql-problems-log.md` |

**The MySQL path, as it stands.** `engines/` holds the seam (47/47),
`collector/dialect.py` the connection and bind style, `collector/probes_mysql/`
ten probes over `information_schema` emitting **the same dataset names and, since
2026-09-29, Oracle's column sets** (`conform()` pads from `oracle_columns.json`), so
the `v_user_*` views and every downstream reader need no change
(`collector.selftest_probes_mysql` **105/105**). Results are proven against the EC2
estate below. `docs/19-mysql-source.md` states exactly what is proven and what is not.

**Phases 4, 4b, 4c, 4d and 5 on MySQL (2026-09-29)** — each is the Oracle machinery
with the engine threaded through, never a fork; the gates are unchanged:

- **Engine comes from the run, not the environment.** 4b/4c read the collector
  manifest's `source_engine`; 4d stamps it on each statement; the SCT CLIs take
  `--source-engine` (the "newest record on disk" had become the MySQL one).
- **SCT routing is per source** (`sct/route.py` `MYSQL_ROUTES` overrides `ROUTES`):
  shared codes mean different things — 9994 is AQ on Oracle, an EVENT on MySQL.
  **No MySQL fix is ever gated by Oracle's source allow-list.**
- **The Oracle "SELECT INTO must be STRICT" rule inverts on MySQL** — MySQL code
  tests the variable for NULL after a zero-row SELECT INTO; STRICT would make that
  an exception. Encoded in the MySQL catalogue and prompt.
- **MySQL's catalogue reads differently:** every PK is named `PRIMARY` (FK parents
  come from `referenced_table_name`), nullability is YES/NO, literal defaults are
  unquoted, `DEFAULT_GENERATED` is not a generated column (the collector had that
  wrong too — fixed at source).
- **CDC readiness speaks MySQL** (`collector/mode.CDC_WORDING`): a MySQL source was
  being told to run `ALTER DATABASE ARCHIVELOG`.
- Gate fixes found live: `pg_policy` read "ON UPDATE" in a COMMENT as a data rewrite
  (now exempt, and `UPDATE "quoted"` is now caught); a fix whose schema the target
  lacks yet (3F000) is BLOCKED on Phase 4c, not REJECTED.
- **Known gaps:** 4c renames reserved words (`order` → `order_tbl`) but DMS does not,
  so Phase 7's mapping must mirror them; `/api/cutover` 400s on a MySQL run (it reads
  the owning schema from the 50-rule assessment, which a SCT-only run lacks) —
  both belong to the Phase 6–10 work.

Self-tests: `remediate.selftest_sct_mysql` 101/101, `convert.selftest_mysql` 71/71,
`appsql.selftest_mysql` 50/50; seeded 4d corpus `scripts/demo-app-mysql/`
(answer key verified 34/34).

**The MySQL estate is on EC2 and SCT assesses it there, since 2026-09-29.**
`dbshift-source-mysql` (`i-0bd32544c1577b5ed`, t3.medium, **MySQL 8.0.46**) in the
same VPC/subnet as `dbshift-source-oracle`, security group `sg-0067f7243b4a0dce7`
open on 3306+22 to the operator `/32` only. SCT's own CSV records
`3.108.190.1:3306`, so the artefact itself proves which server it read.
**It bills while running** — stop it when idle. Rebuild and gotchas:
`scripts/mysql-source/EC2-HOST.md`. Two that will bite again:
**MySQL 8.4's packaged `my.cnf` has no `!includedir /etc/my.cnf.d`** (so a config
file can sit unread, and a wrong `dnf config-manager` repo id silently installs
8.4), and **the SCT cache key now includes the host** — the same estate locally
and on EC2 previously shared a directory, so an EC2 run served the container's
report.

**The console is engine-aware since 2026-09-29.** Connect leads with a
source-engine picker (it changes the form, so it is asked before connecting);
`web/preflight_mysql.py` runs the six checks in Oracle's shape; Phase 3 offers only
the legal pairs; two MySQL-only Discover panels show binlog readiness and the
storage-engine/charset audit; 4b/4c/4d state their own inapplicability on a
homogeneous pair; and Phase 7 branches on the **pair** — Data Pump serves only
Oracle→Oracle. `drive_srcpick.js` **28/28**, `check_overlap.js` **17/17 at both
1440x900 and 390x844** (phone width is new).

**The UI work found three server bugs, all now fixed:** `/api/state` 500'd on
MySQL because `sizing_target.LABEL` lacked a `MYSQL` entry; console discovery built
a `Config` without `source_engine` and ran oracledb against MySQL; and
`preflight_mysql` returned the schema list under the wrong key, so the console fell
back to Oracle's default schemas and produced a run that **looked successful while
collecting zero tables**. `docs/19-mysql-source.md` has all three.

**Phase 2 on the MySQL path is real AWS SCT, not the 50-rule engine.** Decided
2026-09-29. `sct/targets.py` is now a matrix keyed on source engine, and every row
carries `sct_conversion` — which Phase 5 must read.

**Three things a real SCT run against MySQL found, none of them guessable:**

1. **MySQL takes no `connectionType`.** Its `MySqlConnectionProperties` has no
   `$ConnectionType` inner class, and passing one makes SCT load *Oracle's*
   property class and fail with `No enum constant
   OracleConnectionProperties.ConnectionType.BASIC`. The visible follow-on error
   is `Not found object(s) for path "Servers.MYSQL"`, which reads like a
   tree-path bug. MySQL takes no `database` either.
2. **SCT demands `SELECT` and `SHOW VIEW` at SERVER scope** (`ON *.*`), not the
   schema scope the collector needs: `MYSQL Server : [SELECT, SHOW VIEW]`. Third
   time this project has paid for a privilege check that presents as something
   else, after `SELECT ANY DICTIONARY` and `SHOW_ROUTINE`.
3. **SCT's own metadata query is invalid under MySQL 8's default `sql_mode`.**
   `load-partitions-by-schema` breaks on `ONLY_FULL_GROUP_BY` and SCT abandons
   the assessment. **A stock MySQL 8 cannot be assessed until it is relaxed** —
   SCT's SQL, not ours, and a real prerequisite for a client's DBA.

`docs/19-mysql-source.md` has the measurements. `sct.selftest_mysql` 64/64.

**MySQL → MySQL has no conversion step, and that is a structural difference, not
an omission.** AWS publishes no MySQL→MySQL conversion path because there is
nothing to convert; SCT produces a same-engine *assessment* only. So Phase 5 must
read "no conversion action items" on that pair as **CLEAR with a reason**, never as
missing evidence — the opposite of the right default everywhere else.

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

**Decided 2026-09-16:** the client also declares in **Phase 1** whether this is a
**full load** or **full load + CDC**. It was a Phase 7 flag, which meant every run
was assessed as though CDC were in scope: `OPS-001` (NOARCHIVELOG), `OPS-002`
(supplemental logging) and `DQ-001` (no primary key) only matter because CDC reads
redo, and all three halted the gate regardless. On `DBMIG_APP` that is 4 CRITICALs
where **only `RDS-004` is real**. The mode lives in the discovery manifest and is
read by Phases 2, 5 and 7. **It never suppresses a finding** — a CDC-only blocker
on a full-load run is still reported, keeps its severity in
`severity_if_applicable`, and is marked not-applicable with the reason.
`collector/mode.py`, self-test `collector.selftest_mode` 55/55.

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
| Discovery collector | ✅ `collector/`, 47 datasets, local JSON, 5/5 verify checks. **Asks the migration mode (full load / +CDC) since 2026-09-16** — `DBSHIFT_MIGRATION_MODE`, `--migration-mode`, or the console. Console tiles count **user objects, not Oracle's internals** (DBMIG_APP: 9 tables, not 21), pinned to Phase 2's `v_user_tables` by `web.selftest_counts` 22/22 |
| Assessment engine | ✅ `assess/`, 50 rules as data, **7/7 on DBMIG_APP, 14/14 on DBMIG_TELCO**, HTML report. CDC-only findings marked not-applicable on a full-load run, never removed. Console shows the issues as a **sortable table** (accordion still there behind *Detail*) and downloads them as **PDF, Excel, CSV or JSON**. PDF/Excel carry the **SCT-shaped** assessment (`report/export.py`, self-test 42/42) — produced by our own 50 rules and **labelled on every page and sheet as not generated by AWS SCT**. Not-applicable findings are exported with their reason, never dropped |
| **Real AWS SCT (2)** | ✅ `sct/`, built 2026-09-17 — drives the **actual SCT 1.0.677** and **has produced real reports**: 33 occurrences → 12 action items on `DBMIG_APP`, 75 → 14 across three schemas. PDF/CSV served **byte-identical** to SCT's own files; Excel wraps SCT's rows unaltered plus a provenance sheet. Console panel on **Phase 2 · Assess**: **target dropdown** (RDS Oracle, RDS PostgreSQL in scope; Aurora PG and Redshift listed for comparison, carrying no engine so they cannot leak into Phase 3), streaming progress, sortable action items. `sct.selftest` **79/79**, `drive_sct.js` in headless Edge. Needs **`SELECT ANY DICTIONARY`** (granted 2026-09-17; `SELECT_CATALOG_ROLE` is not accepted). Host: **local for the demo, EC2 for a customer, never Lambda** — `docs/16-sct-runner.md`. **Does not replace `assess/`**: SCT ignores the OPS/SEC/DQ/PERF findings Phases 3, 5 and 7 read |
| **Target & Sizing (3)** | ✅ `sizing/`, **two paths since 2026-09-14**. `target.py` separates blockers from effort and refuses a blocked path; PostgreSQL has no edition or licence arithmetic. On `DBMIG_APP`: both paths open, PostgreSQL 8 effort points, **46% of stored code automatic since the model-tier seed of 2026-09-17** (6 of 13 convertible by rule; it was 86% of 7 objects before `09_seed_model_tier_plsql.sql` added six model-tier objects). Self-test 41/41 |
| AWS account | ✅ Granted 2026-09-09, SSO + `DBA_permissions`. See `docs/05-aws-services.md` |
| Bedrock access | ✅ **Live since 2026-09-17** — both tiers invoke `global.anthropic.claude-sonnet-4-6` on account 280646578374 (ap-south-1), verified by `python -m bedrock.verify` 2/2 and by a live remediation run drafting fixes for SCT action items. Haiku 4.5 and Sonnet 4.5 still fail `INVALID_PAYMENT_INSTRUMENT` and stay unbound — a Billing fix by an admin, not IAM. Bindings live in `bedrock/models.json` |
| Rehearsal copy | ✅ `DBMIG_REHEARSAL`, 85/90 objects, same XE instance. `scripts/oracle-source/06_*` |
| Detect & Remediate | ✅ `remediate/`, all 5 gates live. **A proven fix is now applied and kept** — `remediate/apply.py`, built 2026-09-16, the only writer in `remediate/`. **2 fixes applied for real** to `DBMIG_REHEARSAL` (index + statistics verified present; `DBMIG_APP` unchanged at 90 objects), 65 held back with reasons. Self-test 41/41. **Writes to the rehearsal copy, never the source** — that is Phase 9's decision |
| **Remediate SCT (4)** | ✅ `remediate/sct_plan.py` + `sct_generate.py` + `pg_policy.py` + `sct_run.py`, built 2026-09-17 — AI drafts fixes for **SCT's** action items, routed by `sct/route.py` into source / target / decision / human. **Two allow-lists, never one:** the Oracle `policy.py` is untouched (it guards a production source) and `pg_policy.py` governs target DDL — asserted by test that neither imports the other. Target fixes are dry-run on the **real PostgreSQL 16** and rolled back; SCT 5639 reaches READY_TO_APPLY with 4/4 gates. **Every target change needs a named approver, including the automatic route.** Self-test `remediate.selftest_sct` **85/85**. Not wired to `apply.py`; no console screen yet |
| **Gate on SCT (5)** | ✅ `blocker/sct_gate.py` + `sct_run.py`, built 2026-09-17 — halts on **SCT's** action items, **split by where the work belongs** (source / target / decision / human). On `DBMIG_APP` + CDC: 12 items, only **2 halt** (`5200` external tables → full load + CDC, `5659` no primary key → CDC) plus **CDC readiness**, which SCT cannot see and comes from the **Connect preflight's** `v$database` reading via `collector.mode.readiness`. Absent evidence reports blocked, never clear. `--compare` shows the rules gate beside it and **refuses to compare across different estates**. Self-test `blocker.selftest_sct` **70/70**. `gate.py` unchanged and still runs |
| **Console, Phases 1-5** | ✅ The SCT path runs end to end in the browser — Connect → Discover → Assess (AWS SCT) → Remediate (AI drafts, gates decide) → Blocker gate, plus reload and phone width. `scripts/console-test/drive_sct_1to5.js` **45/45** in headless Edge. The 50-rule panels are collapsed behind *Technical* disclosures, not deleted — Phases 6, 7, 9 and 10 still read their records. **SCT results cache on (schemas, target), not the collector run id**: keying on the run id made every re-discovery throw away a 25-minute assessment |
| **Apply converted code** | ✅ `convert/apply.py`, built 2026-09-14. Creates APPROVED objects on PostgreSQL in one transaction, all or nothing, recorded against a person. **All 6 objects applied for real.** Self-test 35/35. The only writer in `convert/` |
| **Convert PL/SQL (4b)** | ✅ `convert/`, built 2026-09-12. PL/SQL → PL/pgSQL, 5 gates, **compiled for real on PostgreSQL 16 in Docker and rolled back**. **The model tier is exercised since 2026-09-17**: `09_seed_model_tier_plsql.sql` seeded six `DBMIG_APP` objects whose constructs (`CONNECT BY`, `BULK COLLECT`/`FORALL`, `LISTAGG`, `EXECUTE IMMEDIATE`, `REF CURSOR`, `MERGE INTO`) `constructs.json` already classified `tier=model`, so `DBMIG_APP` now routes 6 ready / 6 model / 1 manual. It was 6/6 by rule with **zero** model objects before that seed — a fact about the estate, not the agent. `DBMIG_TELCO` is unseeded and still routes 6/6 by rule. `docs/phases/phase-04b-convert.md` |
| **Schema DDL (4c)** | ✅ `convert/ddl.py`, built 2026-09-14. Tables, keys, checks and indexes for a PostgreSQL target, from discovery. **Every statement run on PostgreSQL 16 and rolled back**: 30/30 on DBMIG_APP, 52/52 on DBMIG_TELCO. Self-test 39/39. `docs/phases/phase-04c-schema-ddl.md` |
| Provision (6) | ✅ **Both engines since 2026-09-14.** `dbshift-target-dbmig-app` deployed and verified 2026-09-11 (RDS Oracle 19c EE). The PostgreSQL path renders as `dbshift-target-<estate>-pg` and was validated against the live account on 2026-09-14 (PostgreSQL 16.9, template accepted, every check PASS) — **not deployed** |
| Migrate (7) | ✅ **Two engines. Run against AWS 2026-09-14:** replication instance and both endpoints created for real; **target endpoint connected**, source refused with `ORA-12170` because AWS cannot reach an on-premises listener without a VPN — which is the network requirement Phase 1 states. Two bugs found and fixed (endpoint SSL mode, security group port). All DMS resources deleted. |
| Migrate (7) detail | Oracle: Data Pump full load run 2026-09-11, 11/11 tables match. PostgreSQL: `dms/` built 2026-09-14 — the **residue** (sequences by rule, views by model, external tables by a person). Self-test 85/85. **No data migrated**: the source is unreachable from AWS. `docs/phases/phase-07b-dms.md` |
| Validate (8) | ✅ **Both engines.** Oracle: `validated` 2026-09-12, 5 levels, 5.4M rows a side, 0 mismatches. PostgreSQL: `validate/crossengine.py` built 2026-09-14 — a canonical text form on both sides, same MD5, **proven against the real PostgreSQL**. Self-test 32/32. Levels 2 and 5 report not-comparable by design |
| Cutover (9) | ✅ **Cut over 2026-09-12** — records re-run for the target's run `6e48d16a`, OPS-001/OPS-002 accepted by `guru.ts@ganitinc.com`, 1 step applied, 0 failed. Applications are not repointed; the source stays authoritative |
| Kill switch | ✅ `killswitch/` — stop, empty and destroy every `dbshift-*` resource. **Scans EC2 instances since 2026-09-29**: it had no EC2 branch because `RunInstances` was denied when it was written, so the two running source hosts were invisible to the one tool whose job is to say what bills. A stopped instance is reported as not-billable-for-compute *and* told that its EBS volume still bills. Verified against the live account: 2 ours, 4 other teams' — reported, never touched. `_do` also had to learn to dispatch STOP on kind, since it called `rds.stop_db_instance` for any STOP. **The instance was stopped again after the cutover; RDS auto-restarts a stopped instance after 7 days** |
| **Report (10)** | ✅ `report/`, built 2026-09-12. The "DMS and SCT report": SCT-style conversion assessment + DMS pre-migration assessment from records. Console stage **10 · Report** (rail, unlocked by the assessment) plus the header **Report ↗** link. `docs/phases/phase-10-report.md` |

## EC2 is available again — re-measured 2026-09-28

`ec2:RunInstances` was explicitly denied account-wide and **is not any more**.
Re-probed with a real regional AMI (`ami-0ee11497c4eac651d`): `RunInstances`,
`CreateKeyPair`, `CreateSecurityGroup`, `CreateTags` and `AllocateAddress` all
return `DryRunOperation`. `CreateKeyPair` was a hard `Deny` in September.

**An in-VPC source host already runs:** `dbshift-source-oracle`
(`i-07b0d1acfe31edc1a`, t3.large) in default VPC `vpc-05f9bf94bf057b67e`. So
`docs/HANDOFF-2026-09-18-OPTION-A.md` (the router port-forward plan) is superseded
for new work, and `docs/17-source-reachability.md` carries the reversal.

**The trap in that document fired again while measuring this.** AWS validates
arguments before evaluating IAM, so a bad AMI id returns `InvalidAMIID.Malformed`
rather than an authorization result. The SSM public AMI parameter returns
`ParameterNotFound` on this account, and the empty value it yielded produced
exactly that misleading error. **Resolve AMIs with `ec2 describe-images`.**

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
and migrated data. Prices come from the AWS Price List API (`pricing/query.py`)
and need `pricing:GetProducts`; the public offer file (`--price-file`) is only a
labelled fallback when the API cannot answer. The AWS region is one value
(`awsregion.py`, chosen on the console, persisted in `web/target_region.json`)
that `provision.policy.REGION` / `dms.policy.REGION`, the kill switch and the
price lookups all read.

## Running it

```
$env:DBSHIFT_COLLECTOR_PASSWORD='...'      # read-only dbmig_collector
.\.venv\Scripts\python.exe -m collector.run       # discover  -> collector/output/<run_id>/
#   --migration-mode full-load-and-cdc   # default is full-load; decides what counts as a blocker
.\.venv\Scripts\python.exe -m collector.verify    # reconcile against ground truth
.\.venv\Scripts\python.exe -m assess.run          # assess    -> assess/output/assessment.json
.\.venv\Scripts\python.exe -m assess.report       # render    -> assess/output/report.html
.\.venv\Scripts\python.exe -m sizing.run          # target+sizing -> sizing/output/sizing.json
#   --engine postgresql --conversion convert/output/conversion_plan.json   # the heterogeneous path
.\scripts\postgres-target\run_pg.ps1              # local PostgreSQL 16 (Docker) for Phase 4b
.\.venv\Scripts\python.exe -m convert.run         # PL/SQL -> PL/pgSQL -> convert/output/conversion_plan.json
.\.venv\Scripts\python.exe -m report.run          # SCT/DMS-shaped report -> report/output/migration_report.html
.\.venv\Scripts\python.exe -m remediate.apply_cli --check   # what would be applied to the rehearsal copy
#   --applied-by you@example.com --confirm DBMIG_REHEARSAL   # ...and apply it, recorded against a person
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
`drive_counts_export.js` covers the Phase 1 counts and the Phase 2 table and
downloads (26/26); it downloads the CSV for real and parses what arrives. It
runs without a collector password against a server seeded from the newest run
on disk, so those two screens stay testable on a machine that cannot reach
Oracle.

**The rail is numbered by architecture phase, not by screen order.** Connect is a
prerequisite, not a phase, so it carries no number. Numbering it would shift every
phase by one and contradict `docs/02-architecture.md` in front of a client. The browser never talks to
Oracle — the server does, calling the same `collector` and `assess` modules the
CLI uses, so the UI cannot show a result the CLI would not.

- **Connect** runs a six-check preflight (reachability, auth, container,
  catalogue access, row-data access, CDC readiness) and states what a client
  network would need. The password lives in process memory only.
- **Discovery** asks the **migration mode** first — full load, or full load +
  CDC — because it decides what counts as a blocker, and shows whether the
  source is actually configured for CDC from the preflight's own evidence. Then
  it streams probe-by-probe progress over SSE and shows a summary with every
  dataset in an expandable list. Changing the mode marks a completed discovery
  as needing a re-run, since the assessment and gate built on it judged a
  different migration.
- **Assessment** streams rule-by-rule progress, then scores and the issues as a
  **sortable table** — severity, rule, finding, category, object count, fix level
  — with a row opening its own detail in place. **Detail** switches back to the
  full accordion, and **Download CSV / JSON** takes the findings away
  (`/api/assessment.csv`, one row per finding, Excel-safe BOM).
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

## Credentials

Local database credentials live in **`credentials.local.ps1`** (gitignored by the
`credentials*` rule). Dot-source it, do not run it:

```powershell
. .\credentials.local.ps1                  # collector -> DBMIG_APP
. .\credentials.local.ps1 -Estate telco    # collector -> DBMIG_TELCO
. .\credentials.local.ps1 -Estate rehearsal  # the writable copy, for Phase 4
```

It clears the per-estate variables before setting them, because dot-sourcing
twice in one shell otherwise leaves the previous estate behind — and Phase 4
decides what to remap a fix *onto* from `DBSHIFT_PRIMARY_SCHEMA`.

⚠️ **These passwords are also committed in five tracked files, and the repo is
public on GitHub.** The listener is bound to `0.0.0.0`, so a published password
reaches the database from anywhere on this network — verified by connecting over
the LAN address, not assumed. Rotate before the next client-facing run.
**`docs/15-credential-exposure.md`** has the locations and the fix.

## Two things that bite

- **`SELECT_CATALOG_ROLE` is not data access, and it is not `SELECT ANY
  DICTIONARY` either.** Row-level profiling needs the explicit grants in
  `scripts/oracle-source/05_grant_collector_read.sql`; **AWS SCT refuses to
  connect** without `SELECT ANY DICTIONARY` specifically, checking for the
  privilege rather than for equivalent reach
  (`scripts/oracle-source/08_grant_sct_dictionary.sql`). This role has now cost
  this project time twice.
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
