# Phase 4c — Schema DDL for PostgreSQL

> **Latest update — 2026-09-14 (built and compiled for real).** `convert/ddl.py`
> generates the tables, keys, checks and indexes a PostgreSQL target needs, from
> what discovery recorded. **Every statement was executed against PostgreSQL 16
> inside a transaction that was rolled back**: 30 of 30 on `DBMIG_APP`, 52 of 52
> on `DBMIG_TELCO`, zero failures, with no code changes between the two estates.
> Self-test `convert/selftest_ddl.py` **39/39**, including the live compile.
>
> **Update — 2026-09-18.** `convert/ddl_apply_run.py` gives `ddl_apply.py` a
> command line, so applying 4c's schema to a real target is now a repeatable
> step rather than an inline script rewritten on the day. `ddl_run` itself still
> applies nothing: it compiles into a transaction and rolls back. Only
> `ddl_apply_run --apply` writes, and it asks for the target DSN typed back and
> a named approver, like `convert.apply_run`.

## Purpose

Create the target schema properly, rather than letting DMS improvise it.

DMS will create missing tables itself, using a fixed type mapping that knows
nothing about the estate: every `NUMBER` becomes `numeric`, whatever its
precision. It creates **no constraints and no indexes at all**. So a migration
that relies on DMS for DDL arrives with no primary keys, no referential
integrity, no access paths, and a `NUMBER(1)` flag stored as an
arbitrary-precision decimal.

This phase produces DDL from the same discovery data the rest of the pipeline
uses, with the type mapping Phase 4b already relies on.

## What actually happens

`convert/ddl.py :: build()`

1. **Tables are selected** by the same function Phase 7 uses
   (`dms.mappings.select_tables`), so what gets DDL and what gets data cannot
   disagree. Oracle internals, materialized views, external tables and queue
   tables are held back.
2. **Columns** are mapped through `convert/typemap.py` — the same mapping that
   decides `NUMBER(10)` is a `BIGINT`, not a `numeric`.
3. **Defaults** are translated where they have an equivalent and **declined
   with a reason** otherwise. A default that silently disappears is worse than
   one that is reported.
4. **Constraints** become keys and checks, grouped by when they may safely run.
5. **Indexes** are emitted, minus the ones a constraint creates on its own.
6. **Notes** record everything a reviewer must know, each with a severity.

## The order, and why it is not alphabetical

```
schema -> tables -> [ THE DATA LOAD ] -> primary/unique keys -> foreign keys
                                                             -> checks -> indexes
```

Constraints and indexes come **after** the load. Validating a foreign key row by
row during a bulk insert is the slowest possible way to do it, and an index
maintained during a load is rebuilt far faster afterwards. This is the same
order Phase 7's Data Pump path uses for the homogeneous case.

## The things that need a person, and why

| Note | What it means |
|---|---|
| `reserved_word` | `ORDER` is a legal Oracle name and a PostgreSQL keyword. Renamed to `order_col` rather than quoted forever — **application SQL must change** |
| `virtual_column` | computed, not stored. PostgreSQL has generated columns, but the expression is Oracle SQL |
| `check_not_translated` | the condition uses `SYSDATE`, `NVL`, `DECODE`… Translating a business rule wrongly either rejects valid data or admits invalid data |
| `index_expression` | a function-based index; the expression needs translating |
| `index_type` | a bitmap or domain index has no direct equivalent |
| `unmappable_type` | `ROWID`, `BFILE` — no meaning on another engine. **Severity error**: the table is incomplete |
| `depends_on_types` | columns typed as a user-defined type. Phase 4b converts those, and they must exist first |

## Design decisions

- **Reserved words are renamed, not quoted.** Quoting works, but then every
  hand-written query must quote it forever, including ones nobody has written
  yet. Renaming is a one-time application change that is visible.
- **Sequence defaults are translated.** Oracle's `"OWNER"."SEQ"."NEXTVAL"`
  becomes `nextval('owner.seq')`. Declining it would leave the column with no
  default, so every insert relying on it would fail — and Phase 7's residue
  already resets those sequences to the source's current value.
- **Check constraints are declined conservatively.** A business rule translated
  wrongly is worse than one reported as needing a person.
