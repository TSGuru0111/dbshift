# MySQL as a source engine

> **Status — 2026-09-29 (Phases 4–5).** Remediate, convert (4b), schema DDL (4c),
> application SQL (4d) and the gate (5) run on the MySQL path, against the EC2 estate,
> with Bedrock live. MySQL -> RDS for PostgreSQL: 10 SCT items all routed, 7/7 routines
> ready for approval, 84/84 DDL statements compiled, 17 of 23 seeded application
> statements converted and parsed, gate PROCEED. MySQL -> RDS for MySQL: SCT returns
> zero items and the gate says why. Section **Phases 4 and 5** below.
>
> **Status — 2026-09-28 (later).** Phase 1 is built and **run against a live
> MySQL 8.0.46**, with a seeded estate and all twelve defects verified present.
> `engines/` 47/47, `collector/selftest_probes_mysql` **81/81**, discovery
> **0 failed queries** over ~205,000 rows in 2.3s.
>
> The live run found **four bugs the offline test could not**, which is the whole
> argument for running it: two SQL faults, one driver quirk and one missing grant
> that would have reported an estate with no stored code at all. They are written
> up under **What the live run found** below, because each is the kind of thing
> that would otherwise be re-derived.
>
> **Phase 2 is proven too, 2026-09-29**: real SCT 1.0.677 produced 34
> occurrences across 10 action items for MySQL → PostgreSQL, parsed with zero
> unmapped columns. Three more live-only findings came out of it — see
> **Phase 2: real AWS SCT on the MySQL path**.
>
> **The estate now lives on EC2, and SCT has assessed it there.**
> `dbshift-source-mysql` (`i-0bd32544c1577b5ed`, t3.medium, MySQL 8.0.46) runs in
> the same VPC as the Oracle host; SCT's own CSV records `3.108.190.1:3306` as the
> server it read. Four more findings came out of that move, including an SCT cache
> collision that served a container's report for an EC2 run —
> `scripts/mysql-source/EC2-HOST.md` has all four with their measurements.
>
> Phases 3–10 are not started.

## Why a second source engine, and what shape it took

DBShift migrated one source engine to two targets. The client now also needs
MySQL, to both RDS for MySQL and RDS for PostgreSQL. The decision was a
**source-engine adapter layer** rather than a parallel `mysql/` tree, because
almost everything that makes the Oracle path trustworthy is engine-independent:
the rule catalogue is data, the five gates are policy, the cross-engine checksum
is a canonical text form, and the console rail is a list.

What is genuinely engine-specific turned out to be small and concentrated:

| Concern | Where it lives now |
|---|---|
| Engine names, legal `(source, target)` pairs | `engines/__init__.py`, `engines/spec.py` |
| Connect, driver error class, bind style | `collector/dialect.py` |
| Catalogue SQL | `collector/probes/` (Oracle), `collector/probes_mysql/` (MySQL) |
| Everything else | unchanged |

## The four legal pairs

`engines/spec.py` is the only place that knows these, and `pair()` refuses
anything else by name rather than returning `None`:

| Source | Target | Kind | Converts code | Data mover |
|---|---|---|---|---|
| Oracle | RDS for Oracle | homogeneous | no | Data Pump |
| Oracle | RDS for PostgreSQL | heterogeneous | yes | DMS |
| MySQL | RDS for MySQL | homogeneous | no | DMS |
| MySQL | RDS for PostgreSQL | heterogeneous | yes | DMS |

**MySQL → RDS for Oracle and Oracle → RDS for MySQL are refused.** Both are easy
to ask for by accident once the source picker and the target picker each offer
two engines.

**Both MySQL paths use DMS.** Unlike Oracle, where Data Pump serves the
homogeneous path, there is no MySQL equivalent worth a separate implementation and
DMS reads the binlog for CDC on either target.

## Connecting

```powershell
$env:DBSHIFT_SOURCE_ENGINE      = 'MYSQL'
$env:DBSHIFT_DSN                = '10.0.1.42:3306/dbmig_mysql_app'
$env:DBSHIFT_SCHEMAS            = 'dbmig_mysql_app'
$env:DBSHIFT_COLLECTOR_PASSWORD = '...'
.\.venv\Scripts\python.exe -m collector.run
```

