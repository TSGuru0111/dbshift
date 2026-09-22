# DBShift AI

AWS-native, human-in-the-loop AI agent for migrating on-premises Oracle to
Amazon RDS for Oracle. Company accelerator project.

**Start here:** `CLAUDE.md` for the map, then `docs/00-README.md` for the
documentation index.

## Run it against your own Oracle database

Nothing here is tied to the reference estate. Discovery, assessment and the
console all work against any Oracle database you can reach with a read-only
account.

### 1. Set up

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r collector/requirements.txt
```

### 2. Create a read-only account on your database

```sql
CREATE USER dbshift_collector IDENTIFIED BY "your-password";
GRANT CREATE SESSION, SELECT_CATALOG_ROLE TO dbshift_collector;
```

That covers discovery and every structural rule. **Optional:** to also profile
row data for duplicates and encoding problems, grant `SELECT` on the application
tables — `scripts/oracle-source/05_grant_collector_read.sql` shows the pattern
(explicit per-table grants, not `SELECT ANY TABLE`).

### 3. Start the console

```bash
.venv/Scripts/python.exe -m web.server        # http://127.0.0.1:8765
```

Enter your DSN, user and password. **Leave the schema field blank** and it
discovers your application schemas itself — anything Oracle does not flag as
`ORACLE_MAINTAINED`. Or name them explicitly, comma-separated.

The preflight tells you before anything runs whether the account can reach the
database, read the catalogue, read row data, and whether the database is ready
for change data capture.

### Or run it headless

```bash
export DBSHIFT_COLLECTOR_PASSWORD='...'
export DBSHIFT_DSN='your-host:1521/YOURSERVICE'
export DBSHIFT_COLLECTOR_USER='dbshift_collector'
export DBSHIFT_SCHEMAS='HR,SALES'          # required for the CLI — it does not auto-discover

.venv/Scripts/python.exe -m collector.run   # discover
.venv/Scripts/python.exe -m assess.run      # assess
.venv/Scripts/python.exe -m assess.report   # render an HTML report
.venv/Scripts/python.exe -m sizing.run      # size and edition decision
```

The CLI deliberately does **not** auto-discover schemas or read the console's
toggles — it always runs the shipped catalogue against exactly what you name, so
a scripted run cannot be quietly reshaped by someone's UI preferences.

### What will differ on your database

- **Recall will show `n/a`.** The 100% recall figure is measured against eight
  defects deliberately seeded into the `DBMIG_APP` reference estate. On any other
  database that metric is meaningless, so it is reported as not applicable rather
  than as 0%. Your findings are still real.
- **Counts in `docs/` describe the reference estate**, not yours.
  `collector.verify` compares against them and reports the difference as drift;
  set `DBSHIFT_PRIMARY_SCHEMA` to point it at your own schema.
- **Data-quality rules stay quiet without row-read grants.** `DQ-010` says so
  explicitly rather than letting silence look like a clean bill of health.

## Layout

```
CLAUDE.md                  Read automatically by Claude Code. The map.
docs/                      All reference material, numbered in reading order
scripts/oracle-source/     SQL that builds the synthetic source estate
data/                      Golden Data Pump dump (gitignored)
collector/                 Discovery — 12 probes over the data dictionary
assess/                    Assessment — 50 rules stored as data
sizing/                    Size and edition decision, with an OLA input hook
bedrock/                   Model tier bindings and client
web/                       The operator console
infra/                     CDK / CloudFormation — later
```

## Status

Phases 1–3 complete and running locally with no AWS dependency: discovery,
assessment at 7/7 recall against the seeded answer key, and the size/edition
decision. Phase 4 (Detect & Remediate) is blocked on Bedrock invoke — see
`docs/05-aws-services.md`.
