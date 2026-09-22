# Phase 1 — Discover

> **Latest update — 2026-09-16 (later).** **The console's summary tiles now count
> what a client would count.** "Tables" reported the raw `DBA_TABLES` row count —
> **21 on `DBMIG_APP`, where the client has 9** — because it included the tables
> Oracle manages on their behalf: 7 Text index internals, a materialized view's
> container, a queue table, an mview log, an external table. The hint said
> "including Oracle internals", which explained the number without making it
> reconcilable, and a client counting their own schema would conclude the tool
> was wrong about an estate they know better than we do. Objects went 90 → 71 the
> same way. The classification is Phase 2's own `v_user_tables`, and
> `web.selftest_counts` (22/22) fails if the two ever disagree on a collected run.
>
> Earlier — **2026-09-16.** **Discovery now asks how the estate is moving:
> full load, or full load plus CDC.** The answer decides whether three CRITICAL
> findings are blockers or irrelevancies — `OPS-001` (NOARCHIVELOG), `OPS-002`
> (no supplemental logging) and `DQ-001` (no primary key) all exist only because
> change data capture reads redo. Until now the mode was a Phase 7 flag, so every
> run assessed as though CDC were in scope and the gate halted on all three:
> on `DBMIG_APP` that is **4 CRITICALs where only 1 is real**, sending a client
> to schedule a database restart they may not need. The mode is recorded in the
> manifest and read by Phases 2, 5 and 7. Self-test 55/55.
>
> Earlier — **2026-09-10.** **Run against a second estate (`DBMIG_TELCO`,
> 5.67 GB, 32.7M rows) and two bugs fell out that `DBMIG_APP` could not show.**
> A single ungranted table made the profiler skip every alphabetically-later
> table in the schema without attempting it — silently losing the row data two
> seeded defects depended on. And empty datasets carried no column names, which
> broke five assessment rules downstream. Both fixed; see the change log.
> Verify reconciled 5/5 on the new estate (68 objects, 78 constraints).
>
> Earlier the same day: `collector.run.main` was split into
> `execute(cfg, on_event, enabled_probes, extra_probes)`, shared by the CLI and
> the console so there is one orchestration path rather than two that drift.
> Probes can now be switched off and custom query-probes added from the console.
> Earlier the same day: PL/SQL source moved out of line, and the console's
> schema list stopped being hardcoded.

## Purpose

Inventory the source estate. Everything downstream reads only what this phase
captured — no later phase reconnects to Oracle. If it is not collected here, it
does not exist as far as the rest of the pipeline is concerned.

## The question this phase asks

**Full load, or full load plus CDC?** It is asked here, before anything is
collected, because it changes what the whole run *means* rather than only what
Phase 7 does.

| | Full load | Full load + CDC |
|---|---|---|
| What happens | one copy into an outage window | copy, then replicate the changes since |
| Outage | as long as the load | minutes — the cutover waits for CDC to catch up |
| Needs from the source | nothing | ARCHIVELOG **and** supplemental logging |
| `OPS-001` / `OPS-002` / `DQ-001` | not applicable | CRITICAL blockers |

**The default is full load**, and an undeclared run is recorded as `declared:
false`. A full load is the weaker claim — it needs nothing from the source that
is not already true. Defaulting to CDC would silently assert a source
configuration nothing has verified.

**Declaring a mode never suppresses a finding.** A CDC-only blocker on a
full-load run is still found, still reported, and keeps its original severity in
`severity_if_applicable`. It is marked not-applicable *with the reason*. "Does
not block the migration you chose" is a different claim from "clean", and the
record must never let one read as the other.

CDC may be declared against a source that is not ready for it. That is a
remediation task with a restart window attached, not a reason to refuse the
declaration — the run records the gap as a warning and Phase 5 still blocks on
it.

## What actually happens

`collector/run.py :: execute()`

