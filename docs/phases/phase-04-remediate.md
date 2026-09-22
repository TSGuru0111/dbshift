# Phase 4 — Detect & Remediate

> **Latest update — 2026-09-17.** **The phase now remediates AWS SCT's action
> items, routed by where the fix belongs.** `remediate/sct_plan.py`,
> `sct_generate.py`, `pg_policy.py`, `sct_run.py`. SCT is the assessment a client
> reads (Phase 2), so it is what Phase 4 acts on; the 50-rule path in `plan.py`
> is unchanged and still runs.
>
> **The substantive decision: a second allow-list, not a wider one.**
> `remediate/policy.py` guards a client's **production Oracle** and permits
> almost nothing — `CREATE INDEX`, `ALTER TABLE`, `DBMS_STATS`. A target-side
> SCT fix needs `CREATE EXTENSION`, `CREATE TABLE`, `ALTER ... VALIDATE`, which
> that list must never admit. Adding them would have quietly widened what may
> run against the source, so `remediate/pg_policy.py` is a separate file that
> shares no rules with it, and the self-test asserts neither imports the other.
>
> Which policy applies comes from the item's **route** (`sct/route.py`), which
> is a reviewed table — **the model cannot move a statement from one policy to
> the other.**
>
> Proven against the real PostgreSQL 16 in Docker: SCT **5639** (install
> `postgres_fdw`) reaches `READY_TO_APPLY` with all four gates passing, a
> deliberately broken statement is caught by the engine rather than by a syntax
> guess, and `postgres_fdw` is still absent afterwards — which is the rollback
> proving itself. Self-test `remediate.selftest_sct` **85/85**;
> `selftest_apply` unchanged at 41/41.
>
> Earlier — 2026-09-16.** **A proven fix can now be applied and kept.**
> Until today the phase proved fixes and stopped: `rehearsal.py` applied each one
> and rolled it back, so nothing survived. `remediate/apply.py` is the exception
> — the only module here that writes and keeps what it writes.
>
> **It applies to the rehearsal copy, never to the source.** That was a decision,
> not a limitation to route around: the source is the system of record until
> Phase 9, and two of `DBMIG_APP`'s four CRITICALs (`OPS-001`, `OPS-002`) need
> `ALTER DATABASE` and a restart — a maintenance window a DBA schedules, not
> something an agent does while a client watches.
>
> **Run for real on 2026-09-16.** 2 fixes applied to `DBMIG_REHEARSAL`, 65 held
> back with reasons, recorded against `guru.ts@ganitinc.com`. Verified after:
> `IX_COLLATERAL_NOTE_LOAN_ID` present and `PAYMENT_HIST` statistics refreshed on
> the copy — and `DBMIG_APP` **unchanged**, still 90 objects with statistics dated
> 2026-09-08. Self-test `remediate.selftest_apply` 41/41.
>
> Earlier — **2026-09-11 (static stand-ins).** Bedrock invoke is still
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

- **Applying means applying to the copy.** Since 2026-09-16 a proven fix can be
  applied and kept — `remediate/apply.py`, on the rehearsal copy. There is still
  **no apply step against the source**, deliberately: that is Phase 9's decision,
  taken with a certificate, not a function call here. A fix proven on a copy and
  applied to that same copy is a claim this project can stand behind; "we
  corrected your production database" is not, on the evidence available.
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

**2026-09-16** — **A proven fix can be applied and kept.** Client feedback: "if
it is broken in source, correct it and write it to target using agent." The
phase proved fixes and then threw them away -- `rehearsal.py` applies each one
and rolls it back, by design -- so nothing an agent produced ever survived.

`remediate/apply.py` added, plus `remediate/apply_cli.py`. Deliberately a
separate command from `remediate.run`: planning is free and repeatable, this
writes and keeps, and the two should not share an entry point where a stray flag
turns one into the other.

**Where it writes, and why that was the choice.** The feedback said "in source",
and the target chosen was the **rehearsal copy**. The source is the system of
record until Phase 9; `remediate/rehearsal.py` opens with "Nothing here ever
touches the source"; and two of `DBMIG_APP`'s four CRITICALs need
`ALTER DATABASE` plus a restart, which is a maintenance window rather than an
agent action. Applying to the copy is the strongest honest version of the
request.

Five rules, each for a specific failure:

- **Only `AUTO_APPLY` and `READY_TO_APPLY`.** `BLOCKED` most often means nobody
  has approved it, which is the gate working.
- **The `dry_run` gate must have passed** — checked against the gate record, not
  inferred from the status. A status can be widened by a later policy change; a
  gate result is a fact about this run. This is the check most likely to matter
  later, and the self-test asserts a failed *and* a missing dry run are both
  held back even at `AUTO_APPLY`.
