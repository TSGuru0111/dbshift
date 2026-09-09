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

## Phase 1 — Discovery collector (complete) — 2026-09-08

**Outcome:** `collector/` runs read-only as `dbmig_collector`, oracledb thin
mode, and writes 45 JSON datasets (1,437 rows) to `collector/output/<run_id>/`
in ~9.5 s. No AWS dependency. `collector/verify.py` scores a pair of runs
against ground truth: **5/5 checks pass.**

### What was built

- `config.py` — password from `DBSHIFT_COLLECTOR_PASSWORD` only; never a file
- `db.py` — the single logged query path. Every call records label, SQL,
  SHA-256 of the SQL, row count and elapsed ms. Data and credentials are
  never logged. Schema names are **bound**, never interpolated
- `probes/` — 11 modules: identity, objects, tables, indexes, constraints,
  storage, partitions, plsql, programmatic, security, features
- `writer.py` — one file per dataset under a shared envelope
- `run.py` / `verify.py` — collect, then reconcile

### Output shape and why

One file per dataset, each carrying `collector_run_id`, `dataset`,
`schema_version`, `source_queries` and a flat `rows` list whose keys match the
future `source_inventory.*` column names. `collector_run_id` is stamped on every
row as well as the envelope, so a row can be inserted independently of its file
— that is what makes the append-only rule work end to end. The later AWS push
step is `for file in run_dir: POST`, with the ingest Lambda routing on
`dataset`. No transform layer, no restructuring.

`output/runs.jsonl` is an append-only local ledger mirroring the same rule.

### Problems hit and how they were resolved

**`VIRTUAL_COLUMN` invalid identifier (ORA-00904).** `DBA_TAB_COLUMNS` has
neither `HIDDEN_COLUMN` nor `VIRTUAL_COLUMN`; those exist only on
`DBA_TAB_COLS`. *Fix: query `DBA_TAB_COLS` with `user_generated = 'YES'`, which
keeps the virtual/hidden flags — a virtual column cannot be inserted into on the
target — while excluding Oracle's internal LOB and object-type columns.*
Caught because a failed query is recorded and surfaced in the run summary rather
than swallowed; the first run exited non-zero and named the query.

**`V$SYSMETRIC` is empty on 21c XE** (0 rows, no error). Utilization
percentiles are therefore not obtainable from this source. *Fix: collect
`V$OSSTAT` (`NUM_CPUS = 8`, busy/idle time) as a point-in-time snapshot and
record AWR coverage separately.* AWR views do exist here with 3 snapshots, but
XE is not licensed for Diagnostic Pack and 3 snapshots is not a percentile —
**the sizing engine must not claim percentile-based utilization from this
estate.** On a production EE source, querying `DBA_HIST_*` carries Diagnostic
Pack licensing implications and should be an explicitly gated probe.

**`DBA_FEATURE_USAGE_STATISTICS` was already sampled** (496 rows, last sample
2026-09-08 20:48) — the anticipated need to force a sample as SYS did not
arise. **`Partitioning (user)` is the single edition-forcing feature detected**
(`detected_usages = 1`, `currently_used = TRUE`). No TDE, no Advanced
Compression. The SE2-vs-EE verdict will therefore rest on partitioning alone.

### Reference-doc drift found and corrected

The collector contradicted `03-source-estate.md` in four places. All four were
verified against the live database and the doc has been corrected:

| Claim | Documented | Actual |
|---|---|---|
| Constraints | 50 | **51** (11 PKs, not 10) |
| `TABLE` / `INDEX` objects | 19 / 30 | **21 / 31** |
| `DBMIG_LOOPBACK_LNK` owner | `dbmig_rpt`, absent from `USER_OBJECTS` | **`dbmig_app`, and present** |
| XML schema owner | XDB | **`DBMIG_APP`** |

Root cause of the constraint drift: the Phase 0 census was taken **before**
`04_seed_defects.sql` ran. That script creates `"ORDER"` with an inline
`PRIMARY KEY` (`SYS_C008270`) — the 51st constraint. The 90-object count was
taken after seeding, so the old figures were never internally consistent.

Only 5 of the 11 PKs are user-named business keys. The rest sit on Oracle-managed
internals (`AQ$`, `DR$IX_COMM_NOTES_TEXT$*` Oracle Text tables, `SYS_IOT_TOP_*`).
**The assessment engine must exclude those from "objects to migrate"** or it will
report phantom findings — noted in `03-source-estate.md`.

### Verification performed

Two runs, `2309a50d-…` and `be14610a-…`:

| Check | Result |
|---|---|
| Two distinct `collector_run_id` values | PASS |
| Object count vs live `DBA_OBJECTS` | 90 / 90 / live 90 — PASS |
| Constraint count vs live `DBA_CONSTRAINTS` | 51 / 51 / live 51 — PASS |
| PL/SQL SHA-256 identical across runs | 9 objects, 0 drifted — PASS |
| `DBA_FEATURE_USAGE_STATISTICS` queried | 496 rows both runs — PASS |

`verify.py` reconciles against a **fresh independent query**, not against the
collector's own output, and reports documented-vs-actual drift separately rather
than treating the doc as authoritative.

Also confirmed: exactly one INVALID object (`SP_BROKEN_DEMO`), matching the
`04-defects.md` regression check.

### Open for the next phase

- `dbmig_rpt` owns nothing; the cross-schema story rests on grants and the
  `dbmig_app`-owned link. Revisit if a cross-schema object is needed.
- No push step yet, by design — the envelope is shaped for it, but no AWS
  account exists.

---

## Phase 2 — Assessment engine (complete) — 2026-09-08

**Outcome:** `assess/` loads a collector run into SQLite, evaluates **49 rules
stored as data**, and produces 68 findings, five category scores and a measured
recall figure. **7 of 7 detectable seeded defects found — 100% recall, 7/7
severity exact, 0 missed.** Renders a standalone HTML report. Still no AWS.

### What was built

- `loader.py` — collector JSON into SQLite, the local stand-in for Aurora, plus
  three analysis views (`v_user_tables`, `v_user_objects`, `v_user_columns`)
  that centralise the "what counts as a user object" definition instead of
  repeating it across 49 predicates
- `rules.json` — the catalogue: `rule_id`, category, severity,
  `remediation_level`, rationale, SQL predicate. Installed into a `rules` table
  at run time. **Adding rule 50 is inserting a row**
- `engine.py` — validates the catalogue, runs each predicate, turns every
  returned row into a finding carrying *the rule's own* severity and level
- `scoring.py` — five category scores, overall with the critical cap, and the
  answer-key comparison
- `report.py` — renders `assessment.json` as HTML on every run, so the page
  cannot drift from the numbers it claims

### Why SQLite

Rules had to be SQL to satisfy "stored as data with a SQL predicate". SQLite
gives that locally with no service, and the predicates port to the Aurora
metadata repository later with little more than a dialect change. It is the
piece that makes Discover→Assess demonstrable with no AWS account.

### The read-only account could not read data

`SELECT_CATALOG_ROLE` grants **dictionary** access, not **data** access. The
collector could read metadata about `dbmig_app.customer` and not select from it
(ORA-00942) — correct security posture, and fatal to data profiling.

Added `scripts/oracle-source/05_grant_collector_read.sql`: explicit per-table
`SELECT`, **deliberately not `GRANT SELECT ANY TABLE`**, which a client DBA is
right to refuse. Applied as `dbmig_app`, 12 tables granted, revoke block
documented in the script. The probe now detects ORA-00942 once and marks the
whole owner `no_select_privilege` rather than failing per table.

### Defect 7 was never seeded — the answer key was wrong

The engine reported it missing; direct inspection showed it is **not in the
database**. `04_seed_defects.sql` appends `CHR(146)`, but in AL32UTF8 byte
`0x92` is a bare continuation byte, so `CHR(146)` yields NULL and the
concatenation is a no-op. The UPDATE reported success having changed nothing —
the same silent-failure class as the Phase 0 substitution-variable incident.

Full detail and the `UNISTR('\2019')` fix are in `04-defects.md`. The engine
now reports it as `N/A — not present in source` and **excludes it from the
recall denominator** rather than scoring it as a miss. Counting it as a miss
would understate the engine and hide a broken seed script.

### False positives found and eliminated

First clean run flagged `LOAN.PRINCIPAL_AMT` and `MV_LOAN_SUMMARY.TOTAL_PRINCIPAL`
as near-unique columns carrying duplicates. Both are decimal money columns —
near-uniqueness there is arithmetic coincidence, not an unenforced key. Two
tightenings, both principled rather than ad-hoc:

- duplicate candidates must be text or a **whole** number (`data_scale = 0`)
- materialized-view containers are not profiled; duplicates in an aggregate are
  arithmetic

Additional findings dropped 68 → 60 and the seeded-defect detection was
unaffected.

### The scoring model had to be rebuilt

