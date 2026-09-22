# Oracle source estate build scripts

Run in order. **Only needed for a fresh build** — normally restore from
`../../data/dbmig_golden.dmp` instead, which is far faster and already contains
the seeded defects.

| Script | Run as | Notes |
|---|---|---|
| `01_setup_admin.sql` | `system` @ **XEPDB1**, default role | Users, roles, profile, directory. Edit the `C:\oracle\ext_data` path if yours differs — create the folder first. |
| `02_schema_objects.sql` | `dbmig_app` | Every object type. |
| `03_generate_data.sql` | `dbmig_app` | ~1 GB. Row targets are hardcoded `v_total` values — edit in place. Takes 20-40 min on XE. |
| `04_seed_defects.sql` | `dbmig_app` | The 8 defects. See `../../docs/04-defects.md`. |

## After 04, clear incidental invalidations

Adding `loan.legacy_score` invalidates dependent objects. This is normal:

```sql
ALTER PROCEDURE sp_close_loan COMPILE;
ALTER PACKAGE pkg_loan_ops COMPILE BODY;
ALTER MATERIALIZED VIEW mv_loan_summary COMPILE;
```

Then confirm exactly one INVALID object remains (`SP_BROKEN_DEMO`).

## Also needed (run as `dbmig_rpt`)

```sql
CREATE DATABASE LINK dbmig_loopback_lnk
  CONNECT TO dbmig_app IDENTIFIED BY DbMig2026App
  USING '(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=localhost)(PORT=1521))
         (CONNECT_DATA=(SERVICE_NAME=XEPDB1)))';
```

## Gotchas

- **F5, nothing highlighted.** A selection runs only the selection.
- Verify with `COUNT(*)`, not the output pane.
- Service name `XEPDB1`, never SID `XE`.
