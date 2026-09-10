# DBShift AI — Project Context

> This file is read automatically by Claude Code at the start of every session.
> Keep it short and current. Detail lives in `docs/`; this is the map.

## What this project is

An AWS-native, human-in-the-loop AI agent that migrates an on-premises Oracle
database to Amazon RDS for Oracle. Built as a company accelerator — the
deliverable is a demonstrable capability, not a one-off migration.

**Scope is deliberately narrow.** One source engine, one target, ten phases.
Aurora PostgreSQL, Redshift and Oracle Database@AWS are explicitly out of scope.
See `docs/02-architecture.md` for why, and do not re-add them.

## Current state

| Item | Status |
|---|---|
| Source Oracle estate | ✅ Built, 1.03 GB, 90 objects, 51 constraints, verified |
| Seeded defects | ✅ 8 defects in place, documented |
| Golden snapshot | ✅ `data/dbmig_golden.dmp` |
| Discovery collector | ✅ `collector/`, 47 datasets, local JSON, 5/5 verify checks |
| Assessment engine | ✅ `assess/`, 50 rules as data, **7/7 recall**, HTML report |
| Size & Edition decision | ✅ `sizing/`, EE BYOL verdict, rules overrode the proposal |
| AWS account | ✅ Granted 2026-09-09, SSO + `DBA_permissions`. See `docs/05-aws-services.md` |
| Bedrock access | ⛔ **Blocked** — listing works, every invoke fails on a Marketplace subscription gap. `docs/05-aws-services.md` has the two fixes |
| Detect & Remediate | ⬜ Blocked on the above — Bedrock invoke must work first |
| Everything AWS-side | ⬜ Nothing provisioned yet. Set budget alerts before the first resource |

## Running it

```
$env:DBSHIFT_COLLECTOR_PASSWORD='...'      # read-only dbmig_collector
.\.venv\Scripts\python.exe -m collector.run       # discover  -> collector/output/<run_id>/
.\.venv\Scripts\python.exe -m collector.verify    # reconcile against ground truth
.\.venv\Scripts\python.exe -m assess.run          # assess    -> assess/output/assessment.json
.\.venv\Scripts\python.exe -m assess.report       # render    -> assess/output/report.html
.\.venv\Scripts\python.exe -m sizing.run          # size+edition -> sizing/output/sizing.json
```

## The console

```
.\.venv\Scripts\python.exe -m web.server          # http://127.0.0.1:8765
```

Three stages, gated in order: **Connect → Discovery → Assessment**, with a stage
rail across the top that advances as each finishes. The browser never talks to
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

## Two things that bite

- **`SELECT_CATALOG_ROLE` is not data access.** Row-level profiling needs the
  explicit grants in `scripts/oracle-source/05_grant_collector_read.sql`.
- **Seeded defect 7 is not in the database** — the seed script is broken. See
  `docs/04-defects.md`. Max recall is 7/8 until it is re-seeded.

## Where things are

- `docs/` — all reference material, numbered in reading order
- `scripts/oracle-source/` — the SQL that builds the source estate
- `data/` — golden dump (gitignored if large)
- `collector/` — Python discovery collector (to be built)
- `infra/` — CDK / CloudFormation (later)

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
