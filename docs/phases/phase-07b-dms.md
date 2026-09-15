# Phase 7b — Migrate with AWS DMS (the heterogeneous path)

> **Latest update — 2026-09-14 (built, planned against the live account).**
> `dms/` moves data to a PostgreSQL target, because Data Pump writes an
> Oracle-only format that PostgreSQL cannot read. Preflight, table selection,
> mappings, task settings and **the residue** — what DMS leaves behind and who
> finishes it — are all built and proven offline. Self-test **85/85**; console
> drive **26/26**. Planned against the real account: **10 tables move, 11 held
> back with reasons, 8 residue items**.
>
> **Run against AWS on 2026-09-14**: replication instance and both endpoints
> created for real. The **target endpoint connected**; the source refused with
> `ORA-12170` because AWS cannot reach an on-premises listener without a VPN.
> Two bugs found by running it. Everything was deleted afterwards. See
> "Run against AWS" below.

## Purpose

Move rows from Oracle to Amazon RDS for PostgreSQL, and be honest about the
rest. DMS migrates table data. It does not migrate sequences, views,
materialized views, external tables or stored code, and when a table fails
mid-load it suspends that table and carries on. A phase that reported
"migration complete" over that would be lying by omission.

## Why DMS and not Data Pump

Data Pump writes a proprietary Oracle format. There is no PostgreSQL reader for
it, so on the heterogeneous path DMS is not the better option — it is the only
one. It also brings something Data Pump cannot: **change data capture**, which
keeps the target current after the full load and is what turns a full-outage
cutover into a short one.

The cost model is different too, and the phase says so everywhere it matters. A
Data Pump export finishes and stops. **A DMS replication instance bills by the
hour for as long as it exists**, whether or not a task is running.

## What actually happens

`dms/run.py :: plan()` — free, creates nothing:

1. **Records agree.** Same check as Phase 6: every record from one collector run.
2. **The gate allows it.** Phase 5 distinguishes `migrate_full_load` from
   `migrate_cdc`, because the findings that stop replication are not the ones
   that stop a bulk copy.
3. **CDC is actually possible** (`preflight.cdc_possible`). Three checks that
   only run for a CDC task, read from discovery rather than inferred from a
   finding — a waiver accepts a risk, it does not change the database:
   - **archivelog** — DMS reads redo; redo that is not archived cannot be read
   - **supplemental logging** — without it redo carries no key columns, so an
     UPDATE cannot be matched to a target row
   - **primary keys** — a keyless table cannot be identified row by row, so
     every UPDATE and DELETE to it is lost **silently**
4. **Table selection** (`mappings.select_tables`). Of 21 tables on `DBMIG_APP`,
   10 move. Held back: 9 Oracle internals (Text index, AQ, materialized view
   log), the materialized view, and the external table. Each is named with a
   reason.
5. **Mappings and settings** are generated, not hand-written.
6. **The residue** (`residue.build`) — everything DMS will not finish.

`execute()` then creates the replication instance, both endpoints (testing each
before the task depends on it), the task, and starts it.

## Name folding — the detail that breaks everything quietly

Oracle folds unquoted identifiers **up**; PostgreSQL folds them **down**. Carry
`CUSTOMER` across unchanged and PostgreSQL gets a table only `"CUSTOMER"` in
quotes can reach — and every function Phase 4b converted, which says
`FROM customer`, fails to find it.

So the heterogeneous path applies three `convert-lowercase` transformation
rules: schema, table **and column**. The column rule is not optional; without it
lower-cased tables keep upper-case columns and unquoted SQL breaks just as
thoroughly.

## What DMS leaves behind, and who finishes it

`dms/residue.py`. Each item is routed the way the rest of the project routes
work, and **nothing is applied**:

| Tier | What | On `DBMIG_APP` |
|---|---|---|
| **RULE** | one correct answer, derivable from discovery | 5 sequences |
| **MODEL** | needs judgement — rewriting SQL | 1 view, 1 materialized view |
| **PERSON** | no target equivalent; a decision | 1 external table |

**Sequences are the dangerous one.** DMS never migrates a sequence. Left at 1,
the first insert on the target reuses a key that already exists — a constraint
violation if you are lucky, duplicate business keys if you are not. The fix is
arithmetic over `last_number`, so it is a rule with no judgement in it.

**A suspended table is the quiet one.** `TableErrorPolicy: SUSPEND_TABLE` means
a run finishes "successfully" with a table holding a partial copy. It becomes a
residue item, so no later phase treats it as migrated.

## The model tier, now that it is live

**Added 2026-09-14.** The residue's MODEL items are answered by Sonnet 4.6:
`residue.apply_model` gives the model the view's own Oracle SQL, collected by
Phase 1, and asks for a PostgreSQL rewrite qualified to the target schema.

`model_available` used to be hardcoded `False`, which dated from when Bedrock
was blocked and meant a live run still reported MODEL_REQUIRED for work the
model could have done. It now follows whether the tier actually invokes, asked
rather than assumed -- a configured-but-unreachable model would otherwise let
the residue claim answers it never produced. `DBSHIFT_MODEL_MODE=off` forces it
off for a deterministic run.

**Proven on DBMIG_TELCO**: all three MODEL items converted, and **every
statement executed against real PostgreSQL inside a rolled-back transaction**.

| Object | Result |
|---|---|
| `MV_REVENUE_BY_PERIOD` | created `WITH NO DATA`, so it does not block on a scan |
| `VW_ACTIVE_SUBSCRIBER` | schema-qualified join, ran |
| `VW_INVOICE_BALANCE` | `NVL` became `COALESCE`, ran |

