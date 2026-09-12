# Checks catalogue — what we read and what we check

The complete reference for both phases: every statement the collector issues
against the source, and every rule the assessment engine evaluates. Read this
when you need to answer "what exactly does this run against my database?" or
"why did it flag that?"

Counts below are from collector run `b6963f2d` (2026-09-09).

---

# Part 1 — Discovery: what the collector reads

**56 fixed statements, plus one scan per profiled table** (10 in this run, so 66
total). The fixed set never varies; only the scan count moves with the estate.

Everything runs as `dbmig_collector` — `SELECT_CATALOG_ROLE` + `CREATE SESSION`,
plus explicit per-table `SELECT` for the data profile. **No DDL, no DML, ever.**
Every statement is recorded in `manifest.json` with its SHA-256, row count and
duration, which is what lets a client DBA audit exactly what touched their
database.

## Orchestration (3 statements)

Run before any probe, to establish scope.

| Statement | Reads | Purpose |
|---|---|---|
| `config.discover_schemas` | `DBA_USERS` | Finds every schema Oracle does **not** maintain itself (`ORACLE_MAINTAINED='N'`). Anything found here but not configured is reported as discovered-but-not-collected, so schema drift is visible rather than silent |
| `config.verify_schemas` | `DBA_USERS` | Confirms the configured schemas actually exist |
| `identity.snapshot` | `DUAL` | Container and database name, stamped into every output file |

## Probe 1 — identity (5 statements)

Establishes what the database *is*. Feeds target sizing and the CDC readiness
rules.

| Statement | Reads | Why it matters |
|---|---|---|
| `identity.version` | `V$VERSION` | Engine version — Data Pump does not import downward, so source and target versions must be compatible |
| `identity.instance` | `V$INSTANCE` | Host, version, uptime |
| `identity.database` | `V$DATABASE` | **`LOG_MODE` and supplemental logging** — these decide whether DMS change data capture is possible at all |
| `identity.context` | `DUAL` | Container name; confirms we are in the PDB and not `CDB$ROOT` |
| `identity.nls` | `NLS_DATABASE_PARAMETERS` | Character set. The RDS instance must be **created** with a matching value; it cannot be changed later |

## Probe 2 — objects (1 statement)

| Statement | Reads | Why it matters |
|---|---|---|
| `objects.census` | `DBA_OBJECTS` | The master inventory — 90 rows. Every later count reconciles against this |

## Probe 3 — tables (3 statements)

| Statement | Reads | Rows | Why it matters |
|---|---|---|---|
| `tables.list` | `DBA_TABLES` | 21 | Row estimates, compression, partitioning flag, IOT type |
| `tables.columns` | `DBA_TAB_COLS` | 182 | Full column structure. **`DBA_TAB_COLS`, not `DBA_TAB_COLUMNS`** — only the former carries the virtual and hidden flags, and a virtual column cannot be inserted into on the target |
| `tables.comments` | `DBA_TAB_COMMENTS` | 1 | Documentation that should survive the move |

## Probe 4 — indexes (3 statements)

| Statement | Reads | Rows | Why it matters |
|---|---|---|---|
| `indexes.list` | `DBA_INDEXES` | 31 | Type, uniqueness, status, clustering factor |
| `indexes.columns` | `DBA_IND_COLUMNS` | 41 | Column order — needed to tell whether a foreign key is genuinely supported |
| `indexes.expressions` | `DBA_IND_EXPRESSIONS` | 2 | Function-based index definitions |

## Probe 5 — constraints (2 statements)

| Statement | Reads | Rows | Why it matters |
|---|---|---|---|
| `constraints.list` | `DBA_CONSTRAINTS` | 51 | PK / FK / unique / check, plus **`VALIDATED`** — an `ENABLE NOVALIDATE` constraint means existing rows were never checked |
| `constraints.columns` | `DBA_CONS_COLUMNS` | 52 | Which columns each constraint covers |

Uses `SEARCH_CONDITION_VC` rather than `SEARCH_CONDITION` to avoid the LONG column.

## Probe 6 — storage (3 statements)

| Statement | Reads | Rows | Why it matters |
|---|---|---|---|
| `storage.segments` | `DBA_SEGMENTS` | 48 | **Real physical bytes.** Target storage is sized from this, never from row counts |
| `storage.lobs` | `DBA_LOBS` | 6 | LOB columns decide DMS LOB mode, which silently truncates in limited mode |
| `storage.tablespaces` | `DBA_TABLESPACES` | 5 | Encryption and compression settings |

