# Phase 4b — Convert PL/SQL

> **Latest update — 2026-09-12 (built, and proven on both estates).** A new
> phase converts stored code from PL/SQL to PL/pgSQL, gates it five ways, and
> **compiles it for real on PostgreSQL 16 inside a transaction that is rolled
> back**. On `DBMIG_APP` (run `ee35e2bf`): 9 objects, **6 ready for approval**
> — two types, a function, a procedure, a package body flattened into two
> routines, and a trigger — every one created and rolled back on the target.
> 1 manual (an XML-schema generated type), 1 excluded because it is broken on
> the source, 1 package specification absorbed into its body. On
> `DBMIG_TELCO` (run `58c5244e`): 8 objects, **6 ready**, same shape, **no code
> changes**. Self-test 64/64. The model tier was needed by **zero** objects on
> either estate — every construct present is inside the deterministic subset,
> which is the result, not a gap. **Nothing has been applied anywhere.**
>
> This phase exists because of a decision recorded on 2026-09-12: the
> Oracle-to-Oracle *target* stays as it is, but PL/SQL conversion is added as a
> **capability** for the heterogeneous path — the part AWS's own Oracle
> Modernization Accelerator lists as manual. See `../02-architecture.md`.

## Purpose

Turn stored code into PL/pgSQL that a person can trust enough to approve. The
conversion itself is the easy half. The hard half is the same as Phase 4's:
proving that what came out means what went in — and the specific failure mode
of every text-level translator, including a model, is a construct dropped
silently by output that still compiles.

## What actually happens

`convert/plan.py :: build(inventory)` — per object, in dependency order (types,
then routines, then package bodies, then triggers):

1. **Load** — `convert/inventory.py`. The PL/SQL text Phase 1 collected (inline
   or from `plsql_source/<sha256>.txt`), stored compilation errors, and the
   column, table, trigger and type catalogues. The SHA-256 on each object is the
   collector's, not recomputed.
2. **Route by rule** — `convert/classify.py` scans `convert/constructs.json`, a
   catalogue of 60 Oracle constructs, each with a tier. The worst construct
   found decides: one **manual** construct and the object is a person's; one
   **model** construct and it is the reasoning tier's; otherwise the
   deterministic rules take it. Comments and string literals are blanked first,
   so `'SYSDATE'` in a message is not a construct. An object with stored
   compilation errors is **excluded** — converting a compile error faithfully
   produces a compile error.
3. **Source a conversion**, in order, governed by `model_mode`:
   - **rule** — `convert/rules.py`. Deterministic rewrites for the subset,
     each recorded with its reason. Anything outside the subset raises
     `Declined`, a routing decision.
   - **static_fixture** — `model_mode="static"` (the default).
     `bedrock/static/convert/<ESTATE>.json`, keyed on owner, type and name and
     written against the source SHA-256; a changed source is refused as stale.
     Labelled `source: static_fixture`, `model_id: null`. Empty on both
     estates, and the file says why.
   - **bedrock** — `model_mode="live"`. Strict JSON against a schema, validated
     before use, one audit row per call in `convert/output/agent_decisions.jsonl`.
     Exercised with a stub client in the self-test; not reachable while invoke
     is blocked.
4. **Five gates** — `convert/gates.py`, stopping at the first failure:
   `static → policy → parity → compile → approval`.
5. **Record the outcome**: `READY_FOR_APPROVAL`, `APPROVED`, `BLOCKED`,
   `REJECTED`, `MODEL_REQUIRED`, `MANUAL`, `EXCLUDED_BROKEN_ON_SOURCE`, or
   `ABSORBED_INTO_BODY`. One `.sql` per converted object lands in
   `convert/output/plpgsql/`, headed with its source and status.

### The gates

| Gate | What it does |
|---|---|
| **static** | Creates something, and only names this object may take (`name`, `name_fn` for a trigger, `package$member` for a package). Carries construct accounting |
| **policy** | `convert/policy.py`. Statement headers only, bodies masked. May only `CREATE` a function, procedure, trigger, type or domain. Never `DROP`, `GRANT`, `ALTER`, `SECURITY DEFINER`, another language, or anything else — whatever produced it |
| **parity** | Every construct the classifier found must be accounted for: **translated**, with its PostgreSQL marker actually present in the output, or **not_translated** with a reason. No Oracle form may survive (`NVL(`, `SYSDATE`, `:NEW`, `VARCHAR2`…). A function body may not commit. A trigger function must return |
| **compile** | Created on PostgreSQL inside one transaction with `check_function_bodies = on`, then rolled back. Reports exactly what that proves — see below |
| **approval** | Always a named person. There is no auto-apply level for converted business logic |

