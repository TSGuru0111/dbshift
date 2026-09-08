# Build log

Append-only. Records what was done, what broke, and why things are the way they
are. Read this before assuming a design choice was arbitrary.

---

## Phase 0 — Source estate build (complete)

**Outcome:** Oracle 21c XE, `dbmig_app` schema, 1.031 GB, 90 objects,
50 constraints, 8 seeded defects, golden dump exported.

### Problems hit and how they were resolved

**CDB vs PDB.** Connecting SQL Developer as SYS to service `XE` landed in
`CDB$ROOT`. Every `CREATE USER` failed with ORA-65096 (common users in the
container root must be named `C##something`). *Fix: connect to service name
`XEPDB1`, not SID `XE`.* This cost the most time of anything in the build.

**SYS not required.** The admin script was rewritten to run as `SYSTEM` with
the default role rather than SYS as SYSDBA. `SYSTEM` has the DBA role, which
covers everything needed.

**Custom tablespace removed.** The original script created a dedicated
tablespace with a bare datafile name, which fails on a native Windows install
without `DB_CREATE_FILE_DEST` set. *Fix: use the built-in `USERS` tablespace and
just extend its autoextend ceiling.* No OS-specific paths anywhere now.

**Passwords simplified.** Original passwords contained `#`, which causes
trouble in SQL*Plus-style script execution. Changed to plain alphanumeric.

**`DBMS_XMLSCHEMA.registerSchema` signature mismatch.** Failed with PLS-00306.
Root cause found by querying `ALL_ARGUMENTS`: `SCHEMADOC` is typed `VARCHAR2`
in 21c, but the script declared the XSD as a `CLOB`. *Fix: declare as
`VARCHAR2(4000)`.*

**BINARY XML storage rejected the schema** (ORA-44424) — binary storage
requires a schema registered specifically for binary use. *Fix: `STORE AS CLOB`
instead.*

**`GRANT EXECUTE ON DBMS_XMLSCHEMA` failed** with ORA-01031 — that package is
owned by XDB and SYSTEM cannot grant on it. Worked around; not needed in the end.

**Substitution variables silently broke the data load.** The data script used
`&cust_n` style DEFINE variables while also setting `SET DEFINE OFF`. Result:
PLS-00103 on every block and **zero rows loaded**, while the tail of the script
still reported success. *Fix: removed substitution variables entirely; row-count
targets are now hardcoded `v_total` values edited in place.*

**Partial script execution.** Pressing F5 with text highlighted in SQL Developer
runs only the selection. This produced a run where the final PROMPT block
appeared but no data loaded. *Lesson: always Ctrl+Home and confirm nothing is
selected before F5 — and verify with a COUNT query rather than trusting the
output pane.*

**`DBA_SEGMENTS` not visible to `dbmig_app`** (ORA-00942) — it lacks
`SELECT_CATALOG_ROLE`. *Fix: use `USER_SEGMENTS` for size checks.*

**Dependent objects invalidated by defect 6.** Adding `loan.legacy_score`
invalidated `MV_LOAN_SUMMARY`, `SP_CLOSE_LOAN` and `PKG_LOAN_OPS` body. Normal
Oracle dependency behaviour, cleared with explicit `COMPILE` statements.

**`expdp` is not a SQL statement.** Pasting it into a SQL Developer worksheet
returns "Unknown Command". It runs from Windows Command Prompt.

### Verification performed

- `USER_OBJECTS` census: 90 objects across 17 types
- `USER_CONSTRAINTS`: 10 P, 5 R, 1 U, 32 C, 2 O
- `USER_SEGMENTS`: 1.031 GB
- Database link tested with a live cross-schema query returning 60000
- Exactly one INVALID object (`SP_BROKEN_DEMO`) — the intended one
- Export completed in 4m49s, all row counts matched

---

## Phase 1 — Discovery collector (complete) — 2026-09-08

**Outcome:** `collector/` runs read-only as `dbmig_collector`, oracledb thin
mode, and writes 45 JSON datasets (1,437 rows) to `collector/output/<run_id>/`
in ~9.5 s. No AWS dependency. `collector/verify.py` scores a pair of runs
against ground truth: **5/5 checks pass.**

### What was built

- `config.py` — password from `DBSHIFT_COLLECTOR_PASSWORD` only; never a file
- `db.py` — the single logged query path. Every call records label, SQL,
  SHA-256 of the SQL, row count and elapsed ms. Data and credentials are
  never logged. Schema names are **bound**, never interpolated
- `probes/` — 11 modules: identity, objects, tables, indexes, constraints,
  storage, partitions, plsql, programmatic, security, features
- `writer.py` — one file per dataset under a shared envelope
- `run.py` / `verify.py` — collect, then reconcile

