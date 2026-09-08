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

## Phase 1 — Discovery collector (not started)

<!-- Append entries here as work proceeds -->