### What the compile gate proves, and what it does not

PostgreSQL DDL is transactional, which Oracle's is not. That single property is
what makes this gate safe where Phase 4's syntax gate had to stay offline:
every object is created for real and the transaction is rolled back, so the
target is left exactly as found.

Creating a PL/pgSQL function parses the body and resolves every declaration,
including `%TYPE`. It does **not** plan the SQL statements inside the body —
those are checked when the function first runs. The gate says so in its detail
rather than claiming more. If the `plpgsql_check` extension is available on the
target it runs too, and that does check the embedded SQL; the stock image does
not carry it, and the report says which.

**The shadow schema.** `%TYPE` and `CREATE TRIGGER ... ON table` need the tables
to exist. `convert/target.py :: shadow_statements()` builds them from
discovery's column catalogue — the tables the converted code references, columns
and `NOT NULL` only, no data, no constraints — inside the same rolled-back
transaction. Converted types are created first so a column of type `TY_ADDRESS`
resolves. It is not the migrated schema; it is enough of one to compile against.

### What the rules translate

| Oracle | PostgreSQL | Why it is not just a text swap |
|---|---|---|
| `SELECT … INTO v` | `SELECT … INTO STRICT v` | Without `STRICT`, zero rows sets `v` to NULL instead of raising `NO_DATA_FOUND`, and every Oracle exception handler becomes dead code |
| `NVL(a,b)` | `COALESCE(a,b)` | |
| `SYSDATE` | `date_trunc('second', LOCALTIMESTAMP)` | Oracle `DATE` has second precision; a microsecond timestamp would never equal a stored value |
| `:NEW.x` / `:OLD.x` | `NEW.x` / `OLD.x` | |
| `RAISE_APPLICATION_ERROR(-20001, 'm')` | `RAISE EXCEPTION 'm' USING ERRCODE = 'P0001', DETAIL = 'ORA-20001'` | The number is kept where an application matching on it can find it |
| row trigger | trigger function `RETURNS trigger` + `CREATE TRIGGER` | **`RETURN NEW` is added.** A BEFORE trigger that does not return cancels the row change silently |
| `PACKAGE BODY` | one routine per member, `package$member` | No packages; the spec is checked against the body and creates nothing |
| `TYPE … AS OBJECT (…)` | `CREATE TYPE … AS (…)` | Attributes only |
| `VARRAY(3) OF t` | `DOMAIN AS t[] CHECK (cardinality(VALUE) <= 3)` | The limit becomes a constraint, so the target refuses what the source refused |
| `NUMBER(10,0)` | `BIGINT`; `NUMBER(5,0)` → `INTEGER`… | `convert/types.json`. The same key optimisation AWS's accelerator applies |
| `DATE` | `TIMESTAMP(0)` | PostgreSQL `DATE` has no time |
| `COMMIT` in a procedure | kept | Legal in a procedure `CALL`ed outside a transaction. In a **function** the rules decline — PostgreSQL forbids it |

**One thing the rules deliberately do not translate.** Oracle runs stored code
with the definer's rights by default; PostgreSQL with the caller's. Adding
`SECURITY DEFINER` would be the faithful translation and would also widen
privileges on the target. Every routine records `AUTHID_DEFINER` as
`not_translated` with that reason, the policy gate refuses `SECURITY DEFINER`
outright, and the decision is left with a person who can see it.

## Inputs / Outputs

| | |
|---|---|
| Input | A collector run directory (`--run <id>`, default latest) |
| Input | `convert/constructs.json`, `convert/types.json` — the catalogues |
| Input | `DBSHIFT_PG_DSN` (`host:5432/dbname`), `DBSHIFT_PG_USER`, `DBSHIFT_PG_PASSWORD` — or the compile gate reports `BLOCKED` |
| Output | `convert/output/conversion_plan.json` — every object, route, conversion, gates, status |
| Output | `convert/output/plpgsql/<OWNER>.<TYPE>.<NAME>.sql` — one file per converted object |
| Output | `convert/output/agent_decisions.jsonl` — one row per model call, when live |

## Design decisions

