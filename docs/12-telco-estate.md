# Second source estate — DBMIG_TELCO

A second synthetic Oracle estate, ~5 GB, built to answer one question:
**do Phases 1–5 actually work, or do they only work on `DBMIG_APP`?**

`DBMIG_APP` is the estate the collector probes and the 50 assessment rules were
written against. Scoring an engine on the data it was tuned on measures the
tuning, not the engine. This estate is deliberately different in domain, shape,
size and defects, and nothing in it was looked at while writing the rules.

**`DBMIG_APP`, `DBMIG_RPT` and `DBMIG_REHEARSAL` are untouched.** This estate is
additive: a separate user, a separate tablespace, separate scripts.

## Connection

```
Host:     localhost
Port:     1521
Service:  XEPDB1              <-- service name, NOT the SID (XE)
User:     dbmig_telco
Password: DbMig2026Telco
```

The read-only `dbmig_collector` account reaches it through per-table `SELECT`
grants issued at the end of `02_schema_objects.sql`.

## Why a dedicated tablespace

`USERS` is a **smallfile datafile capped at `MAXSIZE 4G`**, and ~2.25 GB of that
was already allocated to `DBMIG_APP` and `DBMIG_REHEARSAL`. A 5 GB schema cannot
physically fit there.

`DBMIG_TELCO_TS` is therefore a **bigfile** tablespace — one datafile,
autoextending to 20 GB — at
`C:\APP\GANITADMIN\PRODUCT\21C\ORADATA\XE\XEPDB1\DBMIG_TELCO01.DBF`.

XE's 12 GB user-data limit still applies across the instance. With ~2.1 GB of
existing estate plus ~5.3 GB here, total lands near 7.5 GB — inside the cap, but
there is not room for a third estate of this size.

## Domain and object census

Telecom billing: subscribers, devices, call detail records, invoices, payments,
support tickets.

| Object type | Count |
|---|---|
| TABLE | 10 (+2 seeded by defects) |
| INDEX | 20 |
| TABLE PARTITION / INDEX PARTITION | 6 / 6 |
| SEQUENCE | 5 |
| VIEW | 2 |
| TYPE | 2 |
| MATERIALIZED VIEW + MV LOG | 1 + 1 |
| PACKAGE / PACKAGE BODY | 1 each |
| PROCEDURE / FUNCTION / TRIGGER / SYNONYM / LOB | 1 each |

Deliberately spans the same breadth of object types as `DBMIG_APP` so every
collector probe has something to find, but arranged differently:

- **`CDR`** is range-partitioned by `call_date` across 5 quarters plus a
  `MAXVALUE` catch-all, with a **LOCAL** index. `DBMIG_APP`'s partitioned table
  has a global index.
- **`PLAN_CATALOG`** is an **index-organized table**. `DBMIG_APP` has no IOT.
- **`SUPPORT_TICKET.transcript`** is a **SecureFile** CLOB. `DBMIG_APP`'s CLOB is
  BasicFile.
- **`MV_REVENUE_BY_PERIOD`** is `BUILD DEFERRED` + `REFRESH COMPLETE ON DEMAND`
  with an MV log present — a different refresh path from `DBMIG_APP`'s.

## Row counts and size

Targets are **measured, not estimated** — see the header of
`03_generate_data.sql` for the calibration method and the numbers.

| Table | Rows | Bytes/row | Size |
|---|---|---|---|
| `cdr` | 21,000,000 | 156.2 | 3.05 GB |
| `invoice_line` | 5,000,000 | 154.2 | 0.72 GB |
| `support_ticket` | 400,000 | 1,437.7 | 0.54 GB |
| `invoice` | 2,500,000 | 135.5 | 0.32 GB |
| `payment` | 2,000,000 | 92.2 | 0.17 GB |
| `subscriber` | 800,000 | 247.0 | 0.18 GB |
| `device` | 1,000,000 | 156.8 | 0.15 GB |
| `usage_staging` | 250,000 | — | seeded by defect 2 |
| `network_cell` | 20,000 | — | |
| `plan_catalog` | 500 | — | IOT |
| | | **total** | **~5.3 GB** |

### Two sizing traps this hit

**A small sample measures allocation, not density.** A fresh partition allocates
an 8 MB initial extent, so a 50,000-row sample reported ~920 bytes/row for `CDR`
when the true figure is ~156. Extrapolating that projected **26.5 GB** — enough
to overrun both the tablespace and XE's limit, hours into a load. Density has to
come from *used blocks* (`DBMS_STATS`, then `USER_TABLES.BLOCKS`) plus index leaf
blocks plus LOB segments.

