# Phase 3 — Size & Edition decision

> **Latest update — 2026-09-14 (two migration paths).** The phase now decides
> **which engine**, not only how big. Two targets are supported and the client
> chooses: Amazon RDS for Oracle (homogeneous) or Amazon RDS for PostgreSQL
> (heterogeneous). `sizing/target.py` assesses both from the estate's own
> evidence and separates **blockers** (the engine cannot express this at all)
> from **effort** (work with a known shape); `sizing/validate.py` then sizes
> whichever was chosen, with the Oracle licence arithmetic applying only to the
> Oracle path. Self-test `sizing/selftest_target.py` 41/41; console drive 23/23.
> Earlier: `execute()` shared with the console, and measured utilization as CSV.

## Purpose

Decide what to buy: **which engine**, then edition where one exists, licence
model, instance class and storage. **This is the only phase where a model makes
a judgement call**, and the whole design exists to bound it — the proposer
suggests, the rules engine decides.

## The two paths

| | Amazon RDS for Oracle | Amazon RDS for PostgreSQL |
|---|---|---|
| Kind | Homogeneous | Heterogeneous |
| Stored code | Moves unchanged | Rewritten by Phase 4b, compiled before it counts |
| Table DDL | Moves unchanged | Converted (not yet built) |
| Data movement | Data Pump over S3 | AWS DMS — Data Pump writes an Oracle-only format |
| Edition | EE or SE2, decided from feature evidence | None — the engine is open source |
| Licences | BYOL processor count, or licence-included | None |
| Storage | Oracle segment bytes + headroom | Same, raised 20% first (no segment compression, 8 KB pages, larger indexes) |

**The rules do not choose.** They assemble the evidence and publish a
recommendation with its reasoning; a person picks, and the pick is recorded
with who made it. `sizing.target.choose` refuses a blocked path outright —
a blocker is not a warning.

**The recommendation is deliberately conservative.** PostgreSQL is recommended
only when no blocker exists *and* Phase 4b has actually compiled the stored
code. An estate whose code has never been converted gets "insufficient
evidence", never a guess; an estate with handwork left gets "a judgement, not a
verdict", because whether the remaining objects are worth the licence saving is
a commercial decision the tool should not make.

## What actually happens

`sizing/run.py :: execute()`

1. **Load** the collector run into SQLite (same loader as Phase 2).
2. **Extract facts** — `sizing/facts.py`: segment bytes, feature usage,
   structural evidence (partitioned tables, bitmap indexes, compressed tables),
   character set, log mode, and whether any usable utilization exists.
3. **Propose** — `sizing/propose.py`. Reads raw facts and suggests an edition,
   instance class and storage with a rationale. **Deliberately naive about
   licensing nuance**: it reports what the evidence appears to say. Two
   implementations, `heuristic` (runs today) and `bedrock` (wired, unreachable).
   Whichever ran is recorded in `source`.
4. **Validate** — `sizing/validate.py`. Seven checks, each `PASS` / `OVERRIDE` /
   `WARN`. **Where they disagree the rules win and the disagreement is recorded**
   as a first-class output.

### The edition verdict

`sizing/policy.py` decides from **two independent sources**, because either alone
misleads: feature-usage statistics can be sampled before a feature was exercised,
and a structure can exist unused. Either is sufficient to force EE.

Features are split into **hard** (unavailable on SE2 at any price — partitioning,
TDE, Advanced Compression, Diagnostic/Tuning Pack…) and **contextual** (need a
threshold before they force anything). Multitenant is the one that matters: XE
and RDS both run a container database with a single PDB, which every edition
includes.

## Inputs / Outputs

| | |
|---|---|
| Input | A collector run directory |
| Input | Optional utilization CSV (`--utilization`, or upload in the console) |
| Output | `sizing/output/sizing.json` — facts, proposal, decision |

## Design decisions

**Proposer and validator are separate on purpose.** A proposal is a suggestion;
only the rules engine produces a decision. That split is the architectural claim
of the whole project, and this is where it is demonstrated.

**The override is real, not staged.** The proposer reads the feature-usage view
and cites Multitenant and Partitioning as forcing EE. The rules engine overrules
the Multitenant half — the verdict holds because Partitioning forces EE alone,
but the *justification* changes, and in a licence negotiation the justification is
what gets audited. A naive read hands a client a bill for an option they do not owe.

