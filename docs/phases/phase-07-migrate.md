# Phase 7 — Migrate

> **Latest update — 2026-09-11 (migrated).** With `RDS-004` resolved in-phase on the
> approver's decision (`guru.ts@ganitinc.com`), the full pipeline ran into the RDS
> target: dump uploaded (575 MB, 53 s), staged into `DATA_PUMP_DIR` (602,812,416
> bytes, identical to the export), schema owner created from discovery, the
> external file moved, Data Pump import in 1 m 56 s — *completed with 17 errors*.
> A new step, **Triage the import log and repair**, classified all 17 plus 3
> invalid objects by rule: **6 repaired** (the missing custom role and its 3
> grants; the Oracle Text index, whose 21c import call 19c rejects, rebuilt
> natively from the source DDL in 26 s; 2 synonyms recompiled), **13 expected and
> explained** (12 grants to the source-only discovery account, the seeded broken
> procedure), **0 left for a person**. Phase 4's two runbook steps applied. First
> count: 80 objects plus 7 Oracle Text internals; **11 of 11 comparable tables
> match exactly on rows**; nothing that was valid on the source is invalid.
>
> **Previous — 2026-09-11. Built; first run stopped at the gate, as designed.**
> `python -m migrate.run` and the console's **Phase 7 · Migrate** screen run eleven
> steps in order and show each one live: what it does, who decided it, what it
> changes, whether it can be undone, every command or SQL statement as it runs
> (passwords masked), and the evidence it ends on. The source was re-exported with
> Data Pump **`VERSION=19`** as the schema owner (`DBMIG_APP_V19.DMP`, every table,
> 2 m 46 s). The first console run passed *Records agree* and *Target is up and
> correct* — logging in and re-verifying the live RDS instance — then **stopped at
> the gate: the full load is blocked by `RDS-004`**. Nothing was exported, uploaded
> or created. The run continues once the approver chooses how `RDS-004` is handled.

## Purpose

Move the `DBMIG_APP` schema and its data into the RDS target Phase 6 built — and
make every step something a person can watch, question and audit. The console is
not a progress bar; it is the backend's own account of what it did.

## What actually happens

`migrate/run.py :: execute()` runs `migrate/steps.py :: STEPS` in order and stops
at the first step that returns `fail` or `stop`.

| # | Step | Decided by | Changes |
|---|---|---|---|
| 1 | **Records agree** — the four phase records share one collector run | rule | nothing |
| 2 | **Target is up and correct** — `CREATE_COMPLETE`; the instance's `collector_run_id` tag matches the records; `provision.verify` re-run live | rule | nothing (read-only login) |
| 3 | **Blocker gate allows a full load** — `migrate_full_load` must be clear, or its blockers resolved in this phase | gate | nothing |
| 4 | **Export the source for 19c** — `expdp VERSION=19` as the schema owner, or reuse a successful VERSION=19 dump | rule | source: a dump file |
| 5 | **Upload the dump to S3** — the exchange bucket; size checked with `HeadObject` | orchestrator | one S3 object (7-day expiry) |
| 6 | **Bring the dump into RDS** — `rdsadmin_s3_tasks.download_from_s3` into `DATA_PUMP_DIR`, following the RDS task log; size checked against the export | orchestrator | one file on the target |
| 7 | **Create the schema owner** — the user with the privileges and roles discovery recorded; custom roles created; the flagged profile *not* copied | rule | target: user, grants, role |
| 8 | **Resolve RDS-004** *(only when chosen)* — the external file to S3, its directory recreated on RDS under the same name | approval | S3 object, one directory |
| 9 | **Import schema and data** — `DBMS_DATAPUMP` on the target, log followed line by line | orchestrator | the whole schema |
| 10 | **Triage the import log and repair** — every failure classified: *repaired* where the fix is certain, *expected* where failing is correct, *for a person* otherwise; invalid objects compiled as their owner | rule | grants, a rebuilt index, recompiled objects |
| 10 | **Apply Phase 4's target steps** — disable the scheduler job, first complete refresh of the materialized view | phase4_advice | one job, one MV |
| 11 | **First count** — objects by type and exact row counts, source against target | rule | nothing |

`decided_by` is shown on every card, verbatim:

- **rule** — a deterministic check in this codebase
- **gate** — the Phase 5 blocker gate
- **phase4_advice** — an artefact from the Phase 4 plan. Today these are the
  hand-written static fixtures, and the card says *"static fixture, not model
  output"*
- **approval** — a named person, recorded from the identity on the credentials
- **orchestrator** — sequencing, no judgement

## Inputs / Outputs

| | |
|---|---|
| Input | the four phase records (same run), `provision/output/provision_plan.json`, the live stack |
| Input | discovery datasets: `directories`, `table_privileges`, `users`, `system_privileges`, `role_privileges`, `roles`, `external_tables`, `objects`, `tables` |
| Input | `DBSHIFT_SOURCE_OWNER_PASSWORD` — only for a fresh export; console field held in memory |
| Input | `DBSHIFT_COLLECTOR_PASSWORD` — read-only: external file lookup, source row counts |
| Output | `migrate/output/migration_run.json` — every step, result, evidence and log line |
| Output | on the target: the `DBMIG_APP` schema |

## Design decisions