**`TRUNCATE` does not reset a sequence.** The calibration load consumed
`seq_subscriber_id` 1–50,000; after truncating, `subscriber_id` ran
50,001–850,000. Every child table generating a parent id as
`DBMS_RANDOM.VALUE(1, MAX+1)` then produced 50,000 ids that did not exist, and
the first `DEVICE` chunk died on `ORA-02291`. Because the runner continues past
errors, the load carried on and **`DEVICE` was left silently empty**.

Both are now defended in the script: ids come from the parent's real `MIN..MAX`,
and section 9b asserts every row count and `RAISE_APPLICATION_ERROR`s on a
mismatch, so a partial load cannot report success.

### A third trap, and the guard catching it

**`||` binds tighter than `+`.** In `INVOICE_LINE.description`:

```sql
'... item ' || MOD(lvl, 8) + 1 || ' rated ...'   -- ORA-01722
'... item ' || (MOD(lvl, 8) + 1) || ' rated ...' -- correct
```

Oracle parses the first as `('... item ' || MOD(lvl,8)) + 1` — arithmetic on a
string. The identical expression one line above is fine because it sits inside
`LPAD(...)`, whose parentheses supply the grouping.

This is worth recording because of what happened next: the load continued past
the failure, loaded `PAYMENT` and `SUPPORT_TICKET` normally, and **section 9b
then failed the whole script with `ORA-20001: Load incomplete: 1 table(s)
short`**. Without that assertion the run would have ended with `INVOICE_LINE`
holding zero rows and nothing saying so — the same silent-partial-load failure
as the `DEVICE` case, which is exactly what it was written for.

## Seeded defects — 14, deliberately different

Full key: **`scripts/telco-source/answer_key.json`**. It mirrors
`04_seed_defects.sql`; change one, change the other in the same commit.

Six target rules the `DBMIG_APP` estate **never exercised**, so they have never
fired in this project's history:

| # | Defect | Rule | New? |
|---|---|---|---|
| 9 | Unusable + invisible index | PERF-004 | **never fired before** |
| 10 | Disabled constraint with violating data | DQ-005 | **never fired before** |
| 11 | Column that is entirely NULL | DQ-008 | **never fired before** |
| 12 | Byte-length character semantics | DQ-009 | **never fired before** |
| 13 | Redundant index on same leading column | PERF-008 | **never fired before** |
| 8 | Stored compilation errors | OPS-004 | **never fired before** |

The rest are shared classes placed differently and harder:

| # | Defect | Rule | Different how |
|---|---|---|---|
| 1 | Reserved words | RDS-001/002 | `"SESSION"` with `"COMMENT"`, `"LEVEL"`, `"DATE"` — not `"ORDER"` |
| 2 | No primary key | DQ-001 | 250,000 rows, not 2 — cannot be dismissed as a scratch table |
| 3 | Unindexed FK | PERF-001 | on a column added after the fact |
| 4 | ENABLE NOVALIDATE | DQ-004 | 40 orphans, not 1 |
| 5 | Duplicate near-unique | DQ-002 | 500 duplicate rows |
| 6 | Unconstrained NUMBER | DQ-006 | plus a bare `FLOAT` |
| 7 | Non-ASCII characters | DQ-003 | **seeded correctly** — see below |
| 14 | Sensitive columns | SEC-007 | unmistakable names |

### Defect 7 is the one `DBMIG_APP` never actually had

`docs/04-defects.md` records that `DBMIG_APP`'s defect 7 was never seeded, and
max recall there is 7/8. The mechanism is worth stating precisely, because an
earlier revision of that file got it wrong: `CHR(146)` does **not** return NULL —
it returns a 1-byte value holding `0x92`. In AL32UTF8 that byte is an invalid
lead byte and is **dropped during concatenation**, so `full_name || CHR(146)` is
just `full_name` and the `UPDATE` reported one row changed having changed
nothing. Testing `CHR(146) IS NULL` returns false and would wrongly suggest the
seed had worked.

Here it is seeded with **`UNISTR`**, which takes a Unicode code point regardless
of database character set — U+2019 (the smart quote cp1252 `0x92` really maps
to), U+00E9/U+00E7, and U+20AC. The seed script then **proves it landed** with
`DUMP()` and an `ASCIISTR(x) != x` count rather than trusting the row-count
message that masked the original failure.

## Running it