0. **Record the migration mode.** `collector/mode.py :: decide()` resolves the
   declared mode, and takes its CDC-readiness evidence from the rows the
   `identity` probe already collected — `log_mode` and
   `supplemental_log_data_min` — rather than issuing a second query. If that
   probe was switched off the evidence is absent and the record says so.
1. **Resolve scope.** `_resolve_owners()` queries `DBA_USERS` for schemas Oracle
   does not flag `ORACLE_MAINTAINED`. Configured schemas are verified to exist;
   anything discovered but not configured is **reported, not silently included**,
   so schema drift is visible.
2. **Run each probe in order.** 12 built-in probes plus any custom ones. Each
   owns one area of the data dictionary and knows only its own queries, so adding
   coverage never touches the rest. `on_event` fires `probe_start` / `probe_done`,
   which drives the live view in the console.
3. **Externalise unbounded fields.** `_externalize()` reads an `EXTERNALIZE`
   declaration on the probe. PL/SQL source is written to
   `<run_id>/plsql_source/<sha256>.txt`, leaving an 800-character excerpt and a
   pointer in the row.
4. **Write one file per dataset.** Each carries the same envelope: run id,
   dataset name, schema version, the queries that produced it, and rows whose
   keys match the future `source_inventory.*` column names.
5. **Write the manifest.** Every statement executed, with its SHA-256, row count
   and duration. This is what lets a client DBA audit exactly what ran.
6. **Append to the ledger.** `output/runs.jsonl`, append-only.

**56 fixed statements plus one scan per profiled table.** Full detail of every
statement is in [`../11-checks-catalogue.md`](../11-checks-catalogue.md).

### The data profile

`probes/dataprofile.py` is the only probe that reads **row data**; everything
else reads metadata. Metadata decides what is worth asking:

- A column is checked for duplicates only if the optimizer already believes it is
  ~95% unique *and* nothing enforces it. **Stats propose, the scan disposes.**
- Text columns are checked for characters outside ASCII.
- Both checks fold into **one aggregate per table**, not one per column.
- Above `DBSHIFT_PROFILE_MAX_ROWS` (2M) the table is **block-sampled** to
  `DBSHIFT_PROFILE_SAMPLE_ROWS` (1M) rather than skipped.

**Counts come back, never rows.**

## Inputs / Outputs

| | |
|---|---|
| Input | A read-only Oracle account. `SELECT_CATALOG_ROLE` + `CREATE SESSION` |
| Input | Optional per-table `SELECT` for the data profile |
| Output | `collector/output/<run_id>/` — one JSON file per dataset, plus `manifest.json` |
| Output | `collector/output/runs.jsonl` — append-only ledger |
| Config | `DBSHIFT_COLLECTOR_PASSWORD` (required), `DBSHIFT_DSN`, `DBSHIFT_COLLECTOR_USER`, `DBSHIFT_SCHEMAS` |
| Config | `DBSHIFT_MIGRATION_MODE` — `full-load` (default) or `full-load-and-cdc`. `DBSHIFT_MIGRATION_MODE_BY` records who chose |
| Output | `manifest.json :: migration_mode` — the decision, who made it, and the CDC-readiness evidence behind it |

## Design decisions

**The migration mode is a Phase 1 question, not a Phase 7 flag.** It was a flag
on `dms.run` with its own default, which meant a run could be gated for one
migration and executed as another with nothing in the record contradicting it.
Worse, the assessment had no way to know, so it reported the CDC prerequisites as
CRITICAL on every run — including full-load migrations that never read redo.
`blocker/policy.py` already recorded that `OPS-001` blocks only `migrate_cdc`
and `cutover`; nothing ever told it those phases were not happening. Phase 7 now
defaults to what Phase 1 declared, and an explicit flag there is recorded as an
override rather than applied silently.

**The mode marks findings, it does not delete them.** `assess/engine.py ::
evaluate()` takes severity from the rule row and never computes it, which is what
makes an assessment reproducible. So the mode is a separate pass
(`apply_migration_mode`) that adds `applies`, `not_applicable_because` and
`severity_if_applicable` rather than editing severity in place. The answer key
grades `severity_if_applicable`, because "DQ-001 should call a missing primary
key CRITICAL" is a statement about the rule catalogue and stays true whichever
migration this run is.