- **Queue tables are held back.** `LOAN_EVENT_QTAB` has no `AQ$` prefix and
  looks like an ordinary application table, but its columns are Oracle AQ
  object types that exist nowhere else.

## Known limits

- **Partitioning is not converted.** A partitioned Oracle table becomes an
  ordinary PostgreSQL table. Declarative partitioning exists but the syntax and
  maintenance model differ; Phase 3 counts this as effort on the PostgreSQL path.
- **Grants are not generated.** Privileges are a security decision.
- **Nothing is applied.** `--compile` proves the DDL runs and rolls it back.

## How to run it

```powershell
python -m convert.ddl_run                      # write the plan
python -m convert.ddl_run --compile            # run it on PostgreSQL, then ROLL BACK
python -m convert.ddl_run --run <id> --owner DBMIG_TELCO --compile
python -m convert.selftest_ddl                 # 39 checks, incl. the live compile
```

Writes `convert/output/schema_ddl.json` and a readable `schema.sql`.

**Applying it to a real target** (this writes; everything above does not):

```powershell
$env:DBSHIFT_PG_PASSWORD = '...'
python -m convert.ddl_apply_run --pg-dsn <host>:5432/dbshift --pg-user dbshiftadm
#   preflight only -- prints the statements, checks their shape, applies nothing

python -m convert.ddl_apply_run --apply --pg-dsn <host>:5432/dbshift     --pg-user dbshiftadm --confirm <host>:5432/dbshift --approved-by you@example.com
#   pre-load: the schema, its sequences and types, then the tables

#   ... then Phase 7 loads the rows, and only afterwards:
python -m convert.ddl_apply_run --apply --post-load --pg-dsn <host>:5432/dbshift     --pg-user dbshiftadm --confirm <host>:5432/dbshift --approved-by you@example.com
#   post-load: the keys, checks and indexes
```

**The two passes are not interchangeable.** Pre-load stops after the tables
because validating a foreign key row by row during a bulk load is the slowest
possible way to do it. Records go to `convert/output/ddl_apply_record.json` and
`ddl_apply_post_record.json` — the same two filenames the inline script used, so
the existing 19- and 41-statement records from the real RDS apply are continued
rather than orphaned.

## Change log

**2026-09-18 — `ddl_apply_run.py`, a command line for the apply.** `ddl_apply.py`
had no CLI: Phase 4c's apply to the live RDS target was driven by a `python -c`
script written fresh each time, which is the kind of step that gets done
slightly differently on the day it matters. The module was always the careful
part — four rules, one transaction, a re-checked target — and this only adds the
door to it, with `--post-load`, `--approved-by` and `--confirm`.

Two things it does beyond wrapping the call:

- **Statement-shape violations are reported before a connection is opened**, so
  a defect in the DDL generator does not arrive looking like a database error.
- **It reuses the record filenames the inline script wrote**, because new names
  would have orphaned the existing pre- and post-load records from the real RDS
  apply and made it look as though 4c had never been applied to a target.

`convert.selftest_ddl_apply` still **45/45**; `convert.selftest_ddl` **39/39**.

**2026-09-14 — built.** `convert/ddl.py`, `convert/ddl_run.py`,
`convert/selftest_ddl.py`. `dms.mappings.select_tables` gained a `queues`
argument so both phases hold back the same tables.

Running the DDL rather than reading it found three defects that would each have
produced a broken target:

- **Five sequence defaults were being dropped.** `"DBMIG_APP"."SEQ_LOAN_ID"."NEXTVAL"`
  was not recognised, so `LOAN_ID`, `CUSTOMER_ID` and three others would have
  arrived with no default and every insert relying on one would have failed.
- **The AQ queue table was included.** `LOAN_EVENT_QTAB` carries no `AQ$`
  prefix, so the internal-table rule let it through; its columns are AQ object
  types and the `CREATE TABLE` failed. Fixed in the shared selection, so Phase 7
  no longer tries to migrate its data either.
- **Tables using a user-defined type failed.** `CUSTOMER` needs `TY_ADDRESS`,
  which Phase 4b converts. The plan now declares `depends_on_types` instead of
  emitting DDL that cannot run.

The first run was 24 statements with 10 failures. After the fixes: 30 of 30 on
`DBMIG_APP`, 52 of 52 on `DBMIG_TELCO`.