- **The target is re-checked here**, not trusted from a plan that may be hours
  old.
- **Each fix commits on its own** — unlike `convert/apply.py`, which is one
  transaction because a half-applied schema is worse than none. Remediation
  fixes are independent: a failure on the fourth is no reason to undo three that
  worked.
- **The statement is remapped and re-screened** on the exact text about to run.

Two refusals are about safety rather than readiness: the rehearsal schema must
differ from the source (otherwise every "rehearsal" write lands on production),
and `confirm_schema` must be typed back. Preflight *reports* the first rather
than raising, so a caller checks `ready` instead of catching exceptions.

**Run for real, 2026-09-16.** `DQ-007` (statistics on `PAYMENT_HIST`) and
`PERF-001` (an index on `COLLATERAL_NOTE.LOAN_ID`) applied to `DBMIG_REHEARSAL`,
65 entries held back with a reason each, recorded against
`guru.ts@ganitinc.com`. Verified afterwards on both databases:

| | before | after |
|---|---|---|
| `DBMIG_REHEARSAL` index | absent | **present** |
| `DBMIG_REHEARSAL` statistics | 2026-09-11 | **2026-09-16** |
| `DBMIG_APP` index | absent | absent |
| `DBMIG_APP` statistics | 2026-09-08 | 2026-09-08 |
| `DBMIG_APP` objects | 90 | 90 |

Self-test `remediate.selftest_apply` **41/41**, with `check_target` stubbed so
the refusals are exercised on a machine with no database.

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

## Phase 4 over AWS SCT's action items

`plan.py` remediates the 50-rule findings. `sct_plan.py` does the same for SCT's
action items, and has to make a distinction the rules path never needed: **where
the fix belongs.**

| Route | What Phase 4 does | Gated against |
|---|---|---|
| **source** | drafts a statement for the client's Oracle | `policy.py` + rehearsal dry run + named approver |
| **target** | drafts PostgreSQL DDL for the target being built | `pg_policy.py` + real PostgreSQL dry run + named approver |
| **decision** | drafts nothing — records the advice | n/a |
| **human** | drafts nothing, or drafts and labels it a draft | n/a |

Routing comes from [`../16-sct-runner.md`](../16-sct-runner.md)'s table, not from
here and not from the model.

### Why the two policies stay apart

`remediate/policy.py` exists to keep statements away from a **production
database serving traffic**. Its allow-list is four shapes wide and every
addition is a new way to damage that database.

A target-side fix is a different risk: the target is being built, has no
traffic, and Phase 6 can rebuild it. So `pg_policy.py` is wider — but it is
still a list, and it still refuses anything destructive, because by Phase 7 the
target holds migrated data and a statement that was harmless during the build is
not. **A fix must not depend on which phase it happens to run in.**

It also refuses dollar-quoted function bodies. Converted PL/pgSQL is Phase 4b's
job, where it is compiled and gated properly; a remediation statement carrying a
function body would bypass that.

### Approval is required even for the automatic route

SCT 5639 is the one item this path can fully automate — the fix is
`CREATE EXTENSION postgres_fdw`, always. It still needs a named approver.
Installing an extension on a client's database is their DBA's decision, and
"automatic" here means *no model was needed to write it*, not *nobody has to
agree to it*.

### A bug worth recording: the Oracle static gate rejected the one automatable fix

`gates.static_check` requires the statement to name the object the finding is
about. That is correct on Oracle — a fix touching a different table is not a fix
— and **wrong for a target fix**: `CREATE EXTENSION postgres_fdw` cannot name
the database link it exists to replace. Reusing the Oracle gate therefore
REJECTED SCT 5639, the single item with a perfect deterministic remedy.

`sct_plan.pg_static_check` drops the object-reference rule and keeps the two
that still mean something (a statement exists, it carries a rollback). Whether
the statement is *appropriate* is the policy gate's job; whether it *works* is
the dry run's. The self-test asserts both halves: the target gate accepts it,
and the Oracle gate would still have refused it.

### How to run it

```powershell
$env:DBSHIFT_PG_DSN='localhost:5432/dbshift'      # the Phase 4b PostgreSQL
$env:DBSHIFT_PG_USER='dbshift'
$env:DBSHIFT_PG_PASSWORD='dbshift-local-only'
.\.venv\Scripts\python.exe -m remediate.sct_run                        # plan only
.\.venv\Scripts\python.exe -m remediate.sct_run --model live           # let the model draft
.\.venv\Scripts\python.exe -m remediate.sct_run --approve you@x.com    # hold approval
.\.venv\Scripts\python.exe -m remediate.selftest_sct                   # 85/85, offline
```