**Read-only, thin mode, outbound only.** Connects as a read-only account, uses
`oracledb` thin mode so no Instant Client is needed, and pushes results outward.
Nothing connects inbound to the collector — security teams refuse those holes,
and it never needs one.

**Append-only, with a run id on every row.** The id is on the envelope *and* on
each row, so a row can be inserted independently of its file. That redundancy is
what makes the append-only rule work end to end.

**PL/SQL source lives out of line.** Full text inline would be hundreds of MB on
a real estate, making the file unloadable and unpushable. The SHA-256 was already
the identity used for skip-unchanged, so it doubles as the filename — identical
text is stored once.

**Schema names are bound, never interpolated.** The only interpolated identifiers
are in the data profile, where Oracle cannot bind them; those are validated
against a strict pattern first.

**Sampling changes what a clean result means**, so it is recorded. A sampled scan
sets `actual_rows = NULL` — inflating a sample back up would be a guess presented
as a measurement.

## Known limits

- **Sampling cannot find rare events.** Two duplicates in three billion rows will
  not surface in a 0.03% sample. `DQ-011` marks every sampled table; it does not
  remove the limit.
- **No utilization percentiles.** `V$SYSMETRIC` is empty on XE and AWR is not
  licensed there. `V$OSSTAT` is captured as a point-in-time snapshot instead.
- **The CLI does not auto-discover schemas.** It runs exactly what
  `DBSHIFT_SCHEMAS` names, deliberately, so a scripted run cannot be reshaped by
  whatever schemas happen to exist. The console *does* auto-discover.
- **No preflight in the CLI.** The console runs one; the CLI still fails on the
  query that first hits a missing privilege.
- **Catalogue is 21c-shaped.** 19c and 23ai differ in available views.

## How to run it

```bash
export DBSHIFT_COLLECTOR_PASSWORD='...'
python -m collector.run                                  # defaults to full load
python -m collector.run --migration-mode full-load-and-cdc                         --mode-chosen-by 'name@client.com'
python -m collector.verify         # reconcile two runs against ground truth
python -m collector.selftest_mode  # the mode, end to end. No database
```

Console: **Phase 1 - Discover**, with probe-by-probe progress.

## Change log

**2026-09-16 (later)** — **The summary tiles count user objects, not Oracle's.**
Client feedback: "Discover — check the count it is printing." It was right.
`_discovery_summary` in `web/server.py` reported `entry["row_count"]` straight
from the manifest, so `DBMIG_APP` showed **Tables 21** against a schema with
**9**, and **Objects 90** against 71.

