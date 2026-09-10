# Phase 1 — Discover

> **Latest update — 2026-09-10.** `collector.run.main` was split into
> `execute(cfg, on_event, enabled_probes, extra_probes)`, shared by the CLI and
> the console so there is one orchestration path rather than two that drift.
> Probes can now be switched off and custom query-probes added from the console.
> Earlier the same day: PL/SQL source moved out of line, and the console's
> schema list stopped being hardcoded.

## Purpose

Inventory the source estate. Everything downstream reads only what this phase
captured — no later phase reconnects to Oracle. If it is not collected here, it
does not exist as far as the rest of the pipeline is concerned.

## What actually happens

`collector/run.py :: execute()`

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

## Design decisions

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
python -m collector.run
python -m collector.verify     # reconcile two runs against ground truth
```

Console: stage 2, with probe-by-probe progress.

## Change log

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
