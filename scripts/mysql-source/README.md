# The MySQL source estate

Run in order. Every script ends with a proof block, and **`verify_defects.py` is
the gate** — do not treat a run as done until it reports 12/12.

```bash
mysql -u root -p                 < 00_reset.sql          # drops + recreates the 3 schemas
mysql -u root -p                 < 01_setup_admin.sql    # accounts and grants
mysql -u dbmig_app -p dbmig_mysql_app < 02_schema_objects.sql
mysql -u dbmig_app -p dbmig_mysql_app < 03_generate_data.sql   # ~10s
mysql -u dbmig_app -p dbmig_mysql_app < 04_seed_defects.sql
python scripts/mysql-source/verify_defects.py            # must print 12/12
```

Replace the `CHANGE_ME_*` placeholders in `01_setup_admin.sql` before running it,
or pipe it through `sed`. Passwords belong in `credentials.local.ps1`, never here.

## What gets built

`dbmig_mysql_app` — a retail order estate, deliberately a different domain from
the Oracle estates' lending and telecom so it exercises different shapes.

| | |
|---|---|
| Tables | 9 clean + 8 carrying defects |
| Views | 3 (2 clean, 1 defect) |
| Routines | 5 (4 clean, 1 defect) |
| Triggers | 2 |
| Events | 1 |
| Partitions | 5 RANGE partitions, 4 populated |
| Rows | ~205,000 exact |

`dbmig_mysql_rpt` holds `revenue_snapshot`, so cross-schema references exist —
and on MySQL a schema *is* a database, which makes that a genuinely different
thing from Oracle's cross-schema reference.

`dbmig_rehearsal` is the writable copy Phase 4 applies fixes to.

## The twelve defects

Paired one-to-one with `answer_key.json`. Six have no Oracle analogue at all,
which is the point of a second source engine.

| # | Defect | Severity | MySQL-only |
|---|---|---|---|
| 1 | MyISAM table — no transactions, DMS CDC cannot track it | CRITICAL | ✅ |
| 2 | `utf8mb3` columns — cannot hold 4-byte characters | HIGH | ✅ |
| 3 | No primary key, 50,000 rows | CRITICAL | |
| 4 | Zero dates (`0000-00-00`) — no PostgreSQL representation | HIGH | ✅ |
| 5 | `BIGINT UNSIGNED` above PostgreSQL's signed bigint | CRITICAL | ✅ |
| 6 | Definer-rights routine — RDS grants no SUPER | HIGH | ✅ |
| 7 | Definer-rights view over PII | MEDIUM | ✅ |
| 8 | Reserved-word identifiers (`order`, `key`, `group`) | HIGH | |
| 9 | Duplicate should-be-unique values, one case-only | HIGH | |
| 10 | Orphaned reference, no FK to catch it | HIGH | |
| 11 | Redundant index (prefix of another) | MEDIUM | |
| 12 | Clear-text bank account and national ID | HIGH | |

## Things measured here that are worth knowing

**Four grants the collector cannot do without**, and MySQL's failure mode is
worse than Oracle's — it returns *fewer rows*, not an error:

| grants | routines | routine bodies | triggers | events |
|---|---|---|---|---|
| `SELECT` + `SHOW VIEW` | 0 | 0 | 0 | 0 |
| + `EXECUTE`, `TRIGGER`, `EVENT` | 4 | 0 | 2 | 1 |
| + `SHOW_ROUTINE` | 4 | **4** | 2 | 1 |

`SHOW_ROUTINE` is a *dynamic* privilege (MySQL 8.0.20+), grantable only `ON *.*`.
Without it Phase 4b finds routines and no code to convert. This is the MySQL
analogue of the `SELECT ANY DICTIONARY` lesson, and it is worse because nothing
errors.

**InnoDB's row estimate is wrong, measurably.** `clickstream_raw` holds exactly
50,000 rows and `information_schema.TABLES.table_rows` reported **49,882**. That
is why the exact count comes from `dataprofile.py`, which counts, and why Phase 8
must never compare `num_rows`.

**`SQL SECURITY DEFINER` is MySQL's default for a view.** All three views in this
estate report `DEFINER`, only one of which is the seeded defect. A rule that counts
DEFINER views fires on every clean view too — the finding is *whose definer will
not exist on the target*.

**Zero dates cannot be compared with a literal.** Under the default `sql_mode`
(`NO_ZERO_DATE`), `WHERE signed_on = '0000-00-00'` is itself rejected with
ERROR 1525 even when the stored rows are fine. Cast to `CHAR` and compare text.
The first version of defect 4's proof query failed for exactly this reason and
looked precisely like a seeding failure.

**Every script is re-runnable.** 02 and 04 both drop before creating, because a
partially-applied seeder is a real state — 04 hit it during development when a
foreign key rejected a defect seeded before `03` had created any customers.

## Why the verify script is separate from the proof blocks

`04_seed_defects.sql` ends with its own proof block, and `verify_defects.py`
checks the same twelve **independently** — reading the catalogue or counting the
rows a rule would count, rather than re-running the seeder's own queries. Two
runs of identical SQL prove only that SQL is deterministic.

The reason is on the record: the Oracle estate's defect 7 was seeded by a script
that reported a row modified and changed nothing, capping achievable recall at
7/8 for weeks while the assessment looked like it was under-detecting. See
`docs/04-defects.md`.

The gate is tested in both directions. Converting the MyISAM table to InnoDB makes
it report `11/12` and exit 1; converting it back returns 12/12.
