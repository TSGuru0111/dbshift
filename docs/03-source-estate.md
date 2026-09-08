# Source estate reference

The synthetic on-premises Oracle database that everything else reads.
**Status: built and verified.** Rebuild from `data/dbmig_golden.dmp`, not from
the SQL scripts, unless you specifically need a fresh build.

## Connection

```
Host:     localhost
Port:     1521
Service:  XEPDB1          <-- service name, NOT the SID (XE)
User:     dbmig_app
Password: DbMig2026App
```

**Critical:** connecting to service `XE` lands you in `CDB$ROOT`, the container
root, where `CREATE USER` fails with ORA-65096. Always use `XEPDB1`. Verify with:

```sql
SELECT sys_context('USERENV','CON_NAME') FROM dual;   -- must return XEPDB1
```

## Environment

- Oracle Database 21c Express Edition (21.3.0.0.0), Windows native install
- XE limits: 12 GB user data, 2 GB RAM, 2 CPU threads
- Objects live in the built-in `USERS` tablespace (no custom tablespace —
  removed deliberately to avoid OS-specific datafile path problems)

## Accounts

| User | Password | Purpose |
|---|---|---|
| `dbmig_app` | `DbMig2026App` | Owns every application object |
| `dbmig_rpt` | `DbMig2026Rpt` | Owns the database link; proves cross-schema objects |
| `dbmig_collector` | `DbMig2026Coll` | **Read-only.** `SELECT_CATALOG_ROLE` + `CREATE SESSION` only. The discovery collector must use this account. |

## Size and object census (verified)

**1.032 GB**, 90 objects in `USER_OBJECTS`, **51 constraints**.

Re-verified by the discovery collector on 2026-09-08 against `DBA_*` views.

| Object type | Count |
|---|---|
| INDEX | 31 |
| TABLE | 21 |
| LOB | 6 |
| SEQUENCE | 5 |
| TABLE PARTITION | 4 |
| INDEX PARTITION | 4 |
| TYPE | 3 |
| VIEW | 3 |
| QUEUE | 2 |
| SYNONYM | 2 |
| PROCEDURE | 2 |
| PACKAGE / PACKAGE BODY | 1 each |
| DATABASE LINK | 1 |
| TRIGGER | 1 |
| JOB | 1 |
| FUNCTION | 1 |
| MATERIALIZED VIEW | 1 |

Constraints: **11** primary key (P), 5 foreign key (R), 1 unique (U),
32 check (C), 2 object-type (O, auto-generated for object columns).

Only **5 of the 11 primary keys are user-named** business keys (`CUSTOMER`,
`LOAN`, `LOAN_TXN`, `PAYMENT_HIST`, `COMM_LOG`). The other 6 are
system-generated on Oracle-managed infrastructure: the AQ queue tables, the
`DR$IX_COMM_NOTES_TEXT$*` Oracle Text index internals, `LOAN_NOTICE_XML`, and
`"ORDER"`. **Assessment rules must not treat `DR$`, `AQ$`, `MLOG$` and `SYS_IOT`
objects as user objects to migrate** — they are rebuilt by recreating the Text
index, the queue and the mview log, not moved.

### Why an earlier census said 50 constraints and 19 tables

The Phase 0 figures were taken **before** `04_seed_defects.sql` ran. That script
creates `"ORDER"` with an inline `PRIMARY KEY`, which is the 51st constraint
(`SYS_C008270`) and the 11th PK. The 90-object count was taken *after* seeding,
so the old numbers were never internally consistent. Post-seed values above are
authoritative.

## Row counts

| Table | Rows | Notes |
|---|---|---|
| `loan_txn` | ~3,000,000 | Range-partitioned by date, 4 partitions |
| `payment_hist` | 1,800,001 | The extra row is defect #4, an orphaned FK |
| `comm_log` | 400,000 | Has a CLOB; drives most of the byte count |
| `loan` | 120,000 | |
| `customer` | 60,000 | |
| `collateral_note` | 20,000 | No primary key (defect #3 target) |
| `audit_scratch` | 2 | No primary key (defect #2) |

## Objects that will NOT appear in USER_OBJECTS

Do not treat these as missing — they exist but are owned elsewhere or are not
schema objects at all:

- **Constraints** — in `USER_CONSTRAINTS`, they are table attributes
- **Directory** (`DBMIG_EXT_DIR` -> `C:\oracle\ext_data`) — owned by SYS
- **XML schema** (`http://dbmig.example.com/loan_notice.xsd`) — in
  `DBA_XML_SCHEMAS`, not in `USER_OBJECTS`. The URL is an identifier only;
  Oracle never fetches it.
- **Users, roles, grants, profile** — database-level

### Corrected 2026-09-08 by collector output

- **The database link *does* appear** in `USER_OBJECTS` as type `DATABASE LINK`,
  and `DBMIG_LOOPBACK_LNK` is owned by **`dbmig_app`**, not `dbmig_rpt`. It is
  one of the 90 objects.
- **The XML schema is registered under owner `DBMIG_APP`**, not XDB.
- **`dbmig_rpt` owns no objects at all** — it exists as a grantee only. Nothing
  reads it today; keep it in the collected schema list so cross-schema grants
  stay visible.
- `PDBADMIN` is also flagged `ORACLE_MAINTAINED='N'` but is a PDB
  administration account, not an application schema. The collector reports it as
  discovered-but-not-collected rather than silently including it.

## Golden snapshot

`data/dbmig_golden.dmp` — Data Pump export taken *after* defect seeding.
Restore with `impdp`. Export command for reference:

```
expdp dbmig_app/DbMig2026App@localhost:1521/XEPDB1 schemas=DBMIG_APP ^
  directory=DBMIG_EXT_DIR dumpfile=dbmig_golden.dmp logfile=dbmig_golden_exp.log
```

`expdp` refuses to overwrite an existing dumpfile — delete the old one first.
Export takes ~5 minutes.
