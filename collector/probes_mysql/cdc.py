"""Binlog configuration -- the evidence a MySQL CDC migration is possible.

The Oracle path reads ARCHIVELOG and supplemental logging from `v$database` and
hands them to `collector/mode.readiness()`. This is the MySQL equivalent, and it
exists as its own probe rather than a corner of `identity.py` because the console
shows it as its own panel and Phase 7's preflight reads it directly.

Four things must all hold before DMS can read a MySQL binlog:

    log_bin = ON             the binary log exists at all
    binlog_format = ROW      statement-based logging cannot be replayed row-wise
    binlog_row_image = FULL  a MINIMAL image omits unchanged columns, so DMS
                             cannot rebuild the target row
    binlog retention         DMS must be able to reach back far enough; on RDS
                             this is a stored procedure, on a self-managed
                             server it is expire_logs_days / binlog_expire_logs_seconds

Reported as facts plus a verdict, never as a decision. A client may declare CDC
against a server that is not ready -- that is a remediation task with a restart
attached, not a reason to refuse the declaration. Phase 2 and Phase 5 are where
an unmet requirement becomes a blocker, which is the same division of labour
`collector/mode.py` documents for Oracle.
"""

NAME = "features"


def collect(s, owners):
    # One row, all the settings that decide CDC. @@ variables rather than
    # information_schema because several have no catalogue row.
    settings = s.fetch(
        "features.binlog_settings",
        """SELECT @@log_bin                        AS log_bin,
                  @@binlog_format                  AS binlog_format,
                  @@binlog_row_image               AS binlog_row_image,
                  @@server_id                      AS server_id,
                  @@gtid_mode                      AS gtid_mode,
                  @@enforce_gtid_consistency       AS enforce_gtid_consistency,
                  @@binlog_expire_logs_seconds     AS binlog_expire_logs_seconds,
                  @@log_bin_trust_function_creators AS log_bin_trust_function_creators,
                  @@innodb_buffer_pool_size        AS innodb_buffer_pool_size,
                  @@max_connections                AS max_connections,
                  @@innodb_file_per_table          AS innodb_file_per_table,
                  @@lower_case_table_names         AS lower_case_table_names,
                  @@sql_mode                       AS sql_mode,
                  @@time_zone                      AS time_zone,
                  @@character_set_server           AS character_set_server,
                  @@collation_server               AS collation_server,
                  @@default_storage_engine         AS default_storage_engine,
                  @@event_scheduler                AS event_scheduler,
                  @@transaction_isolation          AS transaction_isolation""",
    )

    # Tables with no primary key. On the Oracle path this is DQ-001 and matters
    # only for CDC; on MySQL it is worse, because InnoDB will have created a
    # hidden clustered index that DMS cannot address -- so an UPDATE or DELETE
    # has no reliable way to find its target row.
    frag, binds = s.binds("o", owners)
    no_pk = s.fetch(
        "features.tables_without_primary_key",
        f"""SELECT t.table_schema AS owner, t.table_name, t.engine, t.table_rows
            FROM information_schema.tables t
            LEFT JOIN information_schema.table_constraints tc
                   ON tc.table_schema   = t.table_schema
                  AND tc.table_name     = t.table_name
                  AND tc.constraint_type = 'PRIMARY KEY'
            WHERE t.table_schema IN ({frag})
              AND t.table_type = 'BASE TABLE'
              AND tc.constraint_name IS NULL
            ORDER BY t.table_schema, t.table_name""",
        binds,
    )

    # Whether this account can actually read replication state. A missing
    # REPLICATION CLIENT grant is the MySQL analogue of the SELECT ANY
    # DICTIONARY lesson: the query fails rather than returning fewer rows, and
    # the remedy is a named grant.
    master_status = s.fetch("features.binlog_position", "SHOW MASTER STATUS")

    # `feature_usage` keeps its Oracle name: one rule reads it, and the question
    # it answers -- "which licensable or migration-relevant features does this
    # estate actually use" -- is the same question on both engines. MySQL has no
    # licence tiers, so every row here is about migration effort, never cost.
    feature_usage = []
    if settings:
        v = settings[0]
        def feature(name, used, detail):
            feature_usage.append({
                "name": name,
                "detected_usages": 1 if used else 0,
                "currently_used": "TRUE" if used else "FALSE",
                "description": detail,
                # Oracle's DBA_FEATURE_USAGE_STATISTICS columns, present and NULL.
                # Found 2026-09-29: sizing/facts.py selects last_usage_date by name,
                # and the dataset-parity selftest checked dataset NAMES, not the
                # columns inside one -- so a populated MySQL dataset with a narrower
                # shape passed parity and failed in Phase 3.
                "last_usage_date": None,
                "first_usage_date": None,
                "version": None,
            })

        feature("Binary logging", str(v.get("log_bin")) in ("1", "ON"),
                "The binary log is what DMS change data capture reads.")
        feature("GTID replication", str(v.get("gtid_mode") or "").upper() == "ON",
                "Global transaction identifiers; affects how a replica is positioned.")
        feature("Event scheduler", str(v.get("event_scheduler") or "").upper() == "ON",
                "Server-side scheduled jobs. RDS manages the scheduler differently "
                "from a self-managed server.")
        feature("Non-InnoDB storage engine",
                str(v.get("default_storage_engine") or "").lower() != "innodb",
                "A default engine other than InnoDB means new tables are created "
                "without transactions or crash recovery.")

    return {
        "source_inventory.mysql_binlog": settings,
        "source_inventory.mysql_binlog_position": master_status,
        "source_inventory.mysql_tables_without_pk": no_pk,
        "source_inventory.feature_usage": feature_usage,
        # Oracle-only catalogue concepts. Shaped and empty: MySQL has no
        # tablespaces a migration cares about (InnoDB's are per-table files),
        # no AWR, no profiles, no synonyms, no DB links, no AQ queues and no
        # materialized views. Declared so every rule referencing one parses.
        "source_inventory.tablespaces": [],
        "source_inventory.awr_coverage": [],
        "source_inventory.profiles": [],
        "source_inventory.synonyms": [],
        "source_inventory.db_links": [],
        "source_inventory.queues": [],
        "source_inventory.materialized_views": [],
        "source_inventory.mview_logs": [],
        "source_inventory.directories": [],
        "source_inventory.external_tables": [],
        "source_inventory.text_indexes": [],
        "source_inventory.xml_schemas": [],
        "source_inventory.types": [],
        "source_inventory.lobs": [],
        "source_inventory.database_options": [],
        "source_inventory.nls_parameters": [],
        "source_inventory.os_statistics": [],
        "source_inventory.system_metrics": [],
    }
