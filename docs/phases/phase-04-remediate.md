# Phase 4 — Detect & Remediate

> **Latest update — 2026-09-10.** Structure built and added to the console.
> **This phase plans fixes and applies nothing.** Two of the five gates
> report `BLOCKED` because the infrastructure they need does not exist, and model
> generation is unavailable because Bedrock `InvokeModel` is blocked on this
> account. On the reference estate: 66 findings planned, 2 with SQL, 61 routed to
> a human, 3 that are decisions rather than statements.

## Purpose

Turn findings into **fixes that are safe to apply**. The second half is the whole
problem — generating SQL is easy, proving it is safe to run on someone's
production database is not.

## What actually happens

`remediate/plan.py :: build(assessment)` — per finding:

1. **Route by level.** `policy.classify()` maps the remediation level, which came
   from the assessment rule and is not re-judged here:
   - `L1` → auto-apply · `L2` → needs approval · `L3` → a human authors it ·
     `L4` → never fixed, it is a decision not a statement
2. **Source a fix** — `remediate/generate.py`, in order:
   - **template** — deterministic, no model, no cost
   - **bedrock** — for findings with no template. **Not wired**
   - **none** — routed to a human
3. **Run the five gates** — `remediate/gates.py`, stopping at the first failure:
   `static → policy → syntax → dry run on rehearsal → approval`
4. **Record the outcome**: `AUTO_APPLY`, `READY_TO_APPLY`, `BLOCKED`, `REJECTED`,
   `MANUAL_ACTION_REQUIRED`, or `NOT_A_FIX`.

### The gates

| Gate | What it does |
|---|---|
| **static** | Single statement, names its target, carries a rollback |
| **policy** | The prohibitions. Never drops a production object, deletes rows, alters privileges or rewrites business logic — whatever level it carries |
| **syntax** | **Offline only.** Oracle executes DDL at parse time, so `DBMS_SQL.PARSE` on a `CREATE INDEX` would create the index. Real parsing belongs on the rehearsal copy |
| **dry run** | Apply and roll back on a restored copy. **BLOCKED** — no rehearsal database exists |
| **approval** | L1 carries its own by policy; everything else needs a named human |

### The templates

Findings with exactly one correct remedy, where a model adds cost and risk but no
information:

| Rule | Fix | Rollback |
|---|---|---|
| `PERF-001` | `CREATE INDEX` on the FK column | `DROP INDEX` |
| `DQ-007` | `DBMS_STATS.GATHER_TABLE_STATS` | `RESTORE_TABLE_STATS` — statistics are derived, so "undo" means restoring the previous set |
| `PERF-004` | `ALTER INDEX … REBUILD`, only when genuinely unusable | back to `UNUSABLE` |
| `DQ-009` | **declines** — the finding does not carry the column's declared length, and guessing would truncate data |

A template declining is a routing decision, not a failure.

## Inputs / Outputs

| | |
|---|---|
| Input | `assess/output/assessment.json` |
| Input | `DBSHIFT_REHEARSAL_DSN` (optional, not yet used for anything) |
| Output | `remediate/output/remediation_plan.json` |

## Design decisions

**A plan you can read beats an apply you cannot audit.** Nothing here touches a
database. Applying is a separate step that does not exist yet, deliberately.

**Every fix carries rollback SQL or it is rejected at generation time** — not at
apply time, by which point it is too late to ask.

**Two gates report BLOCKED rather than passing vacuously.** A gate that always
passes is not a gate, and a plan that looks approved because a check was skipped
is worse than no plan.

**A model-authored fix gets no shortcut.** When Bedrock is wired it must return
the same shape a template does and passes exactly the same five gates.

**`allow_model` is pinned false on the server side**, not merely defaulted — the
console cannot claim a model produced a fix while invoke is blocked.

## Known limits

- **Nothing can be applied.** No rehearsal database, so no fix can be proven safe.
- **61 of 66 findings need a human**, because model generation is unavailable.
  That number falls when Bedrock is wired; it is not a defect in the router.
- **No retry loop yet.** `policy.MAX_ATTEMPTS` is 3 and recorded, but a failed
  generation is not retried — there is nothing to retry into.
- **The two `OPS` criticals are source-side.** `ALTER DATABASE ARCHIVELOG` needs a
  restart. That is a maintenance window, not something an agent applies.

## How to run it

```bash
python -m remediate.run
python -m remediate.run --rehearsal-dsn host:1521/SERVICE
```

Console: **Phase 4 - Remediate**.

## What unblocks it

1. **A rehearsal database** — restore `data/dbmig_golden.dmp` into a second
   instance and set `DBSHIFT_REHEARSAL_DSN`. Needs no Bedrock, and moves the two
   drafted fixes from `BLOCKED` to applyable.
2. **Bedrock Marketplace access** — moves the 61 toward drafted fixes.

These are independent; neither waits on the other.

## Change log

**2026-09-10** — Console stage added. Policy statement-counting fixed: it counted
semicolons, so a PL/SQL block (`BEGIN …; END;`) was rejected as "more than one
statement", which refused every valid `DBMS_STATS` fix.

**2026-09-10 (earlier)** — Built. Verified with six hostile statements —
`DROP TABLE`, `DELETE`, `GRANT`, `CREATE OR REPLACE PACKAGE`, a missing rollback,
and a fix naming the wrong object — all rejected; only the legitimate one reached
the dry-run gate.