`DBSHIFT_SOURCE_ENGINE` accepts `MYSQL`, `MySQL`, `my_sql`, and `MariaDB`
(wire-compatible, and DMS treats it as a MySQL source — but nothing here has been
tested against MariaDB, so the docs claim compatibility, not support). Unset means
`ORACLE`, so every existing script behaves exactly as before.

**The DSN is `host:port/database`, not `host:port/service`.** MySQL has no service
name; the third field is a default database and is optional. A non-numeric port is
refused rather than defaulted, because `host:33o6/db` is a typo and silently
connecting to 3306 would hide it.

### Schema-name case is load-bearing

**A MySQL schema is a directory on disk.** On Linux `Sales` and `SALES` are
different databases. `DBSHIFT_SCHEMAS` is therefore **not** upper-cased on the
MySQL path, where it is on Oracle's — driven by `fold_schema_names` in
`engines/spec.py`.

This was a real bug before it was a design decision: the original
`_schemas_from_env()` upper-cased unconditionally, which would have made the
collector look for a schema that does not exist, find nothing, and produce an
empty run indistinguishable from an empty estate. It is the third time this
project has been bitten by an identifier-case assumption.

## Privileges the collector needs

```sql
CREATE USER 'dbmig_collector'@'%' IDENTIFIED BY '...';
GRANT SELECT, SHOW VIEW, EXECUTE, TRIGGER, EVENT
  ON dbmig_mysql_app.* TO 'dbmig_collector'@'%';
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'dbmig_collector'@'%';
-- Dynamic privilege, 8.0.20+, and there is no schema-scoped form of it.
GRANT SHOW_ROUTINE ON *.* TO 'dbmig_collector'@'%';
```

| Privilege | Without it |
|---|---|
| `SELECT` | no row data, so no profiling and no exact counts |
| `SHOW VIEW` | `view_definition` comes back **empty, not denied** |
| `EXECUTE` | `information_schema.ROUTINES` returns **zero rows** for the schema |
| `TRIGGER` | `information_schema.TRIGGERS` returns **zero rows** |
| `EVENT` | `information_schema.EVENTS` returns **zero rows** |
| `SHOW_ROUTINE` | routines are listed but `routine_definition` is **NULL**, so Phase 4b gets no code |
| `PROCESS` | no server-wide status |
| `REPLICATION CLIENT` | `SHOW MASTER STATUS` fails, so CDC readiness cannot be read |

All eight were **measured**, not assumed — see the table under *What the live run
found*. The first five each produce silence rather than an error.

**The failure modes differ from Oracle's in a way that matters.** Oracle refuses a
query outright when the grant is missing — `SELECT ANY DICTIONARY` is checked by
name and SCT will not connect without it. MySQL's `information_schema` is readable
by anyone, but **shows only what the account can see**. So a missing grant does not
produce "access denied"; it produces *fewer rows*, which looks like a smaller
estate. Anything that reports "0 views" on the MySQL path should be treated as a
grant question first and an estate question second.

`mysql.user` is deliberately not read — `information_schema.user_privileges`
answers the same questions and needs no grant on the `mysql` schema, which is
wider than a read-only profiling account should hold.

## What the MySQL probes collect

Ten probes, reusing Oracle's `NAME`s so the console's toggles and descriptions
work on either engine. **All 47 Oracle dataset names are emitted**, because
`assess/loader.py` builds one SQLite table per dataset and the 51 rules and the
`v_user_*` views are SQL over them.

### Five new datasets, and why each exists

| Dataset | The finding it carries |
|---|---|
| `mysql_table_storage` | A **MyISAM** table has no transactions, no crash recovery, and **DMS CDC cannot track it**. Must be InnoDB before migration. |
| `mysql_charsets` | **`utf8mb3`** (legacy `utf8`) cannot store a 4-byte character — emoji and some CJK truncate. A **`_ci` collation** makes `'A' = 'a'` true on the source and false on PostgreSQL, which changes row identity and uniqueness, not just rendering. |
| `mysql_binlog` | `binlog_format`, `binlog_row_image`, retention, GTID — the CDC prerequisites. |
| `mysql_binlog_position` | `SHOW MASTER STATUS`; also proves `REPLICATION CLIENT` is granted. |
| `mysql_tables_without_pk` | On Oracle this is `DQ-001` and matters only for CDC. On MySQL it is worse: InnoDB has a hidden clustered index DMS cannot address, so an UPDATE or DELETE has no reliable way to find its target row. |

