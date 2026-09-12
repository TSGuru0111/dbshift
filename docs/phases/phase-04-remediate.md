# Phase 4 — Detect & Remediate

> **Latest update — 2026-09-11 (static stand-ins).** Bedrock invoke is still
> blocked, so the model seam is now filled by **hand-written static output**,
> `bedrock/static/DBMIG_APP.json`, served under `model_mode="static"`. It is
> labelled everywhere as `source: static_fixture`, `model_id: null`, and the plan
> reports `model_generation_enabled: false` — a static entry is not a model and
> never claims to be. Result on run `18c1809a`: all 25 L2 no-template findings
> answered. **2 are SQL** (primary keys for `DQ-001`), both passed the real dry run
> on the rehearsal copy and stop only at approval, as L2 must. **23 are advice**
> (new status `ADVICE_DRAFTED`) — DMS LOB settings, table exclusions, target
> runbook steps, a provisioning parameter — because most of what a model would
> say about these findings is not SQL on the source at all. One policy change,
> deliberately narrow: a rollback may drop a constraint **only if its own fix
> added that exact constraint**.
>
> **Previous — 2026-09-10 (rehearsal live).** The dry-run gate now runs
> against a real restored copy, `DBMIG_REHEARSAL` (85 objects, 1,079 MB). Both
> drafted fixes moved `BLOCKED → AUTO_APPLY` with all five gates green:
> `DQ-007` applied in **5,704 ms** and rolled back in **1,254 ms**; `PERF-001`
> applied in **65 ms** and rolled back in **110 ms**. Verified afterwards that
> `DBMIG_APP` still holds 90 objects and its `PAYMENT_HIST` statistics still date
> from 2026-09-08 — the source was not touched. On the reference estate: 66
> findings planned, 2 with SQL, 61 routed to a human, 3 that are decisions rather
> than statements. **The phase still applies nothing to production**; it now
> proves what it would apply.

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
2. **Source a fix** — `remediate/generate.py`, in order, governed by `model_mode`:
   - **template** — deterministic, no model, no cost. Always tried first.
   - **static_fixture** — `model_mode="static"` (the CLI and console default).
     `bedrock/static.py` looks up a hand-written entry for **exactly** this rule,
     owner and object. No entry → routed to a human. An entry whose recorded
     `written_against` no longer matches the live finding detail → refused as
     stale and routed to a human, so a changed estate never receives an answer
     to a question it is no longer asking. An entry may carry SQL (then it takes
     all five gates) or only advice (then it becomes `ADVICE_DRAFTED`).
   - **bedrock** — `model_mode="live"`. **Not wired**; invoke is blocked.
   - **none** — `model_mode="off"`, or nothing above produced anything.

   L3 and L4 return before any of these are tried — see Known limits.
3. **Run the five gates** — `remediate/gates.py`, stopping at the first failure:
   `static → policy → syntax → dry run on rehearsal → approval`
4. **Record the outcome**: `AUTO_APPLY`, `READY_TO_APPLY`, `BLOCKED`, `REJECTED`,
   `ADVICE_DRAFTED`, `MANUAL_ACTION_REQUIRED`, or `NOT_A_FIX`.
   `ADVICE_DRAFTED` means a recommendation exists but there is no statement to
   gate — a DMS setting, a step on the target, a parameter for Phase 6. Entries
   carry an `artefact` with `applies_to_phase` so later phases can consume them.

### The gates

| Gate | What it does |
|---|---|
| **static** | Single statement, names its target, carries a rollback |
| **policy** | The prohibitions. Never drops a production object, deletes rows, alters privileges or rewrites business logic — whatever level it carries |
| **syntax** | **Offline only.** Oracle executes DDL at parse time, so `DBMS_SQL.PARSE` on a `CREATE INDEX` would create the index. Real parsing belongs on the rehearsal copy |
| **dry run** | Apply and roll back on a restored copy. **Live** — see below |
| **approval** | L1 carries its own by policy; everything else needs a named human |

### The dry-run gate

`remediate/rehearsal.py`. Every fix is **remapped** off the source schema, then
applied and rolled back on the copy. Both must succeed.

The remap rewrites quoted identifiers (`"DBMIG_APP"."X"`) and quoted string
owners (`ownname => 'DBMIG_APP'`), then **re-checks that the source schema name
is gone entirely** and refuses if it is not. A half-remapped statement would run
against production, so a partial rewrite is an error rather than a best effort.
It also refuses outright when the rehearsal schema and source schema are the
same name.

A failed rollback after a successful apply returns `dirty: true` and fails the
fix — that is the case the gate exists to catch, and it also means the copy needs
re-importing before the next run.

### Building the rehearsal copy

`scripts/oracle-source/06_create_rehearsal.sql` (as `SYSTEM`), then
`06_rehearsal_import.par` via `impdp`. Both carry the reasoning inline.