## Probe 7 — partitions (3 statements)

| Statement | Reads | Rows | Why it matters |
|---|---|---|---|
| `partitions.tables` | `DBA_PART_TABLES` | 1 | **Partitioning is Enterprise Edition only on RDS** — this drives the licence verdict |
| `partitions.table_partitions` | `DBA_TAB_PARTITIONS` | 4 | Individual partitions and bounds |
| `partitions.key_columns` | `DBA_PART_KEY_COLUMNS` | 2 | Partition keys |

## Probe 8 — plsql (3 statements)

| Statement | Reads | Rows | Why it matters |
|---|---|---|---|
| `plsql.source` | `DBA_SOURCE` | 61 | Every line of stored code, reassembled per object and **SHA-256 hashed** |
| `plsql.errors` | `DBA_ERRORS` | 1 | Stored compilation errors — broken now, not merely stale |
| `plsql.invalid_objects` | `DBA_OBJECTS` | 1 | Objects with `STATUS <> 'VALID'` |

Source is reassembled by joining lines in `LINE` order with no trimming or case
folding. Normalising would make the hash stable across changes that are real.
**The hash is what enables skip-unchanged**, which is what keeps Bedrock costs
viable on a re-run.

## Probe 9 — programmatic (13 statements)

Everything that is neither a table nor an index. These are the objects that
migrate badly and are easiest to forget.

| Statement | Reads | Rows | Why it matters |
|---|---|---|---|
| `programmatic.views` | `DBA_VIEWS` | 3 | Definitions |
| `programmatic.mviews` | `DBA_MVIEWS` | 1 | Must be rebuilt and re-primed on the target |
| `programmatic.mview_logs` | `DBA_MVIEW_LOGS` | 1 | Refresh machinery |
| `programmatic.sequences` | `DBA_SEQUENCES` | 5 | **`LAST_NUMBER` must be carried forward** or keys collide after cutover |
| `programmatic.synonyms` | `DBA_SYNONYMS` | 2 | May point at objects outside migration scope |
| `programmatic.triggers` | `DBA_TRIGGERS` | 1 | Must be disabled during bulk load or they corrupt row counts |
| `programmatic.db_links` | `DBA_DB_LINKS` | 2 | Need recreation plus a network path on the target |
| `programmatic.directories` | `DBA_DIRECTORIES` | 16 | **RDS has no OS filesystem** — every directory needs S3 integration |
| `programmatic.external_tables` | `DBA_EXTERNAL_TABLES` | 1 | Read from server files; they exist but return nothing on RDS |
| `programmatic.scheduler_jobs` | `DBA_SCHEDULER_JOBS` | 1 | Resume on their schedule the moment the target opens |
| `programmatic.queues` | `DBA_QUEUES` | 2 | AQ carries internal state Data Pump does not reproduce |
| `programmatic.types` | `DBA_TYPES` | 3 | **DMS does not replicate user-defined types** |
| `programmatic.xml_schemas` | `DBA_XML_SCHEMAS` | 1 | Must be registered before dependent tables load |

## Probe 10 — security (6 statements)

| Statement | Reads | Rows | Why it matters |
|---|---|---|---|
| `security.users` | `DBA_USERS` | 40 | Accounts, status, authentication type |
| `security.roles` | `DBA_ROLES` | 92 | Role inventory |
| `security.role_privs` | `DBA_ROLE_PRIVS` | 4 | Who holds DBA or other elevated roles |
| `security.sys_privs` | `DBA_SYS_PRIVS` | 14 | `ANY` privileges cross schema boundaries entirely |
| `security.tab_privs` | `DBA_TAB_PRIVS` | 25 | Object grants, including grants to `PUBLIC` |
| `security.profiles` | `DBA_PROFILES` | 72 | Password lifetime and resource limits |

## Probe 11 — features (6 statements)

**This probe carries the commercial story.**

| Statement | Reads | Rows | Why it matters |
|---|---|---|---|
| `features.usage_statistics` | `DBA_FEATURE_USAGE_STATISTICS` | 496 | **Proves which licensed features were actually used**, rather than assuming from what is installed. This is what forces the SE2-vs-EE verdict |
| `features.database_options` | `V$OPTION` | 88 | What is installed |
| `features.parameters` | `V$PARAMETER` | 10 | CPU count, memory targets, `COMPATIBLE` |
| `features.osstat` | `V$OSSTAT` | 16 | CPU count and busy/idle time |
| `features.sysmetric` | `V$SYSMETRIC` | **0** | Empty on XE — see limits below |
| `features.awr_snapshots` | `DBA_HIST_SNAPSHOT` | 1 | AWR coverage, to know whether percentiles are even possible |

