# Phase 4d — The application's SQL

> **Latest update — 2026-09-20 (the console screen states its evidence, and
> this file exists).** `#appsqlTargetStatus` — half the band above the fold —
> read **"Checking the target…"** on every load because no JavaScript anywhere
> in the console ever wrote to it. It is now wired to the registered PostgreSQL
> DSN before a run and to the **parse and result outcome** after one, the same
> way Phase 4c's compile line was wired on the same day. Nothing in `appsql/`
> changed.
>
> The first thing the wired line reported was a real finding nobody could see
> before: on `DBMIG_APP` the shadow schema builds **11 of 16 tables and is 5
> short**, so the only two statements the deterministic rules converted are
> `REJECTED` by a `relation "customer" does not exist` — condemned for a fact
> about the database, not about the rewrite.
>
> This is also the first phase doc 4d has had. The phase was built by
> 2026-09-18; the sections below describe the code as it is now, and the change
> log starts at that build rather than reconstructing it day by day.

## Purpose

Convert the SQL the **application** sends, which lives in mapper files in a
source repository rather than in the database.

**AWS SCT never reads this.** SCT connects to a schema and assesses what is
stored there: tables, PL/SQL, views. It cannot see a `ROWNUM` in a MyBatis
`<select>`, because that statement is text in a file in a Git repository. A
client who clears every SCT action item, migrates, and repoints the application
still breaks on the first pagination query — and breaks *silently*, because a
`ROWNUM` rewrite that returns a different page returns rows rather than an
error.

This phase is the one that looks there.

## What actually happens

`appsql/plan.py :: build()`, over a directory of mapper XML.

1. **Extraction** — `extract.from_dir()`. MyBatis `<select>`, `<insert>`,
   `<update>` and `<delete>` elements, with `<sql>` fragments collected
   separately because an `<include>` refers to one and a statement that
   includes it is incomplete without it. Three things are preserved rather
   than flattened: the dynamic tags stay in the text; `${}` interpolation and
   `#{}` binds are kept distinct; and a single concrete rendering is produced
   separately as `probe_sql`, labelled as one branch of several.
2. **Classification** — `classify.scan()` against `constructs.json`
   (**30 constructs: 10 by rule, 15 model, 5 manual**). The worst construct
   found decides the route. Comments and string literals are blanked first, but
   an Oracle **hint is a comment** and is lifted out before the blanking and
   spliced back, because a silently dropped hint is the finding.
3. **Conversion** — `transform.transform()`, sources tried in order: `rule`
   (deterministic, free, byte-identical between runs), `bedrock` (the model
   tier, only for the model tier), `none`. Whichever produced the text is
   recorded on the entry.
4. **The gates** — `gates.run()`, five of them, stopping at the first *failure*
   but never at a *blocked*. See below.
5. **The record** — every statement with its Oracle original, its proposed
   PostgreSQL, the constructs that forced each change, what the gates
   established, and for a manual statement the construct and the reason no
   correct rewrite exists.

### The five gates

```
static -> parity -> parse on the target -> result equivalence -> approval
```

| Gate | Asks |
|---|---|
| `static` | is this still the same *kind* of statement? A SELECT that converts into an UPDATE is a defect no parity check notices. This is where a policy gate would be; DML does not need one |
| `parity` | is every construct accounted for, with no Oracle residue left in the text |
| `parse` | **does the target accept this statement at all** — one connection, catches a column that does not exist on the target |
| `result` | **does it return the same rows as the Oracle original, over the same data** |
| `approval` | a named person accepted it |

**Validation is two gates, not one, and that is the whole point of the phase.**
4b compiles a function and compilation is the entire question. A query has two
separate questions, and merging them is how a converter passes while being
wrong: `parse` is cheap and catches a missing column, `result` is expensive and
is the only thing that catches a `ROWNUM` pagination rewrite that parses
perfectly and returns a different page. A `parse` pass is never reported as
validation.

Every gate returns pass, fail, or **blocked**, and blocked is load-bearing:
with no target, `parse` and `result` report blocked with the reason, never a
silent pass.

### The shadow schema

`appsql/shadow.py`. The parse gate has no answer on an empty database — every
statement fails with `relation "customer" does not exist`, which says nothing
about the rewrite. The first version of this phase reported all twelve
conversions `REJECTED` for exactly that reason.