### Output shape and why

One file per dataset, each carrying `collector_run_id`, `dataset`,
`schema_version`, `source_queries` and a flat `rows` list whose keys match the
future `source_inventory.*` column names. `collector_run_id` is stamped on every
row as well as the envelope, so a row can be inserted independently of its file
— that is what makes the append-only rule work end to end. The later AWS push
step is `for file in run_dir: POST`, with the ingest Lambda routing on
`dataset`. No transform layer, no restructuring.

`output/runs.jsonl` is an append-only local ledger mirroring the same rule.

### Problems hit and how they were resolved

**`VIRTUAL_COLUMN` invalid identifier (ORA-00904).** `DBA_TAB_COLUMNS` has
neither `HIDDEN_COLUMN` nor `VIRTUAL_COLUMN`; those exist only on
`DBA_TAB_COLS`. *Fix: query `DBA_TAB_COLS` with `user_generated = 'YES'`, which
keeps the virtual/hidden flags — a virtual column cannot be inserted into on the
target — while excluding Oracle's internal LOB and object-type columns.*
Caught because a failed query is recorded and surfaced in the run summary rather
than swallowed; the first run exited non-zero and named the query.

**`V$SYSMETRIC` is empty on 21c XE** (0 rows, no error). Utilization
percentiles are therefore not obtainable from this source. *Fix: collect
`V$OSSTAT` (`NUM_CPUS = 8`, busy/idle time) as a point-in-time snapshot and
record AWR coverage separately.* AWR views do exist here with 3 snapshots, but
XE is not licensed for Diagnostic Pack and 3 snapshots is not a percentile —
**the sizing engine must not claim percentile-based utilization from this
estate.** On a production EE source, querying `DBA_HIST_*` carries Diagnostic
Pack licensing implications and should be an explicitly gated probe.

**`DBA_FEATURE_USAGE_STATISTICS` was already sampled** (496 rows, last sample
2026-09-08 20:48) — the anticipated need to force a sample as SYS did not
arise. **`Partitioning (user)` is the single edition-forcing feature detected**
(`detected_usages = 1`, `currently_used = TRUE`). No TDE, no Advanced
Compression. The SE2-vs-EE verdict will therefore rest on partitioning alone.

### Reference-doc drift found and corrected

The collector contradicted `03-source-estate.md` in four places. All four were
verified against the live database and the doc has been corrected:

| Claim | Documented | Actual |
|---|---|---|
| Constraints | 50 | **51** (11 PKs, not 10) |
| `TABLE` / `INDEX` objects | 19 / 30 | **21 / 31** |
| `DBMIG_LOOPBACK_LNK` owner | `dbmig_rpt`, absent from `USER_OBJECTS` | **`dbmig_app`, and present** |
| XML schema owner | XDB | **`DBMIG_APP`** |

Root cause of the constraint drift: the Phase 0 census was taken **before**
`04_seed_defects.sql` ran. That script creates `"ORDER"` with an inline
`PRIMARY KEY` (`SYS_C008270`) — the 51st constraint. The 90-object count was
taken after seeding, so the old figures were never internally consistent.

Only 5 of the 11 PKs are user-named business keys. The rest sit on Oracle-managed
internals (`AQ$`, `DR$IX_COMM_NOTES_TEXT$*` Oracle Text tables, `SYS_IOT_TOP_*`).
**The assessment engine must exclude those from "objects to migrate"** or it will
report phantom findings — noted in `03-source-estate.md`.

### Verification performed

Two runs, `2309a50d-…` and `be14610a-…`:

| Check | Result |
|---|---|
| Two distinct `collector_run_id` values | PASS |
| Object count vs live `DBA_OBJECTS` | 90 / 90 / live 90 — PASS |
| Constraint count vs live `DBA_CONSTRAINTS` | 51 / 51 / live 51 — PASS |
| PL/SQL SHA-256 identical across runs | 9 objects, 0 drifted — PASS |
| `DBA_FEATURE_USAGE_STATISTICS` queried | 496 rows both runs — PASS |

`verify.py` reconciles against a **fresh independent query**, not against the
collector's own output, and reports documented-vs-actual drift separately rather
than treating the doc as authoritative.

Also confirmed: exactly one INVALID object (`SP_BROKEN_DEMO`), matching the
`04-defects.md` regression check.

### Open for the next phase

- `dbmig_rpt` owns nothing; the cross-schema story rests on grants and the
  `dbmig_app`-owned link. Revisit if a cross-schema object is needed.
- No push step yet, by design — the envelope is shaped for it, but no AWS
  account exists.

---

## Phase 2 — Assessment engine (not started)

<!-- Append entries here as work proceeds -->