Output: `remediate/output/sct_remediation_plan.json`. It records `applied: false`
and means it — `apply.py` is not wired to this path.

## Change log

**2026-09-17 — Phase 4 remediates AWS SCT's action items.** Client direction:
*"in remediate also now I need AI response to remediate the SCT problems, not
the things we get from our rules"*, with the source/target/human split spelled
out.

Four modules: `sct_plan.py` (route, draft, gate), `sct_generate.py` (templates,
then the model, with **a different prompt per engine** — one prompt would have
produced Oracle syntax for the target), `pg_policy.py` (the separate target
allow-list), `sct_run.py` (the CLI).

- **The Oracle allow-list was not touched.** Asserted by test, because the
  tempting shortcut — one policy with a flag — would have made a client's
  production database less safe to serve a target-side feature.
- **SCT's own recommendation is carried into every record and every prompt**, so
  a reviewer compares our draft against AWS's advice rather than taking ours on
  trust.
- **A grouped item names its objects.** SCT emits one row per occurrence and
  Phase 2 groups them; a fix for 27 columns needs to know it is writing a
  pattern, not one statement.
- **`model_mode='off'` drafts nothing and says so per item** rather than
  reporting an empty plan, which would read as "no work to do".

Two bugs found by running it rather than reading it: the Oracle static gate
rejecting SCT 5639 (above), and `pg8000` cursors not being context managers —
`with conn.cursor()` raised a `TypeError` that the gate reported as *the
statement failed*, i.e. the check blaming the estate for its own defect. Both
fixed; the second now follows the explicit connect/try/finally shape
`convert/target.py` already used.

One test bug fixed too: two prompt assertions matched a substring that spans a
line break in wrapped source, so they failed while the prompts were correct.
They match whitespace-collapsed text now.

**Not done yet:** `apply.py` does not accept an SCT plan, the console has no
Phase 4 SCT screen, and a live-model run is untested because the AWS SSO token
expired mid-session (`aws sso login --profile dbshift-bedrock` to refresh).

**2026-09-17 (later) — the model tier ran live against SCT's items, and the
result is the argument for the whole design.**

Bedrock verified 2/2 on Sonnet 4.6 and `--model live` drafted for real. On
`DBMIG_TELCO`, 7 action items:

| SCT | Route | Outcome |
|---|---|---|
| 5984 | source | **declined** — "a reviewer must know the actual precision and scale... choosing wrong values could silently truncate existing data" |
| 5581 | target | **declined** — "the column definitions and primary-key columns are unknown" |
| 5034 | human | **declined** — correctly identified it as Phase 4b's work |
| 5326 | target | drafted a statement, **REJECTED by the dry run** |

**The model declined three of four rather than guessing**, which is what the
prompt's "an empty `sql` is a correct answer, not a failure" instruction is for.
Each refusal came with the reason and what a reviewer must establish first.

**The one statement it wrote is the result worth keeping.** For SCT 5326 it
produced:

```sql
ALTER TABLE DBMIG_TELCO.CK_DEVICE_STAT DISABLE CONSTRAINT CK_DEVICE_STAT;
```

That is **Oracle syntax aimed at a PostgreSQL target**. It passed `pg_policy` —
`ALTER TABLE` is legitimately on the allow-list — and was then caught by the
real database:

```
pg_dry_run  fail  42601: syntax error at or near "CONSTRAINT"
```

**An allow-list cannot catch this and a syntax checker would not have either.**
Only executing against the actual engine does. That single result justifies the
dry run being a real connection rather than a parse, and it is why
`pg_dry_run` blocks rather than passes when no target is configured.

Two bugs of mine found on the way, both silent:

1. **`complete(tier, prompt)` takes the tier first.** I called
   `complete(prompt, tier=...)`, which raises `TypeError` — and my broad
   `except Exception` reported it as *"the model call failed"*. So every item
   came back undrafted on a run where Bedrock was verified working: a code
   defect wearing an outage's clothes. `TypeError` and `AttributeError` are now
   re-raised rather than converted, and the self-test pins the client's
   signature so a contract change surfaces here instead of at a client.
2. **`str.format()` ate the prompt's JSON example.** `{"sql": ...}` is read as a
   field name, raising `KeyError: '"sql"'`. The braces are doubled now, with a
   comment saying why so nobody tidies them back.

`remediate.selftest_sct` **102/102**, including a fake-client suite that covers
a fenced reply, a non-JSON reply, and an empty `sql` treated as the correct
answer it is.

