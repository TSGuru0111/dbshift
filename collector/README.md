# Discovery collector

Reads the source Oracle estate and writes an inventory as local JSON.
Spec: `../docs/08-next-tasks.md`. Findings: `../docs/07-build-log.md` Phase 1.

## Run

```
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r collector/requirements.txt

$env:DBSHIFT_COLLECTOR_PASSWORD='...'
.\.venv\Scripts\python.exe -m collector.run
.\.venv\Scripts\python.exe -m collector.verify
```

Run from the repo root, not from `collector/`.

| Variable | Default | |
|---|---|---|
| `DBSHIFT_COLLECTOR_PASSWORD` | — | **required**, never stored in a file |
| `DBSHIFT_COLLECTOR_USER` | `dbmig_collector` | read-only account |
| `DBSHIFT_DSN` | `localhost:1521/XEPDB1` | service name, not the SID |
| `DBSHIFT_SCHEMAS` | `DBMIG_APP,DBMIG_RPT,DBMIG_COLLECTOR` | comma-separated |

## Guarantees

- **Read-only.** Connects as `dbmig_collector` (`SELECT_CATALOG_ROLE` +
  `CREATE SESSION`). It issues no DDL or DML.
- **Thin mode.** No Oracle Instant Client.
- **Append-only.** One `collector_run_id` per run, stamped on the envelope *and*
  every row. Nothing is ever updated in place.
- **Auditable.** `manifest.json` records every SQL statement, its SHA-256, row
  count and elapsed time. Data and credentials are never logged.
- **Schema names are bound**, never interpolated into SQL.

## Output

```
collector/output/
  runs.jsonl                    append-only ledger, one line per run
  <collector_run_id>/
    manifest.json               run metadata + full SQL log
    objects.json  tables.json  columns.json  ...   (45 datasets)
```

Every dataset file shares one envelope:

```json
{
  "collector_run_id": "…", "dataset": "source_inventory.objects",
  "schema_version": 1, "collected_at_utc": "…",
  "probe": "objects", "source_queries": ["objects.census"],
  "row_count": 90,
  "rows": [ { "collector_run_id": "…", "owner": "DBMIG_APP", … } ]
}
```

Row keys are flat and lowercase, matching the future `source_inventory.*`
column names. The AWS push step is `for file in run_dir: POST` with the ingest
Lambda routing on `dataset` — no transform layer.

## Known source limitations

- `V$SYSMETRIC` is empty on 21c XE, so **utilization percentiles are not
  available from this estate**. `V$OSSTAT` is collected as a point-in-time
  snapshot instead. Do not present percentile-based sizing from this source.
- AWR exists here but with 3 snapshots and no Diagnostic Pack licence. A
  production `DBA_HIST_*` probe must be explicitly gated for licensing.

## Verify

`verify.py` compares the two most recent runs and reconciles against a **fresh
independent query**, not the collector's own output. It also reports drift
between the live database and `../docs/03-source-estate.md`.