### The 22 datasets MySQL has no analogue for

Emitted **shaped and empty**, each with a recorded reason in
`selftest_probes_mysql.NO_MYSQL_ANALOGUE` — `tablespaces`, `awr_coverage`,
`profiles`, `synonyms`, `db_links`, `queues`, `materialized_views`, `mview_logs`,
`directories`, `external_tables`, `text_indexes`, `xml_schemas`, `types`, `lobs`,
`database_options`, `nls_parameters`, `os_statistics`, `system_metrics`, `roles`,
`index_expressions`, `plsql_errors`, `invalid_objects`.

They are emitted rather than omitted because an undeclared empty dataset produces
a column-less SQLite table, and every rule referencing it then dies with "no such
column" — a failure that looks like a broken rule three phases downstream.

### CDC readiness, in Oracle's vocabulary

Oracle's ARCHIVELOG + supplemental logging and MySQL's binlog settings answer the
same question, so `identity.py` maps MySQL's onto the existing columns that
`collector/mode.readiness()` already reads:

| MySQL | Renders as | Because |
|---|---|---|
| `log_bin = ON` | `log_mode = ARCHIVELOG` | is a replication log retained at all |
| `binlog_format = ROW` **and** `binlog_row_image = FULL` | `supplemental_log_data_min = YES` | supplemental logging exists so the log carries enough column data to rebuild an UPDATE; `FULL` row image is exactly that guarantee, and `ROW` is what makes it meaningful |

So `mode.readiness()`, the gate, and the console's existing Log-mode row work with
no MySQL branch — while the native readings stay alongside, unaltered, in
`mysql_binlog`.

`cdb` and `con_name` are **NULL**, not `'NO'`: there is no container question to
answer on MySQL, and `'NO'` would assert one.

## Row counts: what to trust

**`information_schema.TABLES.table_rows` is an estimate.** It is InnoDB's sampled
count and routinely 20-50% wrong. It lands in `num_rows` because Oracle's column
means the same kind of thing (last-gathered statistics) — and **Phase 8 must never
compare it.**

The exact count comes from `dataprofile.py`, which counts. Above
`DBSHIFT_PROFILE_MAX_ROWS` (2,000,000) it samples, and MySQL has no `SAMPLE`
clause, so:

- **single-column integer primary key** → modulus on that key, a real spread;
- **anything else** → `LIMIT`, which is a **prefix, not a sample**, and is
  labelled as such in `skip_reason`.

A prefix reported as a sample would be a lie about coverage, and every duplicate
count built on one would describe only the first N rows.

## What is proven, and what is not

**Proven:**

- Dataset parity — all 47 Oracle datasets emitted; every table the rule catalogue
  queries resolves; MySQL-only datasets namespaced `mysql_` so they cannot collide.
- Schema names are bound, never interpolated, on every query every probe issues.
- Marker count equals bind count for every query — the UNION arithmetic in
  `objects.py` is checked mechanically, not by eye.
- Every probe query is read-only.
- Identifier interpolation is guarded; `` a`b ``, `a b`, `a;drop`, `a'b`, `a-b` are refused.
- DSN parsing, including refusing malformed input rather than defaulting.
- The CDC mapping, across all four log_bin/format/row_image combinations.
- **Oracle is unaffected:** 28/28 selftest modules; live `DBMIG_APP` discovery 48
  datasets and `collector.verify` 5/5; assessment recall 7/7 on `DBMIG_APP` and
  14/14 on `DBMIG_TELCO`, severity exact on both.

**Proven against a live MySQL 8.0.46 (2026-09-28):**

- Discovery runs clean: **56 datasets, 0 failed queries**, ~205,000 rows in 2.3s.
- All 12 seeded defects appear in the collected data, and `verify_defects.py`
  reports **12/12** independently of the seeder's own proof block.