What was being counted: `DR$IX_COMM_NOTES_TEXT$*` (7 Text index internals),
`MLOG$_LOAN`, `AQ$_LOAN_EVENT_QTAB_H`, plus — and these are the ones a prefix
check misses — `MV_LOAN_SUMMARY` (a materialized view's container),
`LOAN_EVENT_QTAB` (a queue table) and `EXT_CUSTOMER_EXTRACT` (an external
table). None of those three carry a `$`.

- The tile shows the user count; the hint says how many were set aside
  ("12 Oracle-managed tables excluded"), or "every table found" when there are
  none. **The raw count is still in the dataset list below**, so the two
  reconcile rather than one replacing the other.
- Counting happens in the endpoint over every collected row, **before** the
  200-row display cap, so it stays right on an estate bigger than the cap.
- The `owned` rule applies to tables only. Phase 2's `v_user_objects` filters on
  prefixes alone — a materialized view *is* a user object even though the table
  backing it is not. Applying `owned` to both under-reported objects by 4, which
  is what the new self-test caught.

`web/selftest_counts.py` added, **22/22**. Its last checks load the same run
through `assess.loader` and compare the tile with `v_user_tables` and
`v_user_objects` — the definition is duplicated because the view lives in SQLite
that Phase 2 builds and Phase 1 has not yet run, so the test is what keeps them
honest. Verified on four estate configurations (`DBMIG_APP`, `DBMIG_TELCO`, and
two multi-schema runs); the console agrees with Phase 2 on all four.
Browser drive: `scripts/console-test/drive_counts_export.js`, 26/26.

**2026-09-16** — **The migration mode is asked in Discovery.** `collector/mode.py`
added: the two modes, the readiness test, the decision record and the
rule-applicability map. Wired through `config` (`DBSHIFT_MIGRATION_MODE`, a CLI
flag), the manifest, the console (**Migration mode** on the Discovery screen,
`POST /api/migration-mode`), and then the three readers —

- **Phase 2** marks CDC-only findings not-applicable instead of CRITICAL, keeping
  the original severity in `severity_if_applicable`. The HTML report shows a
  "not a blocker for this migration" note on each, so a downgraded finding cannot
  be mistaken for a clean one.
- **Phase 5** drops `migrate_cdc` from the downstream list on a full-load run and
  reports it `not_in_scope` rather than `clear`. Its summary now names the
  migration it judged.
- **Phase 7** defaults `--migration-type` to what Phase 1 declared, and records
  `migration_type_overridden` when the flag disagrees.

Measured on the real `DBMIG_APP` run `2f67a47b`: **4 CRITICAL findings become 1**
on a full-load migration — only `RDS-004`, the external table, which blocks
either way. `cutover` moves from blocked to clear. Answer-key recall stays 7/7
with severity exact 7/7.

One bug found while building it: the HTML report copies grouped issues through a
fixed key list, so `applies` never reached the page and the note never rendered —
OPS-001 would have shown as a bare INFO with no explanation.

**2026-09-10 (later)** — **Two bugs found by running Phase 1 against a second
estate (`DBMIG_TELCO`), neither visible on `DBMIG_APP`.**

1. **One ungranted table blinded the profiler for the whole schema.**
   `dataprofile.py` treated a single `ORA-00942` as proof the owner held no data
   access and added it to an `unreadable` set, skipping every table processed
   after it. Tables are processed alphabetically, so an ungranted `"SESSION"`
   caused `SUBSCRIBER`, `SUPPORT_TICKET` and `USAGE_STAGING` to be skipped
   **without being attempted**, despite valid grants. Two seeded defects living
   in `SUBSCRIBER` were then reported as assessment misses — the collector had
   simply never looked. The owner-level short circuit is gone; each table is
   judged on its own attempt. Per-table grants are exactly what a least-privilege
   collector account has, so this would misfire on real client estates.

2. **Empty datasets carried no schema.** A dataset with zero rows was written as
   an empty row list, so a consumer had no way to know its shape. `Session.fetch`
   already read `cur.description` and discarded it; it now records the column
   names per query label, and `writer.write_dataset` stores them in the envelope
   as `columns`. Probes whose query label differs from the dataset name declare
   the mapping in a `QUERY_LABELS` dict rather than relying on the suffix
   matching by coincidence. See phase 2 for what this was breaking.

**2026-09-10** — `main()` split into `execute()` with an event callback and
probe filters, shared with the console. Probes can be disabled and custom
query-probes added.

**2026-09-09** — PL/SQL source externalised (`plsql_source.json` fell to 7 KB).
Large tables block-sampled instead of skipped; `LOAN_TXN` went from no evidence
at all to a 33% sample. Duplicate candidates narrowed to text and whole numbers
after two false positives on money columns; materialized-view containers excluded.

**2026-09-08** — Built. 12 probes, 47 datasets. Two bugs found by running it:
`VIRTUAL_COLUMN` does not exist on `DBA_TAB_COLUMNS` (it is on `DBA_TAB_COLS`),
and `SELECT_CATALOG_ROLE` grants catalogue access but not row access — which made
data profiling silently impossible until per-table grants were added.