**Rules first, model second, person last — and the rules win.** A construct
with exactly one correct translation gets a rule. A model is asked only where
judgement is needed, and its answer passes the same five gates a rule's does.
Where the gates and the model disagree, the gates win and the object is
`REJECTED` with the reason on it.

**Parity is the gate that matters.** Compile success is table stakes. The
failure that hurts is a conversion that compiles and quietly dropped
behaviour. Parity makes every construct found in the source a line item that
must be closed — translated, with its marker present, or declared with a
reason — and refuses any Oracle form left standing.

**Declining is a routing decision, not a failure.** The rules cover a bounded
subset and say so. Half-converting an object would produce something that
compiles and lies; refusing it sends it to a tier that can reason about it.

**The compile gate is real because PostgreSQL lets it be.** Transactional DDL
is the reason this phase can prove more than Phase 4 could. It is used, and
what it does not prove is written on every gate result.

**Broken on the source is not converted.** `SP_BROKEN_DEMO` and
`SP_RATE_CDR_BROKEN` carry stored compilation errors (`OPS-004`). They are
excluded with the error quoted, because the migration is not where that gets
fixed.

**Static output is labelled, not disguised** — exactly as in Phase 4. On both
estates it was not needed at all, and the fixture file says so rather than
holding invented entries.

**Local PostgreSQL in Docker, not Aurora.** Nothing PostgreSQL-shaped is
installed on this machine and installing it needs admin rights; the account
has no AWS credentials in this session and ~$100 of credit. The official
`postgres:16-alpine` image stands in the way `DBMIG_REHEARSAL` stands in for a
rehearsal RDS instance. Pointing the phase at Aurora later is a DSN change.

## Known limits

- **The subset is the subset.** 21 rule-tier constructs. `DECODE`, `ROWNUM`,
  `CONNECT BY`, cursors, `BULK COLLECT`, dynamic SQL, `TO_CHAR` formats and
  the `DBMS_*`/`UTL_*` packages are model tier and route to a person until
  Bedrock is reachable. The self-test proves the routing and the seam with a
  synthetic `CONNECT BY` function and a stub client.
- **Embedded SQL is not planned by the compile gate** unless `plpgsql_check`
  is installed on the target. A body that references a column the shadow does
  not have will pass here and fail at first call.
- **The shadow is columns and nullability.** No constraints, no data, no
  sequences. A trigger that depends on a sequence default will compile and not
  run.
- **Package state has no home.** A package-level variable, cursor or
  initialisation block declines the whole body.
- **Approval is recorded from a flag**, not from an identity. Phases 6, 7 and 9
  take the approver from the AWS caller identity; this phase should too once
  it has a reason to touch AWS.
- **Nothing applies the DDL.** There is no target schema to apply it into —
  Aurora PostgreSQL is still not a provisioned target — and applying converted
  code is a migration step, not a conversion step.

## How to run it

```powershell
.\scripts\postgres-target\run_pg.ps1                 # local PostgreSQL 16 in Docker
$env:DBSHIFT_PG_DSN='localhost:5432/dbshift'; $env:DBSHIFT_PG_USER='dbshift'; $env:DBSHIFT_PG_PASSWORD='dbshift-local-only'
python -m convert.run                                # latest collector run, static stand-ins, compile live
python -m convert.run --no-compile                   # offline gates only
python -m convert.run --model-mode off               # no stand-ins: rules or a person
python -m convert.run --approved-by someone@example.com
python -m convert.selftest                           # 64 offline checks
```

Against the second estate: `--collector-output telco-output/collector --output-dir <somewhere else>`.

**Console: Phase 4b · Convert PL/SQL**, on the rail between Remediate and the
Blocker gate. It unlocks with the assessment, like Remediate, and is optional —
the homogeneous path skips it. Register the PostgreSQL target (proven reachable
before it is accepted; password in server memory only), then *Convert stored
code*: a ticker routes each object live, one compile event follows, and the
result is tiles in plain words, then one card per object in dependency order —
what it was, what it became, *what changed and why* (each construct as a
check row, the untranslated ones amber), the PL/pgSQL, and the five gates. The
server pins `model_mode="static"`, exactly as Phase 4 does.

## Current result

**`DBMIG_APP`, run `ee35e2bf`** — 9 objects.