So the tables come from **Phase 4c's DDL**, not from a second implementation
here, and are built inside one transaction in `dbshift_appsql_shadow` that is
rolled back. Reusing 4c's output is the only version that proves anything: a
schema invented here would give the application SQL a different target from the
one it will actually meet.

Deliberately **no data** (a shadow with rows would invite someone to believe
the `result` gate had run), **no constraints or indexes** (a foreign key cannot
make a SELECT parse), and **it never persists**.

## Inputs / Outputs

| | |
|---|---|
| In | a directory of MyBatis mapper XML — the demo's is `scripts/demo-app/mappers` |
| In | Phase 4c's `convert/output/schema_ddl.json`, for the shadow schema |
| In | a registered PostgreSQL target, for the parse gate |
| Out | `appsql/output/appsql_plan.json` |

Needs **no discovery run and no assessment**, which is the point: the input is
a source repository, not a database.

## On `DBMIG_APP`'s demo application, 2026-09-20

2 mapper files, 18 statements, 1 dynamic, 1 unrenderable.

| | |
|---|---|
| By rule | 2 — **11% of statements** |
| Need the model tier | 10 (the model tier was off for this run) |
| No correct automatic rewrite | 6 |
| Shadow tables built | **11, and 5 short** |
| Result comparisons | **0** |

**The deterministic figure is reported over statements, not constructs.** A
statement carrying one unhandleable construct is declined whole, so the
construct figure flatters the tooling. 11% is the number a client should be
quoted.

The five shadow tables that failed are not a defect in this phase: four need
sequences in a `dbmig_app` schema that the local PostgreSQL does not have, and
`customer` needs the schema itself. Applying 4c first fixes all five.

## Design decisions

- **The manual tier is never sent to the model.** A construct with no correct
  automatic rewrite does not acquire one because a model is fluent. Asking for
  a rewrite of Oracle's empty-string semantics produces confident SQL that is
  wrong, and a dropped optimizer hint needs reporting rather than converting.
  This is why the catalogue carries three tiers and not "automatic" and "hard".
- **Dynamic tags are kept, not stripped.** Stripping them yields SQL that
  parses and is not the SQL the application sends — worse than no extraction,
  because a converter would rewrite a statement that does not exist.
- **`${}` is never rewritten to `#{}`.** It turns a column name into a bound
  literal, so the query fails at runtime rather than at conversion. The rules
  never do it and the `static` gate fails a conversion that does.
- **A statement is never "excluded because it is broken."** 4b can exclude an
  object Oracle itself rejects, because the data dictionary records the
  compilation error. Application SQL has no such signal: a statement that will
  not parse is a **finding**, not an exclusion.
- **`CONVERTED_UNPROVEN` is a state of its own**, distinct from
  `READY_FOR_APPROVAL`. A client reading "ready" about a statement nothing ran
  has been told something untrue.

## Known limits

- **No mapper file is ever written.** Every rewrite is a proposal in the
  record. The thing that changes what an application sends to its database is a
  person, after reading this.
- **The result gate does not run from the console.** `/api/appsql` passes no
  comparisons, so `results_compared` is always false there and every statement
  is correctly reported unproven. Comparisons come from Phase 8.
- **There is no `appsql/run.py`.** Phase 4d is console-only; `plan.py` has no
  offline self-test either, while the other four modules have 89, 67 and 78.
- **MyBatis only.** Other mapper formats, and SQL built by string concatenation
  in application code, are not read.

## How to run it

**Console only — Phase 4d · Application SQL.** Point it at the mapper
directory and press *Convert application SQL*. The model-tier checkbox is off
by default: the rules cost nothing and a model run bills.

```powershell
python -m appsql.selftest         # 89
python -m appsql.selftest_gates   # 67
python -m appsql.selftest_rules   # 78
```

## The console screen

One band of context above the run, then the statements.

The band's left panel is **what is scanned** — the mapper root and the
model-tier switch. Its right panel is **what proves a rewrite**, and it is the
single place this phase's evidence is stated:

| When | What it says |
|---|---|
| Before a run, target registered | rewrites will be parsed on *that DSN*, against a shadow schema that is built and rolled back |
| Before a run, no target | no target is registered, so parse and result will both report **blocked** |
| After a run | how many shadow tables were built **and how many short**, then, separately, whether a result comparison ran |

Parse and result stay **two clauses**, because they are two questions. The
second clause says plainly that no rewrite is proven to return the same rows
until a comparison runs.