A model that returns prose, or declines with an empty `sql`, leaves the item at
MODEL_REQUIRED -- **nothing is invented in its place**, and a labelled stand-in
still answers where one exists. Ten self-test checks cover exactly those paths.

## The model tier, while Bedrock was blocked

**No model runs.** `InvokeModel` fails on this account for want of a payment
instrument. An item needing judgement is either answered from a **labelled
hand-written stand-in** in `bedrock/static/dms/<ESTATE>.json`, or reported as
`MODEL_REQUIRED` with nothing invented in its place.

A stand-in carries `source: "static_fixture"` and `model_id: null`, and the
console renders it with a note saying it was written by a person and is not
model output. That is the same contract `bedrock/static/convert/` uses.

When Bedrock is unblocked, `residue.build(model_available=True)` should call the
model and these fixtures become the regression set its output is compared
against — see `docs/14-bedrock-enablement.md`.

## Cost

| Item | Setting | Why |
|---|---|---|
| Instance class | `dms.t3.small` | smallest orderable in ap-south-1 |
| Storage | 5 GB | the documented minimum; replication storage holds cached changes, not the data |
| Multi-AZ | off | a standby doubles the hourly rate |
| TTL tag | 8 hours | an abandoned instance is visible to the kill switch and to a person |

**Stopping a task does not stop the bill.** Delete the instance:
`python -m killswitch --destroy --confirm <account>`. The kill switch already
scans and deletes DMS replication instances.

## Known limits

- **CDC cannot run on `DBMIG_APP` as it stands.** NOARCHIVELOG, no supplemental
  logging, two keyless tables. The preflight refuses rather than letting the
  full load succeed and the CDC phase fail after the window is spent.
- **Table DDL is not created by this phase.** DMS creates target tables with a
  default type mapping; a production migration would use converted DDL.
- **No data has been migrated.** The replication instance and both endpoints
  were created and tested for real, but the source is unreachable from AWS, so
  no task was created and no row has moved. A migration from this source needs a
  VPN or Direct Connect first.

## How to run it

```powershell
python -m dms.run                                        # plan only, free
python -m dms.run --migration-type full-load-and-cdc     # plan a CDC task
python -m dms.run --execute --confirm <account-id>       # THIS BILLS
python -m dms.run --status                               # where the task stands
python -m dms.run --stop                                 # stop it (still billing)
```

Console: **Phase 7 · Migrate** shows the DMS half when Phase 3 chose
PostgreSQL, and the Data Pump half when it chose Oracle.

## Run against AWS, 2026-09-14

The replication instance, both endpoints and the connection tests were created
for real against account 106325261146.

| Step | Result |
|---|---|
| Replication instance `dbshift-dms-dbmig-app` | created, `available` |
| Source endpoint (Oracle, on-premises) | created |
| Target endpoint (RDS PostgreSQL) | created |
| **Target connection test** | **successful** |
| **Source connection test** | **failed — `ORA-12170: TNS:Connect timeout`** |

**The source failure is a network fact, not a defect.** DMS runs inside AWS and
the Oracle source is on a laptop behind a home router. There is no VPN, no
Direct Connect, and the listener is not reachable from outside. This is exactly
what Phase 1's network requirements screen tells a client they must provide, and
the phase refused *before* creating a task rather than failing mid-migration.

**Two real bugs were found by running it**, both of which would have stopped a
genuine migration:

- **No SSL mode on the endpoints.** DMS defaults to `none`; RDS for PostgreSQL
  refuses an unencrypted connection outright with `no pg_hba.conf entry ... no
  encryption`. `policy.TARGET_SSL_MODE` is now `require`, set in
  `actions.create_endpoints`. The target endpoint connected immediately
  afterwards.
- **The security group opened the wrong port.** `provision/render.py` hardcoded
  Oracle's 1521, so a PostgreSQL target came up with nothing listening on the
  open port; the instance was unreachable and verification timed out with no
  indication why. The port now follows the engine.

Everything was deleted afterwards: both endpoints, the replication instance, and
the PostgreSQL instance stopped.

## Change log

**2026-09-14 — built.** `dms/` created: `policy.py`, `mappings.py`,
`preflight.py`, `actions.py`, `residue.py`, `run.py`, `selftest.py` (85 checks);
`bedrock/static/dms/DBMIG_APP.json` (two labelled stand-ins);
`/api/dms/plan`, `/api/dms/status`, `/api/dms/execute`; the Migrate screen
splits by engine. The `dms-vpc-role` was created in the account — DMS needs it
to place a replication instance in a VPC and it did not exist.

Three bugs found while proving it, all of which would have produced a migration
that looked successful:

- **Log mode was read from the wrong record.** `provision.records.source_facts`
  does not carry `log_mode`, so the CDC check reported ARCHIVELOG as PASS on a
  source that is in NOARCHIVELOG. Read from the sizing facts instead.
- **A materialized view appears in `DBA_OBJECTS` twice**, once as
  MATERIALIZED VIEW and once as the TABLE holding its rows. Keying a dict by
  name kept whichever came last, so `MV_LOAN_SUMMARY` was going to be migrated
  as an ordinary table.
- **The chosen target only restored when connected.** The Migrate screen showed
  Data Pump while the server held PostgreSQL. The target is server state
  independent of any connection, so it now restores before that early return.