| Status | n | Objects |
|---|---|---|
| `READY_FOR_APPROVAL` | 6 | `TY_ADDRESS`, `TY_PHONE_LIST`, `FN_CUSTOMER_FULL_NAME`, `SP_CLOSE_LOAN`, `PKG_LOAN_OPS` body (→ `pkg_loan_ops$record_payment`, `pkg_loan_ops$outstanding_balance`), `TRG_LOAN_STATUS_CHECK` |
| `MANUAL` | 1 | `LoanNotice41_T` — XML-schema generated, quoted mixed-case name |
| `EXCLUDED_BROKEN_ON_SOURCE` | 1 | `SP_BROKEN_DEMO` — `PLS-00049` on the source |
| `ABSORBED_INTO_BODY` | 1 | `PKG_LOAN_OPS` specification |

All six compiled on PostgreSQL 16.15 and were rolled back. Every routine
carries `AUTHID_DEFINER` as not translated.

**`DBMIG_TELCO`, run `58c5244e`** — 8 objects: 6 ready, 1 excluded
(`SP_RATE_CDR_BROKEN`, `ORA-00942`), 1 absorbed. The telco trigger assigns to
`:NEW` — `NEW.due_on := COALESCE(OLD.due_on, date_trunc('second', LOCALTIMESTAMP))`
— which is the case that would have been wrong without the `SYSDATE` rule.

## Applying converted code

> **Added 2026-09-14.** Until now this phase applied nothing, by design: there
> was no target to apply to. With one, `convert/apply.py` creates the approved
> objects for real — **the only place in `convert/` that writes to a database**.

Four rules, each from a specific way this could go wrong:

1. **Only `APPROVED` objects.** Not `READY_FOR_APPROVAL`. An object that passed
   four gates and not the fifth is one a person has not read, and the fifth gate
   *is* a person reading it. `APPLIABLE` is a set of one, so widening it is a
   deliberate edit to that file.
2. **The target is re-checked here**, not trusted from the plan. A plan can be
   hours old and may have compiled against a different database.
3. **One transaction, all or nothing.** A half-applied schema where a package
   body exists and the function it calls does not is worse than no schema: it
   looks finished.
4. **Every statement is recorded before it runs**, so a process that dies
   mid-apply still leaves a record of what was in flight.

Plus a last screen on the exact text about to execute: no `DROP`, `TRUNCATE`,
`DELETE`, `GRANT`, `REVOKE` or `SECURITY DEFINER`, whatever a gate said earlier.
The policy gate checks the converted body; this checks the bytes about to run,
and the two are not the same thing after a plan has been serialised and read
back.

The target DSN is typed back, the way a deploy asks for the account id. One
person approving and another applying is **advisory, not a refusal** — that is
ordinary separation of duty, and refusing it would push people to approve under
whichever identity happens to be running the apply.

**Proven on `DBMIG_APP`:** all six approved objects created for real on
PostgreSQL 16 — two types, a function, a procedure, a package body flattened
into two functions, and a trigger. Five functions and the types existed
afterwards. Self-test `convert/selftest_apply.py` **35/35**, including a
deliberate failure whose rollback left nothing behind.

## Change log

**2026-09-14 — three bugs that only appear once code is applied.** Running the
PostgreSQL path twice in a row found all three; each one made a *working*
conversion look broken, or produced code that could not be called.

**1. The shadow schema used the estate's own name.** That was invisible while
nothing was ever applied: the compile transaction rolled back and the name was
free again. Once the apply path creates real objects, the shadow's
`CREATE TABLE customer` collides with the real `customer`, and every object is
reported `BLOCKED` — which reads as "the conversion broke" when it had in fact
succeeded and been applied. The shadow now lives in `dbshift_shadow_<estate>`,
and `target.to_shadow` rewrites the qualifier on the statements the gate runs
so a compile can never touch what an apply created.

**2. `%TYPE` resolved only where `search_path` happened to be set.** A
declaration of `v_name customer.full_name%TYPE` compiled under the gate, which
puts the schema on the path, and then failed for any real caller with

```
invalid type name "customer.full_name%TYPE"
```

because PostgreSQL resolves `%TYPE` when the body is first parsed at **run**
time. The table is now schema-qualified in the declaration.

**3. Unqualified table names in the body had the same problem, one level
deeper.** Oracle resolves `FROM customer` against the owning schema;
PostgreSQL resolves it against the *caller's* `search_path`, so the converted
function failed with `relation "customer" does not exist` for every application
that had not set the path. Rewriting every table reference would mean parsing
arbitrary SQL; PostgreSQL's own answer is a function-level
`SET search_path = <estate>, pg_temp`, which is one line per function and
reproduces Oracle's name resolution exactly. Every converted function,
procedure and trigger function now carries it.