The note box below the tiles carries what the run could not **cover** — the
model tier being off, statements that cannot be sent to an engine at all. What
the run could not **prove** is the band's job and is not repeated there.

## Change log

**2026-09-20 — the evidence line is wired, and this file exists.**

`#appsqlTargetStatus` rendered a grey LED and the words "Checking the target…"
and **nothing in the console ever wrote to it**. The id appeared exactly once
in `index.html`, in the markup. So the right half of the band above the fold
was permanently a sentence about a check that was not happening — the identical
fault Phase 4c's `#ddlTargetStatus` had, fixed the same day.

It was wired rather than deleted, and the reason is what it immediately
reported. The parse gate builds a shadow schema on a real PostgreSQL and rolls
it back, and **no part of the screen had ever said how that went**. On
`DBMIG_APP` it builds 11 tables and fails 5, so the only two statements the
rules converted are `REJECTED` for `relation "customer" does not exist` — the
exact failure `shadow.py` exists to prevent, visible in the record and
invisible on the screen. Deleting the panel would have kept it invisible.

What changed, all in `web/static/index.html`:

- `appsqlStatus()` / `renderAppsqlTarget()` / `renderAppsqlProof()`, mirroring
  4c's `ddlStatus()` / `renderDdlTarget()` / `renderDdlCompile()` so a reader
  moving between the two phases is not learning a second layout. Called from
  `loadState()` with `s.pg_dsn`, from `#btnPgTarget.onclick` after a successful
  registration, and from `renderAppsql()` with the real result — which outranks
  the promise.
- **Parse and result are two clauses**, not one sentence. Merging them is how a
  converter passes while being wrong.
- `#appsqlNotes` kept, narrowed. The target and the result comparison were its
  first two entries and are now the band's; it carries what the run could not
  *cover*, not what it could not *prove*.
- **`.status.warn` had no CSS at all.** Both phases set `class="status warn"`
  and got the base rule — a grey LED on the neutral surface, which is precisely
  how the dead panels read. "The compile did not run" and "5 shadow tables
  short" are findings, and a finding that looks unset is the same defect
  wearing different text. Also fixes 4c's not-run line and the model-tier
  fallback on Convert PL/SQL.
- **The shared `.mono{display:block}` rule was breaking a sentence.** Written
  for 4c's engine errors, it also matched 4d's inline prose and put `ROWNUM` on
  a line of its own mid-clause. The block form is opt-in by `.errline` now.

`check_4bcd.js` gained `4d has no dead "checking the target" line`, `4d states
its proof once`, and `4d reports the parse gate`. It also **replaces** the old
`4d says the statements are unproven`, which read `#appsqlNotes` for "No
PostgreSQL target" and so failed whenever a target *was* registered — it tested
the demo's data condition rather than the claim. The replacement holds either
way: nothing may read as proven while no result comparison has run.

`drive_appsql.js` had **three** checks reading the same box for the same moved
text and was the second consumer nobody looked for. They now read the band, and
they no longer depend on this machine lacking a target: the claim under test is
that nothing reads as proven, not that a target is missing.

**Results.** `check_4bcd.js` **48/49**, `drive_appsql.js` **29/30**,
`check_overlap.js` **17/17**, `check_layout.js` no overflow at five widths,
`appsql` self-tests **89 / 67 / 78**.

Both remaining failures are data conditions on this machine and neither touches
the code changed here. `check_4bcd.js`: 4b's rail dot shows a ✓ instead of "4b"
once the stage is restored to done. `drive_appsql.js`: with a target registered
and the shadow schema 5 tables short, the two rule-converted statements come
back `REJECTED` rather than `CONVERTED_UNPROVEN`, so no group reads "Rewritten,
not yet proven" — the status is decided in `appsql/plan.py`, which this change
did not touch.

**By 2026-09-18 — built.** `appsql/` with `extract.py`, `classify.py`,
`transform.py`, `rules.py`, `gates.py`, `shadow.py` and `plan.py`, plus the
`constructs.json` catalogue and the `scripts/demo-app/mappers` demo
application. Console screen and `/api/appsql`; no CLI.

The shadow schema is the decision that made the phase mean anything. Parsing
against an empty database reported all twelve conversions of the day as
`REJECTED` with `relation "customer" does not exist` — condemning the work for
a fact about the database. Building 4c's own tables and rolling them back is
the only version that proves something about the rewrite.
