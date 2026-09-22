# Phase 8 — Validate

> **Latest update — 2026-09-21 (later): it found real data loss.**
> Run against the 32.9M-row migration. **Level 3: 11 of 11 comparable tables
> match exactly.** Level 4 then caught what nothing else did: Oracle `FLOAT`
> mapped to `DOUBLE PRECISION` had **truncated 38 significant digits to 15**
> on `SUBSCRIBER.RISK_FACTOR`. Row counts matched, DMS reported zero errors,
> and the load looked perfect. `convert/types.json` now maps `FLOAT` to
> `NUMERIC`.
>
> Four faults in the phase itself, each reporting as a *data* problem and none
> being one: results were unwrapped with `isinstance(x, list)` and pg8000
> returns a **tuple**, so every PostgreSQL value filed as "unreadable"; a dead
> connection stayed cached so one socket drop became twelve unreadable tables;
> the target connection had no keepalive, and a 60-second checksum sends no
> bytes while the server works; and Oracle's `TM9` drops the leading zero, so
> `0.023` rendered `.023` and hashed differently on 250,000 identical rows.
>
> **The screen states its verdict.** The conclusion used to live only in a 12px
> string beside the button. There is now a panel above the levels: the verdict,
> which levels compared *for real*, four counts, and every unexplained
> difference with its reason.
>
> Earlier — **2026-09-21 (the screen says what it needs).**
> `Run validation` shipped **enabled**, offering to compare a target that may
> hold nothing against a source, and failing at the API. It now locks until a
> migration has completed *and* the target is running, and the empty state
> names which of the two is missing — with a button through to Phase 7. The
> lock is deliberately two conditions, not one: a running target with no rows
> on it is the case that reads as a clean validation and is not.
>
> Earlier — **2026-09-14 (cross-engine comparison).** Validation now
> works against a **PostgreSQL** target as well as an Oracle one.
> `validate/crossengine.py` reduces each row to a **canonical text form** on
> both sides before hashing, so trailing zeros, CHAR padding, timestamp zones
> and Oracle's empty-string-is-NULL stop producing false mismatches on data
> that moved perfectly. Both engines compute the same MD5 over that text — no
> `ORA_HASH` anywhere. **Proven against the real PostgreSQL**: the generated SQL
> runs, and the checksum matches Oracle's rendering computed independently in
> Python. Self-test `validate/selftest_crossengine.py` **32/32**.
>
> **Levels 2 and 5 do not run across engines**, and say so rather than
> reporting differences that are all expected.