## Probe 12 — dataprofile (5 fixed + 1 per profiled table)

The only probe that reads **row data**. Everything else reads metadata.

| Statement | Reads | Purpose |
|---|---|---|
| `dataprofile.tables` | `DBA_TABLES` | Row estimates, to decide scan vs sample |
| `dataprofile.columns` | `DBA_TAB_COLS` | Types and cardinality, to pick what to check |
| `dataprofile.key_columns` | `DBA_CONS_COLUMNS` | Columns already covered by a PK or unique constraint — excluded |
| `dataprofile.external_tables` | `DBA_EXTERNAL_TABLES` | Excluded from scanning |
| `dataprofile.mview_containers` | `DBA_MVIEWS` | Excluded — duplicates in an aggregate are arithmetic |
| `dataprofile.scan.<OWNER>.<TABLE>` | the table itself | **One aggregate per table**, not one per column |

**Metadata decides what is worth asking.** A column is checked for duplicates
only if the optimizer already believes it is ~95% unique *and* nothing enforces
it. Text columns are checked for characters outside ASCII. Both checks are
folded into a single pass per table:

```sql
SELECT COUNT(*),
       COUNT("NATIONAL_ID") - COUNT(DISTINCT "NATIONAL_ID"),
       SUM(CASE WHEN INSTR(ASCIISTR("FULL_NAME"), '\') > 0 THEN 1 ELSE 0 END)
FROM   "DBMIG_APP"."CUSTOMER";
```

**Counts come back, never rows.** Above `DBSHIFT_PROFILE_MAX_ROWS` (2M) the
table is block-sampled to `DBSHIFT_PROFILE_SAMPLE_ROWS` (1M) instead of scanned,
so cost is bounded by the target rather than table size.

Skipped by design: Oracle internals (`DR$`, `AQ$`, `MLOG$`, `SYS_IOT`),
materialized-view containers, external tables, and any owner where `ORA-00942`
proves there is no data access.

---

# Part 2 — Assessment: the 51 rules

Each rule is **a row in a table**, not code: an id, a category, a severity, a
remediation level, a SQL predicate and a rationale. The engine runs the
predicate; every returned row becomes a finding carrying that rule's severity.
**Nothing is judged at runtime**, which is why the same estate always scores
identically and why a model cannot quietly change a verdict.

Adding rule 52 is inserting a row in `assess/rules.json`.

### Severity and what it costs

| Severity | Penalty | Meaning |
|---|---|---|
| CRITICAL | 25 | Blocks migration. **Caps the overall score at 60** |
| HIGH | 10 | Will cause failure or data loss if unaddressed |
| MEDIUM | 4 | Needs a decision before cutover |
| LOW | 1 | Worth knowing, not blocking |
| INFO | 0 | Context, or a stated limit of the assessment |

### Remediation level — who may fix it

| Level | Meaning |
|---|---|
| L1 | Safe auto-fix, applied without a prompt |
| L2 | Auto-generated fix, **requires human approval** |
| L3 | A human authors the fix |
| L4 | Never auto-fixed |

The level is a property of the rule. A model returning 99% confidence on a
business-logic rewrite still lands in L3.

## RDS compatibility — 16 rules

Will this object work on Amazon RDS for Oracle at all?