**Note on credentials:** this run used temporary STS credentials supplied in
chat. They are not recorded in any file here, and `aws sso login --profile
dbshift-bedrock` is the right way to get them — see
`docs/15-credential-exposure.md`.

**2026-09-17 (UI) — a placeholder was being read as an approver, so every
target fix blocked.** Reported from a screenshot showing *"hold approval as
you@example.com"* next to **0 ready** and SCT 5639 `BLOCKED`.

`you@example.com` was the input's **placeholder**, not its value. So
`$('#sctRemApprover').value` was empty, the request carried `approve=`, and the
approval gate blocked — with static, `pg_policy` and `pg_dry_run` all passing.
Three gates green and the run still produced nothing applyable, because the
screen looked filled in and was not.

The checkbox now refuses to run with an empty approver and says why —
*"type the approver's name or email — approval is recorded against a person"* —
rather than starting a run whose outcome is predetermined. With a real
approver, SCT 5639 reaches **READY_TO_APPLY** with `approved by
guru.ts@ganitinc.com` recorded on the gate.

**A general lesson worth keeping:** a placeholder that reads like a plausible
value is a trap in any field whose emptiness changes behaviour. The same shape
exists on the rehearsal-database inputs; they are pre-filled with real defaults
rather than placeholders, which is why they do not have this bug.

**The screens were also cut back**, on the note *"the UI is not looking good for
the SCT remedies, I don't need more text"*:

- **Nine explanatory paragraphs removed** from the Assess, Remediate and Gate
  screens — 40-odd lines of justification that belonged in these docs and in
  `title=` tooltips, not above the data.
- **Status chips became readable.** `HUMAN_AUTHORED_REQUIRED` was 23 characters
  of shouting that also set the column width; it is now `a person`, with the
  full constant in the tooltip for anyone grepping the record.
- **Rows are a grid**, so code, title and actor line up down the list instead of
  drifting with each title's length.
- **Group headings carry counts** — `Absorb in the target — 4 · 1 ready ·
  1 human-only` — which is the number a reader actually wants from a group.

Two browser drives updated where they asserted removed sentences: they now
assert the *claim* rather than the wording. `drive_sct_1to5.js` **46/46**,
`drive_sct.js` **37/37**.

**2026-09-17 (review) — the screens now say how to solve it, and what a person
must confirm first.** Client direction: *"in remedies we will show how we should
solve it, and verified by human before directly changing in the target"*, then
*"same in 4b too"*.

The guarantee already held in code -- nothing is auto-applied, `apply.py` is the
only writer and is not wired to the SCT path -- but it was **invisible on
screen**, and most items offered no method at all. `5984`, the largest source
item on the estate, had only *"clears when: the source columns declare precision
and scale"*: a definition of done, not an approach.

**Every routed SCT code now carries `how` and `verify`** in `sct/route.py`:
steps a person can follow, and what they must confirm before it is applied.
Written and reviewed in the table rather than generated -- a model-written
method would differ between runs, and a client reading the same plan twice must
see the same plan. 14 codes plus the unmapped fallback; the self-test asserts
both are present and that each step is a real instruction rather than a stub.

What the row shows now, in this order: **the action**, the SQL if one was
drafted, **how to solve it** as numbered steps, then **"A person must confirm
before this is applied"** as a checklist, then affects/done-when/gates, with
SCT's own words and the routing rationale collapsed at the end. The reasoning
used to sit *above* the answer, which is why the row read as an explanation
rather than a task.

**Phase 4b got the same treatment**, and it needed it more: 4b has the
strongest guarantee in the project -- every conversion is created on a real
PostgreSQL inside a rolled-back transaction -- and said none of it, so a
reviewer could not tell what was already proven and what was still theirs. Its
checklist is derived from the object's own state rather than written per object:
a model-authored conversion says so and asks for a read against the source, a
compile-passed one says *"Already proven: the DDL was created on a real
PostgreSQL and rolled back. It compiles. Behaviour is still yours to confirm"*,
an untranslated construct is listed as left for a person, and a
lower-than-usual model confidence is called out.

**A real gap this exposed: Phase 4b had become unreachable in the console.**
`#btnConvert` was armed off `has_assessment`, because the rules assessment used
to gate everything -- so when that panel left the Assess screen, 4b's button
stayed disabled while `/api/convert` worked fine. It only ever needed
`STATE.run_dir`. Both the live and reload paths now arm it from **discovery**,
which is what it actually reads. Verified: `4b armed by discovery alone: true`,
32 conversions rendered.

`sct.selftest` **278/278**; `drive_sct_1to5.js` **44/44**, `drive_sct.js`
**48/48**.