The first model subtracted severity weights from 100. With enough findings any
category saturates at 0, so `rds_compatibility` and `data_quality` both read
zero and a bad category was indistinguishable from a catastrophic one.

Replaced with: penalty accrues **per rule, not per finding**, scaled
logarithmically by hit count — one rule firing on ten objects is one issue with
a wider blast radius, not ten issues — and the score decays exponentially
(`100·e^(−penalty/60)`) so it keeps resolution everywhere and never bottoms out.
Scores moved from `0 / 0 / 89 / 86 / 26` to `18 / 34 / 88 / 74 / 33`.

### Other problems hit

**`OverflowError: Python int too large to convert to SQLite INTEGER`** — a
sequence `MAXVALUE` defaults to 28 nines, well past 64-bit. *Fix: store
out-of-range integers as text rather than losing the value.*

**`near "limit": syntax error`** — `limit` is reserved in SQLite and
`DBA_PROFILES` has a column by that name. *Fix: quote it.*

**Rules failed to parse against empty datasets.** A dataset with no rows
produced a table with no columns, so every rule referencing it errored. *Fix:
declared fallback schemas for datasets that can legitimately come back empty.*

### Findings the estate did not expect

**The source is `NOARCHIVELOG` with supplemental logging off.** Both are
CRITICAL, and together they mean **DMS CDC cannot run at all** on this estate —
only a full-outage load. The reference architecture assumes ARCHIVELOG plus
supplemental logging as seeded; it is not. Decide before building Phase 6.

`Partitioning (user)` remains the only edition-forcing feature detected, so the
SE2-vs-EE verdict still rests on partitioning alone.

### Results

| Metric | Value |
|---|---|
| Rules evaluated | 49 |
| Findings | 68 |
| Overall score | 39 → **49** after scoring rebuild (capped by 5 criticals) |
| Recall | **7 of 7 detectable (100%)** |
| Severity exact | 7 of 7 |
| Additional findings | 60, triage pending — **not** claimed as false positives |

Report published as an artifact; regenerate with `python -m assess.report`.

### Addendum — scale hardening for client estates (2026-09-09)