**Proven by execution, not by reading.** After a fresh end-to-end run, with the
estate schema deliberately **not** on the caller's `search_path`:

| Object | Result |
|---|---|
| `fn_customer_full_name(1)` | returns `Alice Smith` |
| `pkg_loan_ops$outstanding_balance(10)` | returns `5000.00` |
| `trg_loan_status_check` | blocks reopening a written-off loan, with the original Oracle message |

A Phase 4c check constraint also rejected an invalid `loan_status` during the
test, which is the converted rule enforcing itself.

**2026-09-14 — the apply path.** `convert/apply.py`, `convert/apply_run.py`,
`convert/selftest_apply.py`. `plan.build` now records `approved_by` in the plan
itself, not only in each object's approval gate: the apply reads it to record
who approved what was applied.

**Applying for real found two bugs that no amount of reading would have.** Both
were the same shape — *the compile gate and the apply ran the statement
differently, so the gate proved nothing about the apply*:

- **`search_path`.** The gate sets it; the apply did not. A function declaring
  `v_name customer.full_name%TYPE` compiled under the gate and failed on apply
  with a syntax error, because the unqualified table resolved against the
  shadow schema on the path and nowhere else.
- **`check_function_bodies`.** Same divergence. Without it PostgreSQL stores a
  plpgsql body without validating it at all.

Both are now set identically in both places. Worth recording separately:
`check_function_bodies` validates syntax and declarations but **does not resolve
calls**, so a body calling a function that does not exist is created happily and
fails at run time. That is PostgreSQL's documented behaviour, not a gap in this
phase, and the gate's wording already says so.

**2026-09-12 — shadow schema on a multi-schema run.** Driving the console
with the schema field blank collects three owners (`DBMIG_APP`,
`DBMIG_REHEARSAL`, `DBMIG_TELCO`). The rehearsal copy's `TY_ADDRESS` routes
MANUAL (its identifiers came across quoted), so it was never converted — but
the shadow `CUSTOMER` table still named it, PostgreSQL refused, and one
owner's failure aborted the compile for **every** owner while the gate read
"no PostgreSQL target configured": 0 of 26 compiled. Two fixes in
`target.shadow_statements` / `plan.build`: a column may take a user-defined
type only when this run's type DDL creates it (otherwise a TEXT placeholder
with a note), and a failed scaffold now blocks that owner's objects with the
real reason instead of silencing the others (`gates.compile_check` gained a
`shadow_failed` branch, BLOCKED not FAIL). Result on the same run: **15 of 26
compiled**, every owner judged. Self-test 68/68. Also fixed in the console:
after a *live* assessment the Convert button stayed disabled until a page
reload, because only the restore path enabled it. Both found by the Playwright
drive, not by a person — the second-estate lesson again.

**2026-09-12 — built.** `convert/` created: `constructs.json` (60 constructs,
three tiers), `types.json`, `typemap.py`, `inventory.py`, `classify.py`,
`rules.py`, `policy.py`, `gates.py`, `model.py`, `target.py`, `plan.py`,
`run.py`, `selftest.py`; `bedrock/static/convert/DBMIG_APP.json` (empty,
explained); `scripts/postgres-target/run_pg.ps1`; `pg8000` added to the venv
(pure Python, no libpq). Decision recorded in `docs/02-architecture.md`.

Three defects found by the self-test before the first real run, all in this
code: a catalogue regex for nested subprograms that matched every package body
(removed — the rules already decline real nested subprograms); the type mapper
lower-casing `%TYPE` with the table reference; and the model validator refusing
`AUTHID_DEFINER` because the rules added it without a catalogue row. Then one
found by the first real run: the rules registered `%TYPE` only when it appeared
in a body, not in the declarations, so the parity gate correctly rejected a
correct conversion of `FN_CUSTOMER_FULL_NAME` — the gate doing its job against
its own producer.

**2026-09-12 (later) — console stage.** `/api/pgtarget`, `/api/convert` (SSE)
and `/api/conversion` in `web/server.py`; a `convert` stage on the rail and a
screen in `web/static/index.html`; `plan.build` gained an `on_event` callback
for the ticker. Verified by driving the routes from a script against the live
container (9 converting/converted events, 1 compile, 6 ready) and by parsing
the console's inline script under Node. Not yet done: an approver taken from
the AWS identity rather than a flag.