> **Latest update — 2026-09-12 (validated).** With the security group pointed at
> the current operator address, all five levels ran against the live target:
> **50 named objects present; 72 columns matching on type, length, precision,
> scale and nullability; 40 constraints; 13 indexes; 11 of 11 tables matching on
> exact row counts; and a checksum of every row matching on all 11 tables** —
> 5.4 million rows a side in about 28 seconds (`LOAN_TXN` 3,000,000 in 14.0 s,
> `PAYMENT_HIST` 1,800,001 in 7.7 s). Behaviour: grants, sequences, the
> materialized view `FRESH`, the external table reading its file on RDS. Six
> expected differences, each traced to a recorded decision, a 21c/19c difference
> or the source-only discovery account. **Verdict `validated`, zero mismatches.**
> The target billed about a minute and was stopped again automatically.
>
> One check passed vacuously and has been fixed: the text-index search asked for
> "the", got 0 on both sides, and called that a match. It now takes a word from
> the data and treats "no matches anywhere" as *not comparable*. Chasing that
> down found a real defect **in the source estate**: `IX_COMM_NOTES_TEXT` holds
> **zero tokens** (`DR$…$I` is empty) while reporting `INDEXED` / `VALID`, so
> application searches on the source silently return nothing. The migrated target
> does not share it — Phase 7 rebuilt that index after the load, and it holds
> 13,896 tokens over all 400,000 rows.
>
> **Previous — 2026-09-12 (first run: nothing validated, and it said
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

**2026-09-20 — the levels list actually scrolls, and a bug in the shared
layout rule.** Phase 8's console screen did not cap its list: `#valLevels`
carries `.scrollcap` and still rendered **2089px tall, hanging 1743px below
the bottom of the pane**, so the whole page scrolled instead of the list.

The cause was not in this phase. The rule that hands a view's leftover height
to its capped list has two halves, and only the second was guarded:

```css
.view.on:has(.scrollcap) > *:not(:has(.scrollcap)),                    /* was missing :not(.scrollcap) */
.view.on:has(.scrollcap) *:has(.scrollcap) > *:not(:has(.scrollcap)):not(.scrollcap):not(...)
```

**A list does not contain itself**, so `*:not(:has(.scrollcap))` matches the
`.scrollcap` as well and pinned it to `flex:0 0 auto`. Every other view
survived because its list sits inside a result wrapper and is therefore
matched by the guarded second half; Phase 8 is the only view whose list is a
**direct child of the section**, so it was the only one that broke. One
`:not(.scrollcap)` on the first half fixes it — `#valLevels` now caps at
458px and scrolls its 2089px of content, and the pane does not scroll at all.

Two smaller things on the same screen:

- **The cross-engine explanation was 202px of prose** permanently above the
  fold, taken straight off the list below it. The claim is one sentence and
  the working is on the hint now, where the rest of the console puts it —
  202px → 91px.
- **`mismatch · 1 mismatch(es)`** said the same fact twice, because the status
  word and the count are the same thing when the status *is* `mismatch`. Now
  `1 mismatch(es)`, via `valStatusLine()`, which both the restore path and the
  live path share.

Nothing in `validate/` changed. `check_overlap.js` 17/17.

**2026-09-14 — cross-engine comparison.** `validate/crossengine.py` added:
canonical text expressions per engine, a shared MD5 checksum, the list of
differences that are *correct*, and the columns no text form can compare.
`context.Options` gained `target_engine` and `target_password`; `Ctx` gained
`cross_engine` and a PostgreSQL connection; `Ctx.both` gained `target_sql` and
**refuses** a cross-engine call without it, because sending Oracle SQL to
PostgreSQL turns a syntax error into an apparent data difference.

**What each level does across engines:**

| Level | Oracle target | PostgreSQL target |
|---|---|---|
| 1 Objects | every object matched by name | **tables** matched by name; everything else counted, because a package becomes one function per member and DMS migrates no sequences |
| 2 Structure | columns, constraints, indexes | **not comparable** — the types are deliberately different |
| 3 Row counts | exact counts | exact counts, target names lower-cased |
| 4 Data content | `ORA_HASH` both sides | **canonical text, MD5 both sides** |
| 5 Behaviour | invalid objects, grants, sequences | **not comparable** — Oracle catalogue concepts; Phase 7's residue covers the equivalent |

One bug found by running the SQL rather than reading it: the PostgreSQL numeric
expression used `to_char` with an `FM` mask, which leaves a **trailing decimal
point** on a whole number (`10.` for `10`) where Oracle's `TM9` does not. Every
integer column would have mismatched. A plain `::text` cast after `trim_scale`
produces exactly what `TM9` does.


**2026-09-12 — validated, and a vacuous check found a source defect.** Full run
as summarised at the top: five levels, zero mismatches, six expected differences.
Two fixes came out of it. The text-index check searched for a hardcoded "the",
matched nothing on either side and scored 0 = 0 as a match; it now takes a word
from the data and reports *not comparable* when the search matches nothing on
the source. Investigating that revealed the source's `IX_COMM_NOTES_TEXT` is
**empty** — `DR$…$I` holds no rows, though the index reports `INDEXED` and
`VALID` and has nothing pending sync — because it was created before the data
was generated and never synced. It is a real source-side defect, worth an
assessment rule: *a CONTEXT index whose token table is empty*. Discovery already
collects everything needed to spot it.

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