**Data Pump, not DMS, for this run.** At ~1 GB it loads in minutes, needs no
replication instance to pay for, and uses the S3 path Phase 6 already built.
Data Pump also loads in the order the architecture asks for — tables, rows, then
indexes and constraints, then code — so foreign keys are not validated row by
row during the load. DMS returns when change data capture becomes possible; its
Phase 4 settings are carried in the plan and listed as *not applicable* here.

**Export as the owner, not a DBA.** Least privilege, confirmed from discovery:
`DBMIG_APP` holds READ/WRITE on `DBMIG_EXT_DIR`. The consequence is that the dump
carries no `CREATE USER`, so step 7 creates the owner on the target with exactly
the discovered privileges — which is also more explainable than a DBA-level dump.

**The export directory comes from discovery, not from a constant.** The first
writable directory the owner holds a `WRITE` grant on, resolved to its path.

**The source profile is not copied.** `DBMIG_APP_PROFILE` was flagged by `SEC-006`
(password never expires). Recreating it would carry a known weakness onto the
target on the tool's own initiative; the target starts on `DEFAULT` and the step
says so.

**A custom role is created; an Oracle-maintained one is never faked.** A role
missing on the target is created only if discovery marks it `oracle_maintained =
'N'`. Its object grants arrive with the import.

**`RDS-004` is resolved here only on an approval.** The gate blocks the full load
on the external table. This phase can genuinely fix it — the file moves to an
RDS directory of the same name, so the imported definition works unchanged — but
choosing that route is a decision, recorded against the approver's identity.
Without it, the run stops at step 3 and says how to proceed.

**Runbook SQL from the plan is allow-listed.** Only `DBMS_SCHEDULER` and
`DBMS_MVIEW` calls may run from Phase 4 artefacts, in rule order, so the job is
disabled before the materialized view is refreshed.

**"Completed with 17 errors" is not an outcome.** Data Pump reports a failure and
carries on, so the repair step reads the import log and classifies every entry
by rule. What it repairs, it repairs only when the fix is certain: a grant whose
grantee is a custom role from discovery; a CONTEXT index whose source DDL matches
a plain `CREATE INDEX … INDEXTYPE IS CTXSYS.CONTEXT`, rebuilt as its owner; an
object that was valid on the source, compiled as its owner. Grants to the
discovery account are expected — that account has no business on the target.
Anything unrecognised goes to a person with its full error text. This is the
step where a reasoning model will add the most in the final version: classifying
what these rules do not recognise, and drafting a fix for a person to approve.

**Resuming skips work, never checks.** `--from <step>` (console: *Start from*)
skips completed steps, but records, target and gate run every time — what they
check can change between runs.

**Row counts compare numbers only.** A source count the read-only account could
not take is *not comparable*. The first version compared error strings too, so a
table missing on both sides "matched"; the honest figure fell from 18/21 to 11/11.

**Nothing is hidden in the log.** Every statement is logged before it runs;
passwords are replaced with `********`; polling queries are the only ones left
out, because they would bury the log.

## Known limits

- **The database link imported, but points at the source host** (`RDS-005`). It
  exists on the target and will fail when used; recreating it needs a network path.
- **Oracle Text internals differ by version** — 21c has `$B`, `$C`, `$Q` tables that
  19c does not. They are counted separately (source 10, target 7), never as data.
- **The repair rules cover what this estate produced.** A different estate will
  produce failures they do not recognise; those go to a person, which is correct.
- **The scheduler job is created enabled by the import** and disabled one step
  later. The window is seconds; the job's schedule makes a run inside it unlikely,
  not impossible.
- **The external file must be readable from where this runs.** Here the source is
  local. On a client estate the file lives on the database server.
- **Full load only.** Change data capture stays off while `OPS-001`, `OPS-002` and
  `DQ-001` are open, so cutover needs an outage window.
- **Step 11 is a count, not validation.** Phase 8 validates.

## How to run it

```powershell
python -m migrate.run                            # stops at the gate while RDS-004 is open
python -m migrate.run --resolve-external-via-s3  # the approved route for RDS-004
python -m migrate.run --fresh-export             # export again first
```

**Console: Phase 7 · Migrate** — opens once the target is `CREATE_COMPLETE`.

## Change log

**2026-09-11 — first full migration, and a repair step.** Run with `RDS-004`
resolved in-phase (approval recorded). Steps 1–9 and 11–12 passed; the import
completed with 17 errors. Two were real gaps in this code: `DBMIG_READ_ROLE`
was never created, because step 7 only created custom roles *granted to* the
owner, not those *receiving* grants from its objects (fixed in step 7, and
repaired on the target); and `IX_COMM_NOTES_TEXT` failed because the dump
carries 21c's internal `ctxsys.driimp.create_index` call, which 19c rejects with
`PLS-00306` — the downgrade risk Phase 6 warned about, now seen for real. Added
the repair step, `--from` / *Start from*, and honest row comparison. Re-ran from
`repair`: 6 repaired, 13 expected, 0 for a person; 11 of 11 comparable tables
match on rows.

**2026-09-11 — built.** `migrate/steps.py`, `migrate/run.py`, console stage with a
live step timeline. Source re-exported with `VERSION=19` as `DBMIG_APP`
(`DBMIG_APP_V19.DMP`: `COMM_LOG` 400 MB, `PAYMENT_HIST` 1.8 M rows, all four
`LOAN_TXN` partitions; 2 m 46 s). First console run: steps 1–2 passed including a
live re-verification of the target; step 3 stopped on `RDS-004`; steps 4–11
untouched.
