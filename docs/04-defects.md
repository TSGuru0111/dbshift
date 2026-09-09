# Seeded defects — ANSWER KEY

**This is the test spec.** Any assessment or detection work is measured against
this table. Eight defects were deliberately introduced by
`scripts/oracle-source/04_seed_defects.sql` after the clean build.

| # | Defect | Object | Expected finding | Expected severity |
|---|---|---|---|---|
| 1 | Reserved-word identifier | Table and column both named `"ORDER"` | Identifier collision; fails unquoted on most targets | HIGH |
| 2 | No primary key | `audit_scratch` | DMS CDC cannot reliably replicate a table with no PK | CRITICAL |
| 3 | Unindexed foreign key | `collateral_note.loan_id` | Missing index on FK; performance and locking risk | MEDIUM |
| 4 | Orphaned FK reference | `payment_hist` row with `loan_id = 999999999` | Referential integrity violation; constraint is ENABLE NOVALIDATE to permit it | HIGH |
| 5 | Duplicate should-be-unique value | `customer.national_id = '000000001'` on 2 rows | Natural key with no uniqueness enforced | MEDIUM |
| 6 | Unconstrained NUMBER | `loan.legacy_score` | No precision or scale; mapping risk to a fixed-width target type | MEDIUM |
| 7 | Non-standard character | `customer.full_name` where `customer_id = 3` | CHR(146) cp1252 artifact; encoding risk on migration | LOW | ⚠️ **NOT PRESENT — see below** |
| 8 | Invalid object | `sp_broken_demo` | Fails to compile (PLS-00049); must resolve before migration | HIGH |

## ⚠️ Defect 7 was never successfully seeded

Confirmed 2026-09-08 by the assessment engine and verified directly against the
database. **`04_seed_defects.sql` line 52 does not do what it says.**

```sql
UPDATE customer SET full_name = full_name || CHR(146) WHERE customer_id = 3;
```

This database is **AL32UTF8**, where byte `0x92` is a bare UTF-8 continuation
byte, not a character. `CHR(146)` returns NULL, `full_name || NULL` is
`full_name`, and the UPDATE reported one row modified having changed nothing.

Proof:

```sql
SELECT DUMP(full_name,1016) FROM customer WHERE customer_id = 3;
-- Typ=1 Len=13 CharacterSet=AL32UTF8: 46,61,74,69,6d,61,20,53,68,61,72,6d,61
SELECT COUNT(*) FROM customer WHERE INSTR(full_name, CHR(146)) > 0;   -- 0
```

**To seed it properly**, use the character cp1252 `0x92` actually maps to —
U+2019 RIGHT SINGLE QUOTATION MARK:

```sql
UPDATE customer SET full_name = full_name || UNISTR('\2019') WHERE customer_id = 3;
COMMIT;
```

Until that runs, the maximum achievable recall is **7 of 8**. The engine reports
defect 7 as `N/A — not present in source` and excludes it from the recall
denominator rather than counting it as a miss, so the figure stays honest in
both directions.

## Regression check

```sql
SELECT object_name, object_type, status
FROM   user_objects
WHERE  status = 'INVALID';
```

Must return **exactly one row**: `SP_BROKEN_DEMO`. Anything else means
something broke unintentionally.

Note: adding `loan.legacy_score` (defect 6) invalidates dependent objects —
`MV_LOAN_SUMMARY`, `SP_CLOSE_LOAN`, `PKG_LOAN_OPS` body. That is normal Oracle
dependency invalidation, not a defect. Clear it with:

```sql
ALTER PROCEDURE sp_close_loan COMPILE;
ALTER PACKAGE pkg_loan_ops COMPILE BODY;
ALTER MATERIALIZED VIEW mv_loan_summary COMPILE;
```

## Scoring the assessment engine

After any assessment run, produce:

```
Detected:        N of 8
False positives: N
Missed:          N  (list which, and at what severity)
```

That recall figure is the project's headline metric. Record each run's score in
`07-build-log.md` so regressions are visible.
