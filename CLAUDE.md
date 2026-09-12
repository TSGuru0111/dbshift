# DBShift AI — Project Context

> This file is read automatically by Claude Code at the start of every session.
> Keep it short and current. Detail lives in `docs/`; this is the map.

## What this project is

An AWS-native, human-in-the-loop AI agent that migrates an on-premises Oracle
database to Amazon RDS for Oracle. Built as a company accelerator — the
deliverable is a demonstrable capability, not a one-off migration.

**Scope is deliberately narrow.** One source engine, one target, ten phases.
Aurora PostgreSQL, Redshift and Oracle Database@AWS are explicitly out of scope
as *targets*. See `docs/02-architecture.md` for why, and do not re-add them.
**One exception, decided 2026-09-12:** PL/SQL → PL/pgSQL conversion exists as
a target-agnostic capability (Phase 4b, `convert/`), because it is the part of
any heterogeneous path AWS's own accelerator leaves manual. It compiles against
a local PostgreSQL and applies nothing.

## Current state

| Item | Status |
|---|---|
| Source Oracle estate | ✅ Built, 1.03 GB, 90 objects, 51 constraints, verified |
| **Second estate** | ✅ `DBMIG_TELCO`, **5.67 GB**, 32.7M rows, 68 objects, 14 defects. Telecom billing, own bigfile tablespace. `docs/12-telco-estate.md` |
| Seeded defects | ✅ 8 defects in place, documented |
| Golden snapshot | ✅ `data/dbmig_golden.dmp` |
| Discovery collector | ✅ `collector/`, 47 datasets, local JSON, 5/5 verify checks |
| Assessment engine | ✅ `assess/`, 50 rules as data, **7/7 on DBMIG_APP, 14/14 on DBMIG_TELCO**, HTML report |
| Size & Edition decision | ✅ `sizing/`, EE BYOL verdict, rules overrode the proposal |
| AWS account | ✅ Granted 2026-09-09, SSO + `DBA_permissions`. See `docs/05-aws-services.md` |
| Bedrock access | ⛔ **Blocked** — re-tested 2026-09-12: every invoke now fails with `INVALID_PAYMENT_INSTRUMENT` (the account has no valid payment method for the Marketplace subscription). A Billing-console fix by an admin, not IAM. `docs/05-aws-services.md`; the enablement plan is `docs/14-bedrock-enablement.md` |
| Rehearsal copy | ✅ `DBMIG_REHEARSAL`, 85/90 objects, same XE instance. `scripts/oracle-source/06_*` |
| Detect & Remediate | 🟡 `remediate/`, all 5 gates live. 2 fixes proven apply+rollback on the copy; the other 61 need Bedrock |
| **Convert PL/SQL (4b)** | ✅ `convert/`, built 2026-09-12. PL/SQL → PL/pgSQL, 5 gates, **compiled for real on PostgreSQL 16 in Docker and rolled back**. 6/6 convertible objects ready on both estates, model tier needed by 0. `docs/phases/phase-04b-convert.md` |
| Provision (6) | ✅ `dbshift-target-dbmig-app` deployed and verified 2026-09-11, RDS Oracle 19c SE2 |
| Migrate (7) | ✅ Full load run 2026-09-11 — 11/11 tables match |
| Validate (8) | ✅ `validated` 2026-09-12 — 5 levels, 5.4M rows a side, 0 mismatches |
| Cutover (9) | ✅ **Cut over 2026-09-12** — records re-run for the target's run `6e48d16a`, OPS-001/OPS-002 accepted by `guru.ts@ganitinc.com`, 1 step applied, 0 failed. Applications are not repointed; the source stays authoritative |
| Kill switch | ✅ `killswitch/` — stop, empty and destroy every `dbshift-*` resource. **The instance was stopped again after the cutover; RDS auto-restarts a stopped instance after 7 days** |
| **Report (10)** | ✅ `report/`, built 2026-09-12. The "DMS and SCT report": SCT-style conversion assessment + DMS pre-migration assessment from records. Console stage **10 · Report** (rail, unlocked by the assessment) plus the header **Report ↗** link. `docs/phases/phase-10-report.md` |

## Running it

```
$env:DBSHIFT_COLLECTOR_PASSWORD='...'      # read-only dbmig_collector
.\.venv\Scripts\python.exe -m collector.run       # discover  -> collector/output/<run_id>/
.\.venv\Scripts\python.exe -m collector.verify    # reconcile against ground truth
.\.venv\Scripts\python.exe -m assess.run          # assess    -> assess/output/assessment.json
.\.venv\Scripts\python.exe -m assess.report       # render    -> assess/output/report.html
.\.venv\Scripts\python.exe -m sizing.run          # size+edition -> sizing/output/sizing.json
.\scripts\postgres-target\run_pg.ps1              # local PostgreSQL 16 (Docker) for Phase 4b
.\.venv\Scripts\python.exe -m convert.run         # PL/SQL -> PL/pgSQL -> convert/output/conversion_plan.json
.\.venv\Scripts\python.exe -m report.run          # SCT/DMS-shaped report -> report/output/migration_report.html
```

## The console

```
.\.venv\Scripts\python.exe -m web.server          # http://127.0.0.1:8765
```

Connect, then **Phases 1–10** gated in order — Discover, Assess, Size & Edition,
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
