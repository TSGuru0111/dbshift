# Next tasks

## Immediate: the discovery collector

Build a Python program that connects to the source estate and inventories it.

**Requirements**

- Runs **locally**, not in AWS. It reads the database and pushes results
  outbound over HTTPS. Nothing in AWS connects inbound to the source machine —
  a laptop behind a router has no static address and accepts no inbound
  connections. This is also how real enterprise discovery tools work, because
  security teams refuse inbound firewall holes.
- Connects as **`dbmig_collector`** (read-only, `SELECT_CATALOG_ROLE`), never as
  `dbmig_app`. A client DBA's first question is what the tool can modify; the
  answer must be "nothing".
- Uses `oracledb` in **thin mode** — no Oracle Instant Client dependency, which
  saves ~800 MB of image size and a licensing conversation.
- Credentials from environment variable or OS keychain. **Never** from a config
  file in git.
- Every run generates one `collector_run_id` (UUID) stamped on every row.
  **Append-only** — never update a discovered fact in place. "What changed since
  last month" then becomes a SQL query rather than an argument.
- Logs the SQL it ran and row counts returned. Never logs data or credentials.

**What to read**

- `DBA_OBJECTS` / `USER_OBJECTS` — the object census
- `DBA_TABLES`, `DBA_TAB_COLUMNS`, `DBA_INDEXES`, `DBA_IND_COLUMNS`,
  `DBA_CONSTRAINTS`, `DBA_CONS_COLUMNS` — structure down to column precision,
  nullability, defaults
- `DBA_SEGMENTS` — **real physical sizes**. Size the target from this, not from
  row counts.
- `DBA_PART_TABLES`, `DBA_TAB_PARTITIONS` — partitioning
- `DBA_SOURCE` — actual PL/SQL text of every procedure, function, package,
  trigger. Store each object's text plus a **SHA-256 hash** of it. The hash is
  what enables the skip-unchanged optimization that keeps Bedrock costs viable.
- `DBA_VIEWS`, `DBA_MVIEWS`, `DBA_SEQUENCES`, `DBA_SYNONYMS`, `DBA_DB_LINKS`,
  `DBA_DIRECTORIES`, `DBA_EXTERNAL_TABLES`, `DBA_SCHEDULER_JOBS`
- `DBA_USERS`, `DBA_ROLES`, `DBA_TAB_PRIVS`, `DBA_SYS_PRIVS`
- `V$VERSION` — engine version
- **`DBA_FEATURE_USAGE_STATISTICS`** — proves which licensed features have
  actually been used rather than assuming. This is what drives the SE2 vs EE
  verdict, so it is not optional.

Filter by the four schema owners and exclude Oracle's internal schemas, or you
collect 60,000 system objects and drown the real signal.

**Expected output against this estate:** ~90 object rows, ~200 column rows,
30 index rows, 50 constraint rows, plus a few hundred KB of PL/SQL text.
Runtime: under a minute.

**Definition of done:** run it twice, confirm two distinct `collector_run_id`
values, confirm the object count reconciles exactly against
`SELECT COUNT(*) FROM user_objects`, and confirm the PL/SQL hashes are identical
across both runs.

## Then, in order

1. **Assessment engine** — rules stored as data (a `rules` table with id,
   category, severity, SQL predicate, finding text), not as code. Extending it
   means inserting a row. Score against `04-defects.md`.
2. **Licensing & TCO engine** — deterministic, no model in the loop.
3. **AWS account setup** — only after `05-aws-services.md` access is granted.

## Open decisions

- **Does the demo end clean, or on a blocker?** Ending clean on SE2 completes
  the run. Ending on the agent *blocking* SE2 and showing which features forced
  Enterprise is a stronger sales moment but needs an EE BYOL path to migrate
  into. Cannot have both from one source estate — decide before it matters.