Prompted by reviewing AWS's own
[sample-oracle-modernization-accelerator](https://github.com/aws-samples/sample-oracle-modernization-accelerator).

**What that sample is, and is not.** It targets PostgreSQL/MySQL — a
*heterogeneous* accelerator whose value is conversion: DMS Schema Conversion
handles ~95% of DDL, a Bedrock agent takes the failing 5%, an LLM rewrites SQL
inside MyBatis mappers. That is the branch this project deliberately deleted, so
it is not a competitor. Two things in it confirm decisions already made here:
its LLM sits on residual hard cases and semantic conversion, **not** on
generating dictionary queries; and it has **no assessment layer at all** — no
findings, severities, scores or recall metric. It also requires Oracle Instant
Client, where this collector is thin-mode.

Its headline scale figure is 688 tables / 15M+ rows, and the TB-scale claim is
about **DMS data movement**, not assessment. Moving TB is solved; assessing a
large unfamiliar estate is not — in their sample or previously in this one.

**Aggregation — findings collapse to issues.** 68 findings from 90 objects is
~0.75 per object; at 10,000 objects that is ~7,500 rows nobody triages. Findings
now group to one entry per rule carrying occurrence count, affected owners and a
capped object list. **68 findings → 36 issues**, severity-ranked with blast
radius visible, and the count stays bounded by the rule catalogue rather than by
the estate. The scoring model already counted this way, so scores did not move.

**Sampling — large tables are profiled, not skipped.** `DBSHIFT_PROFILE_MAX_ROWS`
previously *skipped* anything above 2M rows, so `LOAN_TXN` (3M) had no
data-quality evidence at all. Above the threshold the probe now block-samples to
a row target (`DBSHIFT_PROFILE_SAMPLE_ROWS`, default 1M) instead:

```
LOAN_TXN   est=3,000,000   scanned=1,000,092   SAMPLE 33.3333%
```

Cost is bounded by the target, not by table size — a 3B-row table samples at
0.033% and still reads ~1M rows.

**Sampling changes what a clean result means**, so this is recorded rather than
glossed. A sampled scan sets `actual_rows = NULL` (only a full scan establishes
a true count; scaling the sample back up would be a guess presented as a
measurement), `DQ-002` and `DQ-003` append "found in an N% sample — the true
count is higher", and new rule **DQ-011** flags every sampled table: *finding a
duplicate in a sample proves duplicates exist; finding none does not prove
absence.*

50 rules, 36 issues, **recall still 7/7 (100%)**, severity exact 7/7.

### Hard walls removed (2026-09-09)

**SQLite indexes on rule join keys.** `DQ-001`, `PERF-001` and `PERF-008` use
`NOT EXISTS` and self-joins against `constraints` and `index_columns`. With no
index those are quadratic — invisible at 90 objects, minutes per rule at 50,000.
The loader now creates **24 indexes** from a declared map of the join keys the
catalogue actually uses, then runs `ANALYZE`.

**PL/SQL source moved out of line.** Full stored code was inlined in the dataset
file. A large estate carries thousands of packages and hundreds of MB of text,
which makes the file unloadable and unpushable. The runner now writes one file
per object under `<run_id>/plsql_source/<sha256>.txt` and leaves an 800-character
excerpt plus a pointer in the row. **`plsql_source.json` dropped to 7 KB.**

The hash was already the identity used for skip-unchanged, so it doubles as the
filename — identical text is stored once regardless of how many objects share it.
The mechanism is declarative (`EXTERNALIZE` on the probe), not special-cased in
the runner, so any future unbounded field uses the same path.

**Loader insert.** `executemany` now consumes a generator rather than a
materialised list, so the converted copy never coexists with the parsed rows.
True constant-memory streaming needs NDJSON or `ijson` and is deliberately
deferred — with source text externalised there is no measured case for it yet.

50 rules, recall still 7/7.

**Still open before a client engagement**

- **Preflight** — verify granted privileges and Oracle version first and report
  gaps, rather than failing on query 40 of 60
- **Version matrix** — the catalogue is 21c-shaped; 19c and 23ai differ
- **Two-tier profiling and a wall-clock budget** — clients grant a fixed window;
  metadata checks are cheap and complete, data checks are expensive and should
  be opt-in per table
- **Sampling cannot find rare events** — two duplicates in three billion rows
  will not surface in a 0.03% sample. `DQ-011` marks the limit; it does not
  remove it
- **Per-schema rollup** — at 40 application schemas one overall score is
  useless; the valuable sentence is "three schemas carry 80% of the risk"
- **Checkpoint and resume** — a run that dies at probe 9 of 12 restarts from zero
- **Semantic vs structural rules** — structural rules port to any client
  unchanged; semantic ones like `DQ-002`'s near-unique heuristic already
  misfired twice here on money columns. At a client they should be presented as
  **hypotheses to confirm**, not findings

---

## Phase 3 — Size & Edition Decision (complete) — 2026-09-09

**Outcome:** `sizing/` produces the target specification and the licence verdict.
**This is the only phase where a model makes a judgement call**, and the whole
design exists to bound it: the proposer suggests, the rules engine decides.

**Decision for this estate:** Enterprise Edition BYOL, `db.t3.medium`
(2 vCPU / 4 GiB), 20 GB gp3, AL32UTF8, **1 Oracle processor licence**.

### The split that matters

- `propose.py` — reads raw facts and suggests edition, instance and storage with
  a rationale. Deliberately naive about licensing nuance: it reports what the
  evidence appears to say. Two implementations, `heuristic` (deterministic, runs
  today) and `bedrock` (wired, unreachable without an account). **Whichever ran
  is recorded in `source`**, so nothing ever implies a model ran when it did not
- `policy.py` — the deterministic rules. Hard EE-forcing features, contextual
  features, the SE2 vCPU ceiling, storage floors, licence arithmetic
- `validate.py` — seven checks, each PASS / OVERRIDE / WARN. **Where they
  disagree the rules win and the disagreement is recorded** as a first-class
  output, because that logged disagreement is the evidence the AI is bounded

### The override fired, and it is the real demo

The proposer read `DBA_FEATURE_USAGE_STATISTICS` and cited **Oracle Multitenant
and Partitioning** as forcing Enterprise Edition. The rules engine **overruled
the Multitenant half**:

> A single PDB is included in every edition, and RDS for Oracle runs a container
> database with one PDB by default. Only PDB counts above the included allowance
> require the Multitenant option.

The verdict did not change — Partitioning forces EE on its own — but **the
justification did**, and in a licence negotiation the justification is what gets
audited. XE reports Multitenant as used simply because it runs as a CDB; a naive
read hands a client a bill for an option they do not owe.

This was not manufactured. The proposer does the reasonable naive thing with the
raw evidence, and the policy layer carries the knowledge that the raw evidence
lacks.

### Edition evidence comes from two independent sources

Feature usage statistics **and** structural evidence (`DBA_PART_TABLES`, bitmap
indexes, compressed segments). Either alone misleads: usage stats can be sampled
before a feature was exercised, and a structure can exist unused. Either is
sufficient to force EE.

### A bug caught in validation, worth recording

The first implementation treated utilization as available when
`sysmetric_rows > 0 OR awr_snapshots > 8`. This estate has **0 live metrics and
15 AWR snapshots**, so it passed — and the tool would have silently claimed
measured headroom it does not have. Exactly the failure class this project
exists to prevent.

Corrected: utilization requires live metrics **and** at least 24 AWR snapshots
(one day hourly, the minimum before a percentile means anything), and the
reason is carried in the output either way. It now correctly warns that the
sizing is a **capacity-derived floor, not a load-derived recommendation**.

### Deliberately not included

**No prices.** An hourly rate depends on region, term, edition and licence model,
and a wrong number quoted to a client is worse than no number. Licence *counts*
are derivable and are computed; licence *cost* is a commercial negotiation and is
left to the TCO engine with rates the user supplies.

### Results

| Check | Verdict |
|---|---|
| edition | PASS — EE confirmed |
| edition_rationale | **OVERRIDE** — Multitenant removed from the justification |
| instance_known | PASS |
| se2_vcpu_ceiling | PASS |
| storage_floor | PASS — 20 GB engine minimum |
| burstable_class | WARN — t3 is fine for rehearsal, verify credits for production |
| utilization_evidence | WARN — capacity floor only, no measured load |

1 override, 2 warnings, proposal **not** accepted as-is.

### Addendum — the OLA utilization hook (2026-09-09)

**Where AWS OLA actually fits.** AWS OLA and DB OLA are **funded,
partner-delivered engagements with no API** — requested through an AWS account
manager or a partner private offer, delivered in three phases (planning,
script-based discovery, reporting), producing right-sizing, a five-year TCO and
BYOL-vs-license-included guidance. There is nothing to call from code.

What OLA does that this platform **structurally cannot** is measure utilization
over a window. A point-in-time read-only scan has no history to take a
percentile of. That was the standing `utilization_evidence` warning.

So the integration is a file handoff, not an API call. `sizing/utilization.py`
accepts a documented CSV — our contract, not a claim about any product's native
export — that an OLA, Migration Evaluator, AWR, Statspack or vendor monitoring
export can be mapped onto:

```
metric,unit,p50,p90,p95,p99,max,samples,window_start,window_end,source
```

`cpu_cores_used` and `memory_used_gb` are required; `iops` and
`storage_used_gb` are reported only.

**Sizing is on p95 with 1.3x headroom.** Not `max`, which sizes for one outlier
and over-provisions an Oracle licence; not `p50`, which under-provisions by
construction. Both the percentile and the headroom are recorded in `sizing.json`
so the choice is auditable rather than folded into a magic number.

**A feed is refused, never ignored.** Missing required metrics, a window under 7
days, bad dates or non-numeric percentiles all cause rejection, sizing falls back
to the capacity floor, and **the validation trail records why** — so a rejected
feed can never be mistaken for an absent one.

Seven days is a floor with a reason: below that there is no distribution, and a
short weekday window misses month-end batch, which is when a database is at its
real peak.

**New check `peak_headroom`** compares the chosen class against the observed
*maximum*, not just the sizing percentile, and warns when it sits below.

### It changes the answer, which is the point

| | No feed | With a 21-day feed |
|---|---|---|
| Basis | capacity floor | load-derived |
| Instance | `db.t3.medium` (2 vCPU / 4 GiB) | `db.m5.2xlarge` (8 vCPU / 32 GiB) |
| **Processor licences** | **1** | **4** |
| `utilization_evidence` | WARN | PASS |

Capacity-only sizing would have under-provisioned *and* understated the Oracle
licence exposure fourfold. That gap is the commercial argument for running an
OLA, and it is now quantifiable rather than asserted.

The example CSV is marked `example_not_real_data` in every `source` value, and
the committed `sizing.json` and published report are generated **without** it —
they show the honest capacity floor for this estate.

### Positioning

Run this assessment first: seconds, free, and it answers whether the estate
forces Enterprise Edition — the question that decides whether an OLA's weeks are
worth spending. Then use the OLA for the two inputs this platform correctly
refuses to invent: measured utilization and contract economics.

DB OLA produces **no object-level findings** — no severities, no blocker gate,
no recall metric. It answers *what should I buy*, not *what will break*.

---

## Phase 4 — Detect & Remediate (not started)

<!-- Append entries here as work proceeds -->