**The copy lives in the same XE instance as the source.** That is a deliberate
compromise — a separate instance or an RDS target would isolate it properly, but
RDS Oracle is ~$343/month against a $100 credit. Same-instance is adequate for
schema-scoped DDL, which is what L1/L2 fixes are, and the policy gate already
forbids anything wider.

Three object kinds have **database-wide identity** and therefore cannot be
duplicated into a second schema in the same instance:

| Object | Why | Handling |
|---|---|---|
| Object types | 32-hex OID is unique per database (`ORA-02304`) | `transform=oid:n` mints fresh OIDs; structure is identical |
| XML schema | registered per-database against its URL; no remap exists (`ORA-00001` on `XDB.SYS_C006588`) | excluded, and `LOAN_NOTICE_XML` with it |
| Database link | needs `CREATE DATABASE LINK`, deliberately withheld | excluded — a sandbox that can reach other databases is not a sandbox |

Net: **85 of 90 objects**, all 4.5 M rows. The gap is exactly the link, the XML
table, and their dependent index/LOB.

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
| Input | `DBSHIFT_REHEARSAL_DSN` + `DBSHIFT_REHEARSAL_PASSWORD` — both required, or the gate reports `BLOCKED` |
| Input | `DBSHIFT_REHEARSAL_USER` / `_SCHEMA` / `DBSHIFT_PRIMARY_SCHEMA` (optional overrides) |
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

**The console pins `model_mode="static"`**, never `"live"`, so it cannot claim a
model produced a fix while invoke is blocked. (`allow_model` is still accepted by
`plan.build` so an old caller keeps its meaning — `True` maps to `"live"`.)

**Static output is labelled, not disguised.** It exists so the pipeline runs end
to end without Bedrock, and so every downstream phase can be built against real
shapes. It must never be presented as model output: every entry carries
`source: static_fixture` and `model_id: null`. If a client asks whether an AI
wrote a recommendation, the screen gives the true answer.

**One narrow policy exemption, 2026-09-11.** `DROP CONSTRAINT` is prohibited in a
fix *and* in a rollback. That made every correct primary-key fix `REJECTED`,
because a PK has exactly one inverse. `policy.rollback_undoes_own_constraint()`
now allows it only when the fix `ADD CONSTRAINT X` and the rollback
`DROP CONSTRAINT X` name the same constraint. Tested five ways — the own
constraint passes; a different constraint, a fix that drops one, and a rollback
with a second statement appended are all still refused. Verified on the
rehearsal copy that the rollback is a true inverse: after both dry runs the key
columns were nullable again and no constraint or index was left behind.

## Known limits

- **Proving is not applying.** Both fixes are now proven on a copy, but there is
  still no apply step against production, deliberately. That is Phase 5's gate
  and a human decision, not a function call.
- **XML-typed findings cannot be rehearsed** on a same-instance copy, because an
  XML schema URL is unique per database. They stay `BLOCKED`, which is honest —
  a separate instance would lift this.
- **Static output is estate-specific.** `bedrock/static/` has entries for
  `DBMIG_APP` only. A collaborator's estate gets no static answers at all —
  its L2 findings route to a human — which is the honest result, not a gap to
  paper over with generic text.
- **With static mode on, 36 findings still need a human** (25 L3 by policy, 11
  DQ-009 on a discovery gap). The 25 L2 that a model would handle are answered
  by static entries. The breakdown below is what a live model would change:
- **61 of 66 findings are routed to a human — but Bedrock would only move 25.**
  The earlier wording here implied all 61 were waiting on a model. They are not:

  | Level | n | Reason recorded | Bedrock helps? |
  |---|---|---|---|
  | L2 | **25** | `no_template_and_model_disabled` | **yes** |
  | L2 | 11 | `template_declined` (DQ-009) | no — see below |
  | L3 | 25 | `human_authored` | no — policy, not availability |
  | L4 | 3 | a decision, not a statement | no |

  `generate.build_fix()` returns on `human_authored` **before** templates or
  Bedrock are tried, so L3 is reserved by design. Wiring Bedrock does not touch
  those 25 unless the policy itself changes.

- **The 11 DQ-009 findings are a discovery gap, not a model gap.** The template
  declines because the finding does not carry the column's declared length, and
  a model would be missing the same number. Collecting `CHAR_LENGTH` in the
  column probe converts all 11 into templated fixes — deterministic, no model,
  no AWS. This is the cheapest remaining win in the phase.
- **No retry loop yet.** `policy.MAX_ATTEMPTS` is 3 and recorded, but a failed
  generation is not retried — there is nothing to retry into.
- **The two `OPS` criticals are source-side.** `ALTER DATABASE ARCHIVELOG` needs a
  restart. That is a maintenance window, not something an agent applies.

## How to run it

```bash
python -m remediate.run
```

With the dry-run gate live:

```bash
$env:DBSHIFT_REHEARSAL_DSN='localhost:1521/XEPDB1'; $env:DBSHIFT_REHEARSAL_PASSWORD='...'; python -m remediate.run
```

Console: **Phase 4 - Remediate**.

## What unblocks it

1. ~~A rehearsal database~~ — **done 2026-09-10.**
2. **Bedrock Marketplace access** — moves **25** of the 61 toward drafted fixes,
   not all 61. See Known limits for where the other 36 actually sit.
3. **`CHAR_LENGTH` in the column probe** — converts 11 more without a model.

## Change log

**2026-09-12 — database-level static entries are served across estates.** The
full telco run reported `static_outputs_used: 2` although every entry in
`bedrock/static/DBMIG_APP.json` was authored for `DBMIG_APP`. The two are
`OPS-006` (`V$SYSMETRIC`, utilization unavailable) and `RDS-015`
(`NLS_CHARACTERSET`, create the target as AL32UTF8): findings with **no owner**,
so their lookup key is identical on any estate, and their `written_against`
detail matched exactly because `DBMIG_TELCO` is also AL32UTF8 with no metric
rows. The staleness guard therefore did its job — a different character set
would have been refused — and both answers are correct for telco. Recorded
rather than changed: a database-level fact is not estate-specific, and the
detail match is the real guard. Anyone adding an owner-less entry should write
its `written_against` to carry the fact it depends on, as these two do.

**2026-09-11 (static stand-ins)** — `model_mode` (`off` / `static` / `live`)
replaces the `allow_model` boolean; `static` is the CLI and console default.
`bedrock/static.py` + `bedrock/static/DBMIG_APP.json` answer all 25 L2
no-template findings, each written against facts checked on the source that day
(recorded in `evidence`): `NOTE_ID` 20,000 distinct in 20,000 rows, `EVENT_ID` 2
in 2, `LEGACY_SCORE` 0 non-null in 120,000, `COMM_LOG.NOTES` longest value 510
characters. New status `ADVICE_DRAFTED`. Narrow rollback exemption for
`DROP CONSTRAINT` (see Design decisions).

Two draft answers were withdrawn during authoring because they were wrong for
this migration, which is worth recording because a model could make the same
mistake: a `NUMBER(10,2)` precision for `DQ-006` (same engine both sides, so
`NUMBER` migrates as `NUMBER`; the "fix" would have changed production to solve
a heterogeneous-target problem this migration does not have), and excluding
`LEGACY_SCORE` from the load for `DQ-008` (saves nothing measurable, risks
anything still referencing it).

Run `18c1809a`: `AUTO_APPLY` 2, `BLOCKED` 2 (L2 approval only — all other gates
passed), `ADVICE_DRAFTED` 23, `MANUAL_ACTION_REQUIRED` 36, `NOT_A_FIX` 3. Dry
runs: `DQ-001` AUDIT_SCRATCH 358/393 ms, COLLATERAL_NOTE 234/38 ms, `DQ-007`
12,892/1,784 ms, `PERF-001` 66/48 ms (apply/rollback).

**2026-09-10 (rehearsal live)** — Dry-run gate wired to a real copy; both fixes
proven end to end with the timings in `Latest update`.

Four things went wrong building the copy, all worth keeping:

- **There is no `CREATE INDEX` system privilege** in Oracle — only
  `CREATE ANY INDEX`, which the rehearsal user must *not* have. An owner can
  already index its own tables, so nothing was needed.
- **A trailing `-- comment` after a `GRANT`'s semicolon** makes SQL\*Plus fold it
  into the statement and fail with `ORA-00990`, which reads exactly like an
  invalid privilege name and sends you the wrong way.
- **That same bug silently swallowed the next line**, the `ALTER USER … QUOTA
  UNLIMITED` — so the script printed a healthy `OPEN` account, and the 602 MB
  import then failed with `ORA-01950` on every single table, 39 errors deep. The
  script's verify block now raises if the quota is missing, because a check that
  only reports what is easy to report is how this happens.
- **`impdp` is an OS program, not a SQL command** (`SP2-0734` if run at `SQL>`),
  and its `EXCLUDE` clauses need a parfile — Windows quoting mangles them on the
  command line either way.

After the fixes: 39 errors → 1, and that one is `SP_BROKEN_DEMO` compiling with
warnings, which is a **seeded defect faithfully reproduced**, not a failure.

**2026-09-10** — Console stage added. Policy statement-counting fixed: it counted
semicolons, so a PL/SQL block (`BEGIN …; END;`) was rejected as "more than one
statement", which refused every valid `DBMS_STATS` fix.

**2026-09-10 (earlier)** — Built. Verified with six hostile statements —
`DROP TABLE`, `DELETE`, `GRANT`, `CREATE OR REPLACE PACKAGE`, a missing rollback,
and a fix naming the wrong object — all rejected; only the legitimate one reached
the dry-run gate.