| Rule | Sev | Lvl | Checks | Hit |
|---|---|---|---|---|
| RDS-001 | HIGH | L3 | Object name is a reserved word | 1 |
| RDS-002 | HIGH | L3 | Column name is a reserved word | 1 |
| RDS-003 | HIGH | L3 | Directory object needs S3 integration — RDS has no filesystem | 4 |
| RDS-004 | **CRITICAL** | L3 | External table cannot migrate — reads a server file that will not exist | 1 |
| RDS-005 | MEDIUM | L3 | Database link needs recreation and a network path | 1 |
| RDS-006 | LOW | L2 | Scheduler job resumes on its own schedule after cutover | 1 |
| RDS-007 | MEDIUM | L3 | AQ queues carry state Data Pump does not reproduce | 2 |
| RDS-008 | MEDIUM | L3 | Oracle Text index needs the option group configured | 1 |
| RDS-009 | MEDIUM | L3 | XML schema must be registered before dependent tables load | 1 |
| RDS-010 | MEDIUM | L3 | User-defined type — **DMS does not replicate these** | 3 |
| RDS-011 | LOW | L2 | Materialized view needs a refresh strategy on the target | 1 |
| RDS-012 | MEDIUM | L2 | LOB column — DMS LOB mode must be set explicitly or it truncates | 6 |
| RDS-013 | HIGH | **L4** | Partitioned table **requires Enterprise Edition** | 1 |
| RDS-014 | LOW | L2 | Synonym resolves to something outside migration scope | 0 |
| RDS-015 | INFO | L2 | Source character set — the target must be created to match | 1 |
| RDS-016 | HIGH | L2 | **Oracle Text index holds no documents** — searches return nothing, silently | 1 |

`RDS-016` was written on 2026-09-12 after Phase 8 found
`DBMIG_APP.IX_COMM_NOTES_TEXT` indexing **0 of 400,000 rows** while reporting
`INDEXED` and `VALID` with nothing pending sync: it was created before the data
was generated and never synced. It needs one extra grant,
`scripts/oracle-source/07_grant_collector_text.sql`, because the proof
(`CTXSYS.CTX_INDEXES.IDX_DOCID_COUNT`) is not readable by the collector
otherwise — `dba_tables.num_rows` for the `DR$` token tables is `NULL`, and
firing on a missing statistic would flag every estate whose internals are simply
unanalysed. Without the grant the dataset is empty and the rule does not fire.

## Data quality — 11 rules

Is the data itself fit to move?

| Rule | Sev | Lvl | Checks | Hit |
|---|---|---|---|---|
| DQ-001 | **CRITICAL** | L2 | No primary key — **DMS CDC cannot reliably replicate updates or deletes** | 2 |
| DQ-002 | MEDIUM | L3 | Near-unique column carries duplicates — an unenforced natural key | 1 |
| DQ-003 | LOW | L3 | Text column contains non-ASCII characters | 0 |
| DQ-004 | HIGH | L3 | Constraint is `ENABLE NOVALIDATE` — existing rows were never checked | 3 |
| DQ-005 | MEDIUM | L2 | Constraint is disabled and enforcing nothing | 0 |
| DQ-006 | MEDIUM | L2 | `NUMBER` with no precision or scale — truncation risk on any fixed-width target | 1 |
| DQ-007 | MEDIUM | L1 | Optimizer statistics stale or missing — sizing reads these | 3 |
| DQ-008 | LOW | L2 | Column is entirely NULL — dead weight, or a load that never ran | 1 |
| DQ-009 | LOW | L2 | `VARCHAR2` with BYTE semantics — may overflow on a multibyte target | 11 |
| DQ-010 | INFO | L2 | Table could not be profiled — **absence of findings is not absence of problems** | 2 |
| DQ-011 | INFO | L2 | Table profiled by sample only — a clean result is not proof of absence | 1 |

`DQ-010` and `DQ-011` exist so the report states its own blind spots rather than
implying coverage it does not have.

## Performance — 8 rules

Will it perform, and will the migration window hold?

| Rule | Sev | Lvl | Checks | Hit |
|---|---|---|---|---|
| PERF-001 | MEDIUM | L1 | **Unindexed foreign key** — forces a full scan and share lock on parent delete | 1 |
| PERF-002 | LOW | L2 | Table has no indexes at all | 2 |
| PERF-003 | LOW | L2 | Six or more indexes on one table — each rebuilt after load | 0 |
| PERF-004 | MEDIUM | L1 | Index unusable or invisible — migrates as dead weight | 0 |
| PERF-005 | MEDIUM | L2 | Significant row chaining — extra I/O on every read | 0 |
| PERF-006 | LOW | L2 | Large table unpartitioned — loads as a single serial task | 1 |
| PERF-007 | LOW | L2 | Poor clustering factor — range scans approach one block per row | 4 |
| PERF-008 | LOW | L2 | Redundant index sharing a leading column | 0 |

## Security — 8 rules

