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
byte, not a character. The concatenation is a silent no-op and the UPDATE
reported one row modified having changed nothing.

Proof:

```sql
SELECT DUMP(full_name,1016) FROM customer WHERE customer_id = 3;
-- Typ=1 Len=13 CharacterSet=AL32UTF8: 46,61,74,69,6d,61,20,53,68,61,72,6d,61
SELECT COUNT(*) FROM customer WHERE INSTR(full_name, CHR(146)) > 0;   -- 0
```

### Correction — 2026-09-10: `CHR(146)` does not return NULL

An earlier revision of this file said `CHR(146)` returns NULL and that
`full_name || NULL` is therefore `full_name`. **That mechanism is wrong**, though
the conclusion — defect 7 was never seeded — is right. Re-measured directly:

```sql
SELECT CASE WHEN CHR(146) IS NULL THEN 'NULL' ELSE 'NOT NULL' END,  -- NOT NULL
       LENGTH(CHR(146)), LENGTHB(CHR(146)),                         -- 1 char, 1 byte
       DUMP(CHR(146), 1016)          -- Typ=1 Len=1 CharacterSet=AL32UTF8: 92
FROM dual;
```

`CHR(146)` returns a one-byte value holding `0x92`. What actually happens is
that the invalid byte is **dropped during concatenation**:

```sql
SELECT DUMP('abc' || CHR(146), 1016) FROM dual;
-- Typ=1 Len=3 CharacterSet=AL32UTF8: 61,62,63     <- the 0x92 is gone
```

So the result is a genuine no-op, `INSTR(...) > 0` finds nothing, and DQ-003
cannot fire — but because the byte is discarded on concatenation, not because
`CHR(146)` is NULL. The distinction matters for anyone re-deriving this: testing
`CHR(146) IS NULL` returns false and would wrongly suggest the seed had worked.

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