- Stored code arrives with bodies: 4 routines (431–1569 chars), 2 triggers,
  1 event, 5 RANGE partitions with 4 populated.
- The CDC mapping resolves correctly through the existing `mode.readiness()`:
  `log_bin=1 + ROW + FULL` → `ARCHIVELOG` / `supplemental=YES`, `cdb=None`.
- The estate is re-seedable end to end: `00_reset` → `01` → `02` → `03` → `04`
  → verify, in about 15 seconds.

**Not proven:**

- `information_schema.check_constraints` on a server older than 8.0.16. It is
  fetched in isolation so an older server empties that one dataset rather than
  failing the probe, but the fallback is untested.
- MariaDB. `normalize()` accepts the name; nothing has run against it.
- Phases 2–10 on the MySQL path.
- The **EC2 host**. Everything above ran against a Docker MySQL on this machine
  (`scripts/mysql-source/run_mysql.ps1`). `dbshift-source-mysql` is not yet
  provisioned.

## What the live run found

Four bugs, none of which the offline selftest could have caught. Each is now a
regression test in `collector.selftest_probes_mysql`.

### 1. `%` in SQL is a format specifier to pymysql

pymysql interpolates binds with `query % args`, so `LIKE '%TEMPORARY%'` raised
`ValueError: unsupported format character 'T'`. A fake cursor never interpolates,
so the offline test passed happily.

The fix is `MySQLDialect.prepare_sql()`, which doubles a literal `%` **only when
the query carries binds** — pymysql does not interpolate when `args is None`, and
a doubled `%%` would then reach the server literally and match the wrong rows.
`%s` is left alone because that is the marker, and an already-escaped `%%`
survives a second pass.

Putting it in the dialect rather than in each probe is deliberate: a probe author
writing `LIKE '%unsigned%'` is writing correct SQL, and making that wrong
depending on whether the same query happens to bind a schema name is a rule
nobody will remember.

`Session.fetch` also normalises an empty `{}` or `()` to `None`, so a bind-free
query is sent verbatim.

### 2. `generated` is a reserved word

`AS generated` is a syntax error in MySQL 8 — it broke `objects`, `indexes` and
`constraints`, three of the datasets everything downstream reads. Now backticked.
The other Oracle-shaped aliases (`status`, `temporary`, `position`, `engine`,
`collation`) are non-reserved keywords and MySQL accepts them; the live run is
what established which is which.

### 3. Four grants, and MySQL's failure mode is worse than Oracle's

Measured on the same server, same account, adding one grant at a time:

| grants | routines | routine bodies | triggers | events |
|---|---|---|---|---|
| `SELECT` + `SHOW VIEW` | 0 | 0 | 0 | 0 |
| + `EXECUTE`, `TRIGGER`, `EVENT` | 4 | 0 | 2 | 1 |
| + `SHOW_ROUTINE` | 4 | **4** | 2 | 1 |

**`SHOW_ROUTINE` is a dynamic privilege (8.0.20+), grantable only `ON *.*`.**
Without it the routines are listed and `routine_definition` is NULL — so Phase 4b
would find four routines and no code to convert.

Oracle at least refuses the query when a grant is missing. **MySQL returns fewer
rows, with no error**, so a missing grant is indistinguishable from a small
estate. Anything reporting "0 routines" here is a grant question first.

### 4. `mysql.role_edges` needs a grant the collector should not have

Reading it requires `SELECT` on the `mysql` schema, which also exposes every
password hash in `mysql.user`. The probe now reads
`information_schema.applicable_roles`, which needs no extra grant. It reports
less on a server with many accounts — the honest trade is fewer rows over a wider
grant, and a failed query in the manifest is a signal operators should be able to
trust.

## Phase 2: real AWS SCT on the MySQL path

**Proven 2026-09-29.** SCT 1.0.677 assessed `dbmig_mysql_app` against RDS for
PostgreSQL and produced a real report: **34 occurrences across 10 action items**,
its own PDF and three CSVs, parsed with **zero unmapped columns**.

The action items map onto the seeded defects, which is the point of the estate:

| SCT code | Occurrences | What it found | Seeded defect |
|---|---|---|---|
| `8825` | 11 | Check the default value for a Date/DateTime column | 4 (zero dates) |
| `8795` | 8 | Postgres is case sensitive; check the string comparison | 9 (`_ci` collation) |
| `8844` | 5 | Error codes are not the same | — (routine handlers) |
| `8811` | 2 | Unable to convert functions (`LAST_INSERT_ID`) | — (SQL/PSM) |
| `8706` | 2 | Unable to convert datatypes | 2, and ENUM/SET |
| `9997` | 2 | Unable to resolve objects | — |
| `9994` | 1 | Unable to convert objects (Events) | — (the scheduler) |
| `8829` | 1 | No analog of `ON DUPLICATE KEY UPDATE` | — (`sp_place_order`) |
| `8850` | 1 | Values for some parameters are not supported | — |
| `8859` | 1 | Unsupported transaction command in PL/pgSQL | — |

**Routing is honest about what it does not know.** These are MySQL codes and
`sct/route.py` holds Oracle's, so nine route to `human` and one (`9994`, shared)
maps. An unmapped item carries `route_mapped: False` and the reason *"an unmapped
item treated as automatic is the one failure mode this table exists to prevent"*.
Classifying the MySQL codes is a follow-up; reporting them as unclassified is
correct in the meantime.

### Three things the live run found that no amount of reading would have

**1. MySQL takes no `connectionType`, and passing one sends SCT down Oracle's code path.**

Read from SCT's own bytecode: `MySqlConnectionProperties` declares `serverName`,
`port`, `username`, `password`, `useSSL` — and, unlike
`OracleConnectionProperties`, has **no `$ConnectionType` inner class at all**.
Passing `connectionType: 'BASIC'` produced:

```
No enum constant com.amazon.sct.dbloader.properties.OracleConnectionProperties.ConnectionType.BASIC
AddSource failed with exception.
Not found object(s) for path "Servers.MYSQL"
```

The *visible* error is the last one, and it reads like a tree-path bug rather than
a parameter that should not have been there. MySQL also takes no `database` — the
schema filter scopes the assessment.

**2. SCT demands `SELECT` and `SHOW VIEW` at SERVER scope, not per schema.**

```
The specified account (dbmig_collector) does not have sufficient privileges
for working with the following object(s):
MYSQL Server : [SELECT, SHOW VIEW]
```

The schema-scoped grants the collector needs do **not** satisfy SCT; it wants them
`ON *.*` as well. Again the follow-on error was
`Not found object(s) for path "Servers.MYSQL"`, hiding a privilege problem behind
what looks like a path problem. `01_setup_admin.sql` now grants both scopes and
verifies the server-scope pair explicitly.

That is the third time this project has paid for a privilege check that presents
as something else: `SELECT ANY DICTIONARY` on Oracle, `SHOW_ROUTINE` for routine
bodies, and now this.

**3. AWS SCT's own metadata query is invalid under MySQL 8's default `sql_mode`.**

```
Error executing 'load-partitions-by-schema' query:
Expression #1 of ORDER BY clause is not in SELECT list, references column
'information_schema.PARTITIONS.TABLE_SCHEMA' which is not in SELECT list;
this is incompatible with DISTINCT
```

SCT retries three times, then abandons the assessment with *"Metadata loading was
interrupted because of data fetching issues"*. `ONLY_FULL_GROUP_BY` is in **MySQL
8's default sql_mode**, so **a stock MySQL 8 cannot be assessed by SCT** until it
is relaxed. This is SCT's SQL, not ours — nothing in DBShift can work around it,
and it is a real prerequisite a client's DBA must action on the source server.

`run_mysql.ps1` starts its container without `ONLY_FULL_GROUP_BY` for this reason,
with the measurement in the script.

### The target matrix

`sct/targets.py` became a matrix keyed on source engine. From MySQL:

| Target | Scope | SCT |
|---|---|---|
| RDS for MySQL | in scope | **assessment only** — AWS publishes no MySQL→MySQL conversion path |
| RDS for PostgreSQL | in scope (default) | converts code |
| Aurora MySQL | listed, marked | assessment only |
| Aurora PostgreSQL | listed, marked | converts code |