| Rule | Sev | Lvl | Checks | Hit |
|---|---|---|---|---|
| SEC-001 | HIGH | L3 | Invalid object — will not compile on the target either | 1 |
| SEC-002 | HIGH | L3 | Privilege granted to `PUBLIC` — every current and future user | 0 |
| SEC-003 | HIGH | L3 | `ANY` system privilege — crosses schema boundaries | 0 |
| SEC-004 | MEDIUM | L3 | Application account holds DBA or elevated role | 0 |
| SEC-005 | MEDIUM | L3 | Unlimited tablespace — a cost exposure on RDS as well as a control gap | 0 |
| SEC-006 | MEDIUM | L3 | Password never expires | 1 |
| SEC-007 | MEDIUM | L3 | Column name suggests sensitive data, no encryption detected | 1 |
| SEC-008 | LOW | L2 | Account locked or expired but still migrating with its grants | 0 |

## Operational risk — 8 rules

| Rule | Sev | Lvl | Checks | Hit |
|---|---|---|---|---|
| OPS-001 | **CRITICAL** | L3 | **Not in ARCHIVELOG** — no redo, therefore no DMS CDC, therefore no low-downtime cutover | 1 |
| OPS-002 | **CRITICAL** | L3 | **Supplemental logging off** — CDC starts and silently applies incomplete changes | 1 |
| OPS-003 | HIGH | **L4** | Edition-forcing feature in use — unavailable on SE2 at any price | 2 |
| OPS-004 | MEDIUM | L3 | PL/SQL object has stored compilation errors | 1 |
| OPS-005 | MEDIUM | L3 | Scheduler job already failing on the source | 0 |
| OPS-006 | INFO | L2 | Utilization data unavailable — sizing cannot claim measured headroom | 1 |
| OPS-007 | LOW | L2 | Object depends on a database link — breaks *after* cutover, not during | 0 |
| OPS-008 | INFO | L2 | Total estate size — sets the floor on load time and target storage | 1 |

Rules with a hit count of 0 are not dead. They found nothing **on this estate**,
which is itself a result; several will fire immediately on a real client system.

---

# How scoring works

Each category starts at 100. Penalty accrues **per rule, not per finding**,
scaled logarithmically by how many objects the rule hit — a rule firing on 340
tables is one issue with a wide blast radius, not 340 issues. The score then
decays exponentially:

```
score = 100 × e^(−penalty / 60)
```

Linear subtraction was tried first and abandoned: past ~100 penalty every busy
category saturated at 0, making a bad category indistinguishable from a
catastrophic one.

Overall is the mean of the five, and **any critical finding caps it at 60** — a
migration is gated by its worst unresolved problem, not by its average.

# How this is proven

The source carries 8 deliberately seeded defects with a documented expected
severity (`04-defects.md`). Every run scores findings against that answer key:
**7 of 7 detectable found, severity exact on all 7.** The eighth was never
successfully seeded — the seed script is broken, and that is recorded rather
than counted as a miss.

Findings outside the seeded set are reported as **additional findings, not false
positives.** Most describe the estate accurately. A real false-positive rate
needs human triage and is not claimed.

# Known limits

- **No utilization percentiles.** `V$SYSMETRIC` is empty on XE and AWR is not
  licensed here, so sizing rests on capacity and feature usage alone
- **Sampling cannot find rare events.** Two duplicate values in three billion
  rows will not surface in a 0.03% sample. `DQ-011` marks every sampled table
- **Semantic rules are heuristics.** `DQ-002`'s near-unique test already
  misfired twice on money columns before being narrowed to text and whole
  numbers. At a client these belong as *hypotheses to confirm*, not findings
- **Structural rules port unchanged.** No primary key is no primary key
  anywhere; those are safe on any estate

# Adding a rule

Append to `assess/rules.json`:

```json
{
  "rule_id": "RDS-016",
  "category": "rds_compatibility",
  "severity": "MEDIUM",
  "remediation_level": "L3",
  "title": "Short statement of the problem",
  "rationale": "Why this matters for the migration, in one or two sentences.",
  "sql": "SELECT owner, object_name, object_type, '...' AS detail FROM v_user_objects WHERE ..."
}
```

The predicate must return `owner`, `object_name`, `object_type` and `detail`.
Query the `v_user_*` views rather than the raw tables — they exclude Oracle
internals centrally, and skipping them is how you get 340 false positives on
`DR$` objects.

Valid categories: `rds_compatibility`, `data_quality`, `performance`,
`security`, `operational_risk`. The engine validates every field at load and
refuses to run on a malformed catalogue.

**Then re-run the answer key.** A rule change that breaks detection should be
caught here, not at a client site.