**No prices.** An hourly rate depends on region, term, edition and licence model,
and a wrong number quoted to a client is worse than none. Licence **counts** are
derivable and computed; licence **cost** is left to a rate file the user supplies.

**Sizing is p95 with 1.3× headroom** when measured load exists. Not `max`, which
sizes for one outlier and over-provisions an Oracle licence; not `p50`, which
under-provisions by construction. Both values are recorded in the output.

**A utilization feed is refused, never ignored.** Missing metrics, a window under
seven days, or bad values fall back to the capacity floor and the validation trail
records why — so a rejected feed cannot pass for an absent one.

## Known limits

- **Without a feed this is a capacity floor, not a recommendation.** A catalogue
  scan has no history to take a percentile of. The output says so as a WARN.
- **AWS OLA has no API.** The integration is a documented CSV handoff, not a call.
- **Bedrock proposer is not wired.** InvokeModel is blocked on this account.
- **Memory sizing has no good signal on XE** — `sga_target` and `memory_target`
  are 0, and `cpu_count` reports the host's, not the database's.

## How to run it

```bash
python -m sizing.run
python -m sizing.run --utilization path/to/feed.csv
```

Console: **Phase 3 - Size & Edition**, with CSV upload.

## Current result on the reference estate

**Enterprise Edition, BYOL** — `db.t3.medium` (2 vCPU / 4 GiB), 20 GB gp3,
AL32UTF8, **1 processor licence**. Forced by `Partitioning (user)`. Multitenant
detected and dismissed. 1 override, 2 warnings.

With the example 21-day feed it becomes `db.m5.2xlarge` and **4 licences** —
capacity-only sizing understated the licence exposure fourfold. That example is
marked `example_not_real_data` and is not used unless explicitly uploaded.

## Change log

**2026-09-14 — two migration paths; the client chooses.** Added
`sizing/target.py` (blockers, effort, recommendation, `choose`),
`policy.pg_storage_floor_gb`, and `validate._validate_postgresql` as a separate
function rather than branches threaded through the Oracle path — half the
Oracle checks have no PostgreSQL meaning, and a shared function full of
`if engine ==` would read as though they did. `execute()` gained `engine`,
`chosen_by` and `conversion`; the CLI gained `--engine`, `--chosen-by` and
`--conversion`. The console gained `POST /api/engine`, a path chooser above the
sizing run, and per-engine rendering: the edition panels hide on PostgreSQL and
a note explains why. Changing the path marks a completed sizing stale rather
than leaving a stale edition on screen looking current.

Two bugs found by the browser drive, not by reading: the sizing completion
handler printed a literal `null null` on PostgreSQL because it hardcoded
edition and licence; and the path chooser rendered only after a connection, so
it was invisible on first load. Both fixed.

Measured on `DBMIG_APP` with Phase 4b compiled: PostgreSQL open, 8 effort
points across 7 items, 86% of stored code converting automatically, one object
needing a person — so no recommendation, by design.

**2026-09-12 — Multitenant reason corrected in code.** The `CONTEXTUAL_FEATURES`
wording in `sizing/policy.py` now says that a single PDB is included in every
edition *and* that a non-CDB target uses no Multitenant at all, naming RDS 19c
as non-CDB. String and comment only; the verdict logic is untouched, so the
edition decision on both estates is unchanged. Closes the 2026-09-11 entry below.

**2026-09-11 — a stated reason contradicted by the real target.** The rules engine
dismisses Multitenant partly because "RDS for Oracle runs a container database
with one PDB by default". The RDS 19c target Phase 6 created for `DBMIG_APP` is
**non-CDB** (`v$database.cdb = NO`, from `provision.verify`). The *verdict* is
unaffected — a single PDB is included in every edition, and a non-CDB uses no
Multitenant at all — but the *reason* in `sizing/policy.py` is wrong for this
target, and in a licence discussion the reason is what gets audited. Fixed
2026-09-12, see above.

**2026-09-10** — `main()` split into `execute()`; console stage added with
utilization upload.

**2026-09-09** — Utilization hook: CSV contract, p95 + 1.3× headroom, a
`peak_headroom` check against the observed maximum, refusal of short windows.

**2026-09-09 (earlier)** — Built. A bug caught in its own validation: the first
utilization check passed on `0 live metrics OR 15 AWR snapshots`, so the tool
would have silently claimed measured headroom it did not have. It now requires
live metrics **and** a day of history.