```powershell
cd scripts\telco-source

# sqlplus is not installed on this machine; run_sql.py is the substitute.
..\..\.venv\Scripts\python.exe run_sql.py 01_setup_admin.sql    --user system      --password <SYSTEM pw>
..\..\.venv\Scripts\python.exe run_sql.py 02_schema_objects.sql --user dbmig_telco --password DbMig2026Telco
..\..\.venv\Scripts\python.exe run_sql.py 03_generate_data.sql  --user dbmig_telco --password DbMig2026Telco
..\..\.venv\Scripts\python.exe run_sql.py 04_seed_defects.sql   --user dbmig_telco --password DbMig2026Telco
```

`run_sql.py` is a small SQL*Plus subset: `;`-terminated statements, `/`-terminated
PL/SQL, `SET`/`PROMPT`/`WHENEVER` skipped, `SELECT` output printed, errors
reported and execution continued, and a non-zero exit if anything failed.

The load takes roughly 30–40 minutes. Direct-path `INSERT /*+ APPEND */` runs at
~17,000 rows/s here; the original `DBMIG_APP` generator's row-by-row PL/SQL would
take many hours for this volume.

## Pointing the phases at this estate

Every phase is driven by environment variables — no code changes:

```powershell
$env:DBSHIFT_COLLECTOR_PASSWORD = 'DbMig2026Coll'
$env:DBSHIFT_SCHEMAS            = 'DBMIG_TELCO'
$env:DBSHIFT_PRIMARY_SCHEMA     = 'DBMIG_TELCO'
$env:DBSHIFT_REFERENCE_SCHEMA   = 'DBMIG_TELCO'
$env:DBSHIFT_ANSWER_KEY         = 'scripts\telco-source\answer_key.json'
```

`DBSHIFT_REFERENCE_SCHEMA` and `DBSHIFT_ANSWER_KEY` were added to
`assess/scoring.py` for this work. They default to the `DBMIG_APP` key, so the
existing behaviour is unchanged when they are unset — but they must be set
**together**, or recall gets measured against the wrong estate.

### The console needs no env vars

`assess/scoring.py` keeps a `KNOWN_ANSWER_KEYS` registry mapping schema to key,
so the console resolves the right one from the estate you connect to. The env
vars above are for the CLI, which is invoked per estate. Without the registry the
console reported `0/0 recall, applicable: false` on this estate while the CLI
reported 14/14 on identical findings.

`collector.verify` also compares against `DOCUMENTED_OBJECTS = 90` and
`DOCUMENTED_CONSTRAINTS = 51`, which are ground truth for `DBMIG_APP` only. It
reports the difference as drift rather than failing, which is the intended
behaviour on any other database.

## Full local pipeline, 2026-09-12

`scripts/telco-source/run_phases.ps1` now runs Phases 1–5, **4b** and **10**,
still with no code changes — only environment variables and output paths.
Phases 6–9 are excluded on purpose: they need AWS credentials and would bill a
second RDS target. Result of the run (collector run `cc213652`):

| Phase | Result |
|---|---|
| 1 Discover | two runs, ~23 s each; verify **5/5** |
| 2 Assess | 33 issues, 99 occurrences; recall **14 of 14**, severity exact |
| 3 Size & Edition | EE BYOL, 1 processor licence, capacity floor (no utilization feed) |
| 4 Remediate | 3 template fixes (2 × `PERF-001`, `PERF-004`) blocked only on the dry run — no rehearsal copy of this estate exists; 91 need a person; 2 database-level static entries served (see `phase-04-remediate.md`) |
| 4b Convert PL/SQL | **6 of 7 convertible objects compiled** on PostgreSQL 16 and rolled back; `SP_RATE_CDR_BROKEN` excluded (broken on the source); spec absorbed |
| 5 Blocker gate | HALT; `migrate_cdc` and `cutover` blocked (`DQ-001`, `OPS-001`, `OPS-002`); **full load and provision clear** — unlike `DBMIG_APP`, there is no external table |
| 10 Report | stored code 86% automatic; 34 action items (2 simple, 17 medium, 13 complex, 2 decisions); 12 DMS checks: 1 pass, 6 warning, 3 fail, 2 info; full load possible, CDC blocked |

Outputs under `telco-output/`, including `convert/plpgsql/` and
`report/migration_report.html`. A rehearsal copy of this estate would lift the
three dry-run blocks; it is not built because the XE user-data cap leaves no
room for a second 5 GB schema (see *Why a dedicated tablespace* above).
