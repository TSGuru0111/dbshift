# MySQL path — problems found, and what now stops each one

> **Written 2026-09-30**, after taking MySQL through Phases 1–8 on the EC2 estate
> `DBMIG_MYSQL_APP`: locally against stand-ins first, then for real
> (MySQL → Amazon RDS for PostgreSQL 16.15, AWS DMS 3.5.4, 196,544 rows).
> Every entry was found by running something, not by reading code. Each says what
> was seen, why it happened, what changed, and what now catches it if it comes back.
> Earlier Phase 1–2 findings (grants, `sql_mode` for SCT, SCT's `connectionType`)
> are in `docs/19-mysql-source.md`.

## How to read this

- **Seen** is the symptom as it presented — often misleading, which is why it is here.
- **Guard** is the self-test, preflight check or harness that fails if it recurs.
- Problems marked **(both engines)** also affected the Oracle path.

---

## Phase 1 — Discover

| # | Seen | Cause | Fix | Guard |
|---|---|---|---|---|
| 1 | Phase 4c dropped every `DEFAULT CURRENT_TIMESTAMP` column as "computed" | the probe marked `virtual_column` with `extra LIKE '%GENERATED%'`, which also matches `DEFAULT_GENERATED` | match `VIRTUAL GENERATED` / `STORED GENERATED` only; collect `generation_expression` | `collector.selftest_probes_mysql` |
| 2 | `supplemental_log_data_all` could read YES with the binlog off | the string `'NO'` is truthy in Python | compare to `"YES"` | code review; `collector.selftest_mode` |
| 3 | A load that could not finish was only discoverable mid-load | zero dates are a property of the data, invisible to the catalogue | the data profile counts zero dates per date column into `mysql_zero_dates` (every row, as text) | real run: 2 found in `contract_term`, matching the seeded answer key |

## Phase 3 — Size

| # | Seen | Cause | Fix | Guard |
|---|---|---|---|---|
| 4 | `sizing.run` crashed with `KeyError: 'ORACLE'` after writing a correct record | the printout looped over Oracle's path names | loop over the paths legal from this source | CLI run |

## Phase 4 — Remediate (AWS SCT)

| # | Seen | Cause | Fix | Guard |
|---|---|---|---|---|
| 5 | 9 of 10 MySQL action items routed to "a person" with a generic reason | SCT codes are per source vendor; only Oracle's were mapped | `MYSQL_ROUTES` in `sct/route.py`, from the real run | `remediate.selftest_sct_mysql` |
| 6 | Code 9994 described Oracle Advanced Queuing on a MySQL estate | shared 999x codes mean different things per source (here: a MySQL EVENT) | per-source routes override the shared table | same |
| 7 | A MySQL source fix could have been gated by Oracle's allow-list | the source-side gates are Oracle's | a non-Oracle source fix is a person's, never drafted | same |
| 8 | A harmless `COMMENT ON ... 'ON UPDATE CURRENT_TIMESTAMP ...'` rejected as "rewrites data"; every `ON UPDATE CASCADE` FK would have been too | the UPDATE rule scanned comment text and FK referential actions | exempt exactly those two shapes; also now catches `UPDATE "quoted"`, which slipped through before **(both engines)** | `remediate.selftest_sct_mysql` |
| 9 | A correct target fix REJECTED: `schema "dbmig_mysql_app" does not exist` | the target had no schema yet — Phase 4c had not been applied | SQLSTATE 3F000 is BLOCKED (unproven), not REJECTED; 42P01 still rejects a wrong table **(both engines)** | same |
| 10 | The Oracle remediation run planned the MySQL SCT report | "newest record on disk" ignored the source engine | `--source-engine` filter; records carry `source_engine` | CLI |
| 11 | Every live model draft came back "nothing drafted" | the bare `python` has no boto3; the project venv does | run phases with `.venv/Scripts/python.exe` | memory note |

## Phase 4b — Stored code

| # | Seen | Cause | Fix | Guard |
|---|---|---|---|---|
| 12 | The Oracle rule "SELECT INTO must become INTO STRICT" would have broken MySQL code | MySQL leaves the variable NULL on zero rows and `sp_place_order` tests `IS NULL`; STRICT raises instead | the MySQL catalogue and prompt say the opposite | `convert.selftest_mysql`; live 7/7 |
| 13 | A correct conversion REJECTED by parity (`FOR a, b IN SELECT ...`) | the catalogue's marker allowed one loop variable | marker accepts a target list | same |

## Phase 4c — Schema DDL

| # | Seen | Cause | Fix | Guard |
|---|---|---|---|---|
| 14 | Foreign keys would have pointed at the wrong parent table | every MySQL primary key is named `PRIMARY`; the parent was resolved by constraint name | parent from `referenced_table_name`; PKs named `<table>_pkey` | `convert.selftest_mysql` |
| 15 | Every column would have been nullable | MySQL says `YES/NO`, the code read Oracle's `Y/N` | read both | same |
| 16 | `DEFAULT GB` would not parse | MySQL reports literal defaults unquoted | quote literals; map `CURRENT_TIMESTAMP` expressions | same |
| 17 | Table `order` silently became `order_col` | the column-renaming rule was applied to a table name, with no note | tables take `_tbl`, with a note | same |
| 18 | The compile proof failed at `CREATE SEQUENCE schema.table.column` | MySQL's AUTO_INCREMENT counters were scaffolded as Oracle sequences | not on MySQL (identities cover them) — in the CLI and the console | real compile 87/87 |
| 19 | **Real DMS load:** table `order` arrived as `(1, NULL, NULL, NULL)` and failed its NOT NULL | a DMS column-rename rule delivered the renamed columns' values as NULL (proven by a one-table test without the rule: values intact) | keyword columns are created under their source names, quoted, and renamed after the load (`renames` stage, before the keys) | real load 19/19 |

## Phase 5 — Gate

| # | Seen | Cause | Fix | Guard |
|---|---|---|---|---|
| 20 | A MySQL source was told to run `ALTER DATABASE ARCHIVELOG` | binlog facts were mapped onto Oracle's vocabulary, and so was the advice | `collector/mode.CDC_WORDING` per engine | `remediate.selftest_sct_mysql` |
| 21 | "0 action items" on MySQL → RDS MySQL read as "not looked at" | nothing said a homogeneous pair has nothing to convert | the gate says so, driven by the target's `sct_conversion: false` | same |

## Phase 6 — Provision

| # | Seen | Cause | Fix | Guard |
|---|---|---|---|---|
| 22 | Rendering MySQL 8.0 would have billed RDS Extended Support | 8.0 left RDS standard support on 2026-07-31 | the major is chosen from RDS's lifecycle API: 8.4 (supported to 2029), reported as an upgrade | `provision.selftest_mysql` |
| 23 | The stack would have rolled back on create | `SHOW VARIABLES` says `ON`; the RDS parameter group wants `1` for some booleans | normalise, and validate every parameter against RDS's live list in preflight | same; preflight `parameter_group` |
| 24 | The target would have accepted data the source refuses | RDS 8.4's default `sql_mode` is only `NO_ENGINE_SUBSTITUTION` | carry the source's `sql_mode`, collation, event scheduler; force `log_bin_trust_function_creators` (ERROR 1419) and `local_infile` (DMS) | same |
| 25 | Phase 6 claimed PostgreSQL artefacts were "prepared" for an RDS for MySQL target | the check did not know the target | not applicable on a MySQL target, with where the schema comes from | same |
| 26 | A PostgreSQL target was rendered on a gate judged for RDS for MySQL | consistency compared collector runs only | records judged for another target are refused **(both engines)** | same; `dms.selftest_mysql` |
| 27 | `/api/cutover` returned 400 on a MySQL run; the estate could not be named | a homogeneous SCT result has zero findings to take the schema from | fall back to the discovery manifest's single configured schema | same |

## Phase 7 — Migrate (DMS)

| # | Seen | Cause | Fix | Guard |
|---|---|---|---|---|
| 28 | `ERROR 1227` creating views, routines, triggers, events on RDS | `DEFINER=root@localhost` needs SUPER/SET_USER_ID, which RDS never grants | the schema copy removes DEFINER (SQL SECURITY kept) | stand-in with RDS-like grants; `dms.selftest_mysql` |
| 29 | Triggers would re-fire and duplicate audit rows during the load; the purge event would run mid-migration | a plain dump creates them before the data | triggers after the load; events created DISABLED, enabled at cutover | same |
| 30 | DMS would reject `require` on a MySQL endpoint | MySQL endpoints support none / verify-ca / verify-full only (AWS SSL table) | `MYSQL_SSL_MODE = none` inside the VPC | `dms.selftest_mysql` |
| 31 | **Real run:** target endpoint test "ODBC timeout expired" | the CLI never passed the stack's DMS security group, so DMS made its own, which RDS does not trust | CLI passes it; a reused instance joins the group and waits until it is *active* | real run |
| 32 | **Real run:** `KeyError` creating the task | `execute()` passed the caller's `None` migration type on instead of the plan's resolved one | use the plan's | real run |
| 33 | **Real run:** "table `order` does not exist on the target" (false) | the check looked for the source name, not 4c's `order_tbl` | the check applies the same renames DMS will | real run |
| 34 | A load that cannot finish would start | zero dates in NOT NULL columns have no PostgreSQL value | preflight FAILs; a recorded, named decision (nullable on target) downgrades it to WARN and Phase 8 reports the loss | `dms.selftest_mysql`; real run |
| 35 | Residue planned `ALTER SEQUENCE schema.table.column` | MySQL's "sequences" are AUTO_INCREMENT counters | identity `RESTART WITH` on PostgreSQL, `AUTO_INCREMENT =` on MySQL | same; Phase 8 level 5 |
| 36 | A rewritten view reading `line_total` would fail to create | generated columns are added back only after the load | the residue item says to create it after them (`create_after`) | same |
| 37 | **Real run:** DMS execute refused "records_consistent" after passing | a browser harness driving the console rewrote the shared records mid-run | the refusal was correct; lesson: keep the console idle during an AWS run | memory note |
| 38 | `killswitch --destroy` would have terminated both EC2 **source** hosts | every "ours" EC2 instance was terminated | source hosts are stopped, not terminated, unless `--include-source-hosts`; `--only <text>` scopes a teardown to one run | `killswitch.selftest` |
| 39 | Console: DMS source hard-coded to Oracle; needed an env var for the private IP; residue never applied; no way to record the zero-date decision; no schema copy for MySQL → MySQL | the console predated a MySQL source | server endpoints added (2026-09-30); screens being updated | `drive_mysql_1to8.js` |

## Phase 8 — Validate

| # | Seen | Cause | Fix | Guard |
|---|---|---|---|---|
| 40 | Oracle's "'' equals NULL" would have hidden real loss | an Oracle-only behaviour | never applied on a MySQL source; listed as *not* expected | `validate.selftest_mysql` (independent reference) |
| 41 | `DATE_FORMAT('%Y')` failed as a bad format string | pymysql interpolates `%` whenever any argument is passed, even an empty one | no argument when there are no binds | same |
| 42 | Identities read as "at 1" after a correct restart | `pg_sequence_last_value` is NULL until the sequence is next called | read `last_value` and `is_called` from the sequence itself | real run |
| 43 | AUTO_INCREMENT counters could read stale | `information_schema` caches table statistics | `information_schema_stats_expiry = 0` in the validation session | stand-in run |

## The console (found by `scripts/console-test/drive_mysql_1to8.js`)

| # | Seen | Cause | Fix | Guard |
|---|---|---|---|---|
| 46 | Recording the zero-date decision from the UI returned 500; so did reading it | both new endpoints named the records module `provision_records`; the server imports it as `prov_records` | corrected | the harness fails on any 500 |
| 47 | The Migrate screen showed a server error after a teardown | `dms.run.status` raised when the recorded task had been deleted | a deleted task is "no task" (409) | same |
| 48 | Discover's summary said "PL/SQL objects" on a MySQL run | label fixed to Oracle | "Stored routines" on MySQL | the harness fails on Oracle-only wording |
| 49 | The migration-mode detail on Connect said "no redo configuration" and "Requires ARCHIVELOG" on MySQL | `collector/mode.DETAIL` was Oracle's only | `DETAIL_MYSQL`, chosen by source engine | same |
| 50 | Phases 6–8 had no way to act on a MySQL run: no zero-date decision, no schema copy, residue never applied, password fields demanded that the console already held | the screens predated a MySQL source | panels added for all three; passwords taken from the session/SSM | 103/103 (MySQL → PostgreSQL) and 112/112 (MySQL → MySQL) read-only, strict, both widths; `check_overlap` 17/17 at 1440×900 and 390×844 |
| 51 | **Real UI run:** after "Apply ready residue" the result line went blank, so the screen never said what was applied (the harness timed out waiting for it) | the handler's `finally` called `gateResidue()`, which cleared `#residueMeta` whenever the target was live -- wiping "17 applied · 0 failed" a moment after writing it. The apply itself succeeded (`dms/output/residue_apply.json`: 17 applied, 0 failed) | `gateResidue()` clears only its own "needs a deployed target" message | `drive_mysql_1to8.js` now asserts the line survives and that no item is marked failed; proven in the browser with a stubbed apply |
| 52 | **Hands-on UI run:** a second press of "Apply post-load keys and indexes" showed *Applied nothing; the transaction rolled back. column "group" does not exist* -- reading as a broken schema | the first press had worked (24 keys, 10 foreign keys, 10 checks, 45 indexes; `group` already renamed to `group_col` with its data). Post-load had no equivalent of pre-load's "tables already exist" refusal, so the repeat re-ran the renames first; and its failure record replaced the success record | `ddl_apply.apply` refuses a post-load pass whose keys or indexes are already on the target, before opening a write connection, and says so ("already been applied ... Continue with Phase 7's residue and Phase 8"); a refusal writes no record | `convert.selftest_ddl_apply` 56/56 (full and partial repeats); proven read-only against the real target |
| 53 | **Hands-on UI run:** after a finished DMS load and residue, "Phase 8 · Validate" stayed greyed out -- Migrate never opened the next phase | a completed Data Pump import unlocked Validate; a completed DMS load only marked Migrate done (the live run) or set `MIGRATED` (the status check), leaving Validate `idle`, and the status check ran only after pressing Plan, so a reload never saw the finished task. The harness reached Validate with `show('validate')`, which skips the lock | `dmsLoadDone()` unlocks Validate from both DMS paths; the page asks for the DMS task whenever it finds a live target, including on reload | `drive_mysql_1to8.js --execute` reloads and requires Migrate's own button to open Validate; checked on the live console (the button enables after a reload) |

## Tooling (not the product, but it cost time)

| # | Seen | Cause | Fix |
|---|---|---|---|
| 44 | `ParameterNotFound` for an SSM name that exists | Git Bash rewrites a leading `/dbshift/...` into a Windows path; `MSYS_NO_PATHCONV=1` then breaks the `aws` launcher | read SSM secrets with boto3 in-process |
| 45 | The recorded credential expiry lied | the console stamps paste-time + 4 h; the portal block carries no expiry | trust `sts get-caller-identity`, not the stamp |

## The result these fixes produced

MySQL → RDS for PostgreSQL, 2026-09-30: 19/19 tables loaded (196,544 rows, 0 errors);
68 post-load statements applied; 17 identities restarted; Phase 8 — 19/19 row counts
exact, 18/19 tables identical on a checksum of every row, and `contract_term` reported
as the decided zero-date loss. All AWS resources for the run removed afterwards; the
source hosts untouched.

**The same flow driven entirely through the console**, 2026-09-30
(`drive_mysql_1to8.js --execute --strict`, MySQL → RDS for PostgreSQL): Connect,
Discover, Assess, Target, Remediate, 4b–4d, Gate, Provision + Deploy, 4c pre-load,
DMS full load, 4c post-load, residue, Validate -- 107 of 108 checks passed. The one
failure was #51 (a blank result line; the residue had applied 17/0). Phase 8 matched
the CLI run: 19/19 counts, 18/19 tables identical, `contract_term` shown as the
decided loss, 17 identities at or past the source. Resources removed afterwards with
`killswitch --only dbmig-mysql-app`; source hosts untouched.

**Re-run after fixing #51**, 2026-09-30 (same harness, fresh deploy): **110 of 110
checks passed**, including the new ones -- the residue result line stays visible and
no item is marked failed (17 applied, 0 failed). Phase 8 (run `d72ede6d`): 19/19 row
counts exact, 18/19 tables identical on every row, `contract_term` shown as the
decided zero-date loss, 17 identities at or past the source. Resources removed with
`killswitch --only dbmig-mysql-app`; source hosts untouched.