`rds-oracle` does not exist from a MySQL source and is refused by name. Every row
carries `sct_conversion`, which **Phase 5 must read**: on the homogeneous pair zero
action items is a *complete* result, not evidence still to be collected — the
opposite of the right default everywhere else.

### The cache key

`sct/runner.py`'s key gained the engine, because `rds-postgresql` is a target id
from both sources and two estates sharing a schema name would have shared a
directory. **Oracle's key is deliberately unchanged**: prefixing it would have
orphaned the five real 25-minute assessments already in `sct/output/`. MySQL
carries the prefix and preserves schema-name case, since `Sales` and `SALES` are
different databases.

### The JDBC driver

Per engine, because SCT registers the jar under a vendor-specific settings key
(`oracle_driver_file`, `mysql_driver_file`) and the wrong one fails inside
`AddSource` as a driver error. MySQL's `DBSHIFT_MYSQL_JDBC_JAR`, or Connector/J in
`%LOCALAPPDATA%\dbshift-tools\jdbc\` — matched by glob, because the official jar
is named for its version.

## Phases 4 and 5

**Added 2026-09-29.** Each phase is the Oracle machinery with the source engine
threaded through; no gate was forked or loosened. Where the engine comes from
matters: 4b and 4c read the collector manifest's `source_engine`, 4d stamps it on
every extracted statement, and the SCT command lines take `--source-engine`.

| Phase | MySQL -> RDS for PostgreSQL | MySQL -> RDS for MySQL |
|---|---|---|
| 4 Remediate (SCT) | 10 items, 0 unmapped: 2 target, 2 decision, 6 a person | 0 items -- nothing to convert |
| 4b Stored code | 7/7 READY_FOR_APPROVAL (model-drafted, compiled on PG 16) | not applicable |
| 4c Schema DDL | 19 tables, 84/84 statements compiled | not applicable |
| 4d Application SQL | 23 statements: 6 rule, 11 model, 6 manual; 17 parse | not applicable |
| 5 Gate | PROCEED; 10 items reported as work | PROCEED, "homogeneous, not missing evidence" |

### What does not carry over from Oracle

- **`SELECT ... INTO` must not become `INTO STRICT`.** On Oracle it must, because
  PL/SQL raises NO_DATA_FOUND and PL/pgSQL only does with STRICT. MySQL does the
  opposite: zero rows leave the variable NULL, and `sp_place_order` tests
  `IS NULL` straight after. STRICT would turn that into an unhandled exception.
- **Every primary key is named `PRIMARY`.** A foreign key resolved by constraint
  name finds the first `PRIMARY` in the schema. 4c reads the referenced table from
  `constraint_columns` instead.
- **`DEFAULT_GENERATED` is not a generated column.** It marks `DEFAULT
  CURRENT_TIMESTAMP`. The collector's `virtual_column` matched it and 4c would
  have dropped every such column; fixed in the probe, and 4c reads `extra` itself.
- **Literal defaults are unquoted** in `information_schema` (`GB`, `new`), and
  nullability is YES/NO, not Y/N.
- **SCT's shared codes differ by source.** 9994 is Oracle AQ on DBMIG_APP and the
  EVENT `ev_purge_old_audit` here, so MySQL routes override Oracle's.
- **CDC readiness is binlog, not redo.** The words come from
  `collector/mode.CDC_WORDING`; the console shows Binary log / binlog_format /
  binlog_row_image instead of "Log mode: ARCHIVELOG".

### Type decisions in 4c, made for the DMS load

`int unsigned` -> bigint; `bigint unsigned` -> numeric(20,0), except an
AUTO_INCREMENT key or a foreign key to one (bigint, bounded by the identity);
`tinyint(1)` stays smallint (DMS writes 0/1); ENUM/SET -> text with a CHECK on the
declared members; JSON -> jsonb; DATETIME -> timestamp(n); TIMESTAMP -> timestamptz;
TIME -> interval (838 hours does not fit `time`). AUTO_INCREMENT becomes an
identity that must be restarted past the loaded maximum.

### Open, for Phases 6-10

- 4c renames reserved words (`order` -> `order_tbl`, `group` -> `group_col`); DMS
  lower-cases but does not rename, so the Phase 7 mapping must carry the same names.
- Generated columns are omitted by 4c and must be excluded from the DMS mapping,
  then added after the load.
- `/api/cutover` returns 400 on a MySQL run: it names the target from the 50-rule
  assessment's owning schema, which an SCT-only Phase 2 does not produce.

## The console

**Updated 2026-09-29.** The source engine is a first-class choice, and every
screen downstream reads the `(source, target)` pair rather than a single boolean.

**Connect** leads with a source-engine picker, above the connection form, because
it changes the form: the DSN label and default, and the schema placeholder's
casing (lower case on MySQL, since a schema is a directory on disk). Switching
engines clears the connection and everything built on it — confirmed first, and
the rail resets so a completed Discover cannot look current for a database that is
no longer connected.

`web/preflight_mysql.py` runs the six checks in Oracle's shape and order. The
difference that matters: **Oracle refuses a query when a grant is missing; MySQL
returns fewer rows.** So checks 4 and 5 compare what was found against what the
account should find, and a "Stored code visible" warning names `EXECUTE`,
`TRIGGER`, `EVENT` and `SHOW_ROUTINE` — because "0 routines" is almost always a
grant, not an estate.

**Phase 3's picker offers only the legal pairs.** It used to hardcode
`['ORACLE','POSTGRESQL']`, which offered RDS for Oracle from a MySQL source for
the server to then refuse; better not to offer it. The homogeneous card says
*"Nothing to convert — AWS publishes no MySQL-to-MySQL conversion path."*

**Two MySQL-only Discover panels**, both from datasets the collector already
produced, so the screen cannot show a value the CLI would not:

- **Binlog readiness** — a verdict per setting, because all three must hold and an
  operator needs to know *which* one to take to their DBA.
- **Storage engines and character sets** — MyISAM tables, `utf8mb3` columns and
  case-insensitive collations, each with what it implies for the target.

Both are hidden entirely on Oracle rather than shown empty.

**Phases 4b, 4c and 4d state their own inapplicability** on a homogeneous pair,
with the run buttons *disabled rather than hidden* — a greyed button under a
banner says "this does not apply here"; a missing one sends someone hunting.

**Phase 7 branches on the pair.** Data Pump serves exactly `Oracle → RDS for
Oracle`; both MySQL paths use DMS. Phase 8's cross-engine note shows when the
engines differ, which is not the same question as "is the target PostgreSQL".

### Verified

`drive_srcpick.js` **28/28** in headless Edge, and `check_overlap.js` **17/17 at
both 1440x900 and 390x844** against a populated MySQL run — phone width is new;
the previous baseline was desktop only.

### Three bugs the UI work surfaced in the server

1. **`/api/state` 500'd after switching to MySQL** — `sizing_target.LABEL` had no
   `MYSQL` entry, so resetting the target to the source's first legal one broke the
   label lookup. `sizing/target.py` now carries all three targets.
2. **Console discovery ran Oracle's driver against MySQL** — the `Config` it built
   omitted `source_engine`, so `DPY-6005` appeared *after* a successful preflight,
   which is the worst possible place to learn it.
3. **`preflight_mysql` returned the schema list under the wrong key.** The server
   reads `facts["schemas_selected"]`; the list was top-level. `STATE.schemas` stayed
   empty, the console fell back to Oracle's `DBMIG_APP, DBMIG_RPT,
   DBMIG_COLLECTOR`, and the run **looked successful** — a manifest, no failed
   queries, 748 rows of server settings — while collecting zero tables. The only
   symptom was `v_user_tables` having no columns three phases later.

## Two measurements worth keeping

**InnoDB's row estimate is wrong, and by how much.** `clickstream_raw` holds
exactly 50,000 rows; `information_schema.TABLES.table_rows` reported **49,882**.
That is why `num_rows` carries the estimate (as Oracle's does) and the exact count
comes from `dataprofile.py`, which counts — and why Phase 8 must never compare
`num_rows`.

**`SQL SECURITY DEFINER` is MySQL's default for a view.** All three views in the
seeded estate report `DEFINER`, and all five routines do, though only one of each
is a seeded defect. A rule counting DEFINER objects fires on every clean one too:
the finding is *whose definer will not exist on the target*, not *is DEFINER*.
