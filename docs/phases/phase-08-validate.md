# Phase 8 — Validate

> **Latest update — 2026-09-12 (first run: nothing validated, and it said
> "validated").** The run reached the source but every connection to the target
> failed with `DPY-6005`, because the security group admits the single address
> the stack was deployed with (`14.97.44.14/32`) and this machine's address had
> since changed (`49.206.98.235/32`). **The report still said `validated` and
> exited 0**, because the verdict failed only on mismatches and every finding was
> *not comparable*. Fixed three ways: `not_comparable` now yields **`incomplete`**
> and a non-zero exit; levels 3 and 4 no longer read "0 of 0 match" as a pass;
> and a **reachability check** runs first, comparing the security group's rule
> with this machine's address and saying so in one line instead of eleven
> connection errors. Re-run pending a security-group update.
>
> **Previous — 2026-09-12. Built; first run pending.** Five levels compare the
> RDS target against the estate it was built from, and **nothing is written to
> either database — every statement is a SELECT**. Each finding is *match*,
> *expected difference* (with the reason it is correct), *mismatch*, or *not
> comparable* when one side cannot be read. Only mismatches fail the phase. The
> estate compared is **the collector run named on the target's own tag**, not the
> newest files on disk: on 2026-09-11 the output folders moved on to a later
> discovery run while the target still held the earlier one.

## Purpose

Phase 7 moved the data and counted rows. Phase 8 answers a harder question: *is
this the same database?* Equal row counts prove very little — a column can hold
different values, a constraint can arrive unvalidated, an index can be missing,
code can be invalid, a sequence can be set to reissue numbers already used.

## What actually happens

`validate/run.py :: execute()` runs `validate/levels.py :: LEVELS` in order. Every
level runs; unlike Phase 7 nothing stops at the first problem, because a
validation report that stops halfway is not a report.

| Level | What it compares |
|---|---|
| **1. Objects** | Every object by **name and type**, source against target. Oracle-generated names (`DR$`, `SYS_`, `AQ$_`, `MLOG$`) are counted separately, never matched by name |
| **2. Structure** | Columns (type, length, precision, scale, nullability); constraints by **shape** — table, type, column list, validated flag — since Oracle names some itself; indexes by table, type, uniqueness and column list |
| **3. Row counts** | Exact `COUNT(*)` on both sides, table by table |
| **4. Data content** | `SUM(ORA_HASH(...))` over every row, so equal counts cannot hide changed values |
| **5. Behaviour** | Invalid objects, grants, sequence positions, the scheduler job's state, the materialized view's freshness, the external table actually reading its file, a **CONTAINS search** against the rebuilt text index, database links, the password profile, and each check Phase 4 set aside for this phase |

## Inputs / Outputs

| | |
|---|---|
| Input | the collector run named on the target's `collector_run_id` tag, and its discovery datasets |
| Input | `DBSHIFT_COLLECTOR_PASSWORD` — the read-only source login |
| Input | the master password from SSM, for the target |
| Output | `validate/output/validation_report.json`, plus `output/runs/<when>-<status>.json` per run |
| Exit code | `0` only when there are no mismatches |

## Design decisions

**Validate against the run the target was built from.** Read from the instance's
own tag. Using "the latest records" would have compared the database against
facts it was never built from — which nearly happened on 2026-09-11, when the
output folders advanced to a new discovery run two hours after the migration.

**Four verdicts, not two.** *Not comparable* exists because the read-only
discovery account cannot read every object on the source. Counting that as a
match is how Phase 7's first count claimed 18 of 21 tables matched when the
honest figure was 11 of 11.

**An expected difference must name its cause.** A decision recorded earlier
(the profile left behind for `SEC-006`, the job disabled by Phase 4's advice), a
version difference between 21c and 19c (Oracle Text internals), or an account
that exists only on the source (the discovery user). Anything that cannot be
traced to one of those is a mismatch, not a shrug.

**Checksums compare values, not sizes.** Numbers, dates and timestamps are
formatted explicitly and both sessions are set to the same NLS, or the same data
would hash differently for no reason. LOB, XMLType and object columns are
excluded and **reported as excluded** rather than compared badly; their tables'
other columns are still compared. `ORA_HASH` takes 4,000 characters, so wider
rows are truncated — recorded in the evidence.

**Behaviour means behaviour.** The text index is asked to answer a `CONTAINS`
search, not merely to exist; the external table is read, not merely present.

**Read-only, and provably so.** Every statement is a SELECT. Phase 8 can be run
against a production target without an approval, which is the point of keeping
it separate from anything that changes.

## Known limits

- **First run pending**, so the levels are unproven against the real target.
- **Level 4 reads every row on both databases.** On this estate that is about 5.4
  million rows a side. It can be switched off (`--no-checksum`, or the console
  checkbox), which is honest about what was and was not compared.
- **The source side depends on the discovery account's grants.** Whatever it
  cannot read is reported as *not comparable*, never as a match.
- **Statistics are not compared.** Two databases can hold identical data with
  entirely different statistics; that is Phase 4's business, not this one's.

## How to run it

```powershell
python -m validate.run                 # all five levels
python -m validate.run --no-checksum   # skip level 4
```

**Console: Phase 8 · Validate**, which opens once a migration has completed. One
card per level, each finding marked with its verdict, its reason, and its
evidence.

## Change log

**2026-09-12 — a validation that validated nothing, and passed.** First run:
source read fine, every target connection refused, and the report said
`validated` with exit 0. Three fixes, in order of seriousness: the status is now
`incomplete` (exit 1) whenever any check could not be made, not just on
mismatches; levels 3 and 4 report "no table could be compared on both sides"
rather than "0 of 0 match exactly"; and `context.reachability()` runs before the
levels, comparing the security group's allowed CIDRs with this machine's public
address and reporting the difference plainly. The cause was mundane and will
recur: the deploy pins one operator address into the security group, and a home
or office address changes.

**2026-09-12 — built.** `validate/context.py` (estate resolution from the
instance tag, both read-only connections, NLS normalised on each),
`validate/levels.py` (the five levels), `validate/run.py` (orchestration, CLI,
per-run reports), console stage 8. Phase 7's run records now also keep one file
per run: writing only to `migration_run.json` had already lost the record of a
successful migration, approvals included, when a later run stopped at the gate
and overwrote it.
