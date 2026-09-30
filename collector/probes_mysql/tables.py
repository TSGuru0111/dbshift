"""Tables and columns -- MySQL's answer to `dba_tables` / `dba_tab_cols`.

Emits `tables`, `columns` and `table_comments` with Oracle's column names, plus
two datasets that have no Oracle equivalent and carry the findings a MySQL
migration actually turns on:

    mysql_table_storage   storage engine and row format per table
    mysql_charsets        character set and collation per table and column

Those two are the reason a MySQL assessment is not just an Oracle assessment
with different SQL. A MyISAM table cannot be replicated by DMS CDC and has no
transactions; a `utf8mb3` column silently truncates 4-byte characters; a
case-insensitive collation makes `'A' = 'a'` true on the source and false on a
PostgreSQL target, which changes row identity rather than just rendering.

**`num_rows` is an estimate and is labelled as one.** information_schema.TABLES
reports InnoDB's sampled row count, which is routinely 20-50% wrong. Oracle's
`num_rows` is also an estimate (last gathered statistics), so the column means
the same thing on both engines -- but Phase 8 must never compare it. The exact
counts come from `dataprofile.py`, which counts rows.
"""

NAME = "tables"


def collect(s, owners):
    frag, binds = s.binds("o", owners)
    tables = s.fetch(
        "tables.list",
        f"""SELECT table_schema        AS owner,
                   table_name          AS table_name,
                   NULL                AS tablespace_name,
                   'VALID'             AS status,
                   table_rows          AS num_rows,
                   NULL                AS blocks,
                   NULL                AS empty_blocks,
                   avg_row_length      AS avg_row_len,
                   NULL                AS chain_cnt,
                   CASE WHEN create_options LIKE '%partitioned%'
                        THEN 'YES' ELSE 'NO' END AS partitioned,
                   CASE WHEN table_type LIKE '%TEMPORARY%'
                        THEN 'Y' ELSE 'N' END    AS temporary,
                   NULL                AS iot_type,
                   'NO'                AS nested,
                   CASE WHEN row_format IN ('COMPRESSED')
                        THEN 'ENABLED' ELSE 'DISABLED' END AS compression,
                   row_format          AS compress_for,
                   NULL                AS degree,
                   update_time         AS last_analyzed,
                   'YES'               AS logging,
                   'DISABLED'          AS row_movement,
                   -- Beyond Oracle's shape, and the point of the MySQL path:
                   engine              AS engine,
                   table_collation     AS table_collation,
                   data_length         AS data_length,
                   index_length        AS index_length,
                   auto_increment      AS auto_increment
            FROM information_schema.tables
            WHERE table_schema IN ({frag})
              AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name""",
        binds,
    )

    # `user_generated` is always YES: MySQL exposes no hidden or system-generated
    # columns in information_schema the way dba_tab_cols does, so there is
    # nothing to filter. Kept as a column so the two engines' `columns` datasets
    # stay identical in shape and the loader needs no branch.
    columns = s.fetch(
        "tables.columns",
        f"""SELECT table_schema                    AS owner,
                   table_name                      AS table_name,
                   column_name                     AS column_name,
                   ordinal_position                AS column_id,
                   UPPER(data_type)                AS data_type,
                   column_type                     AS data_type_mod,
                   NULL                            AS data_type_owner,
                   IFNULL(character_octet_length, numeric_precision) AS data_length,
                   numeric_precision               AS data_precision,
                   numeric_scale                   AS data_scale,
                   character_maximum_length        AS char_length,
                   CASE WHEN character_maximum_length IS NULL THEN NULL
                        ELSE 'C' END               AS char_used,
                   character_set_name              AS character_set_name,
                   collation_name                  AS collation,
                   is_nullable                     AS nullable,
                   NULL                            AS default_length,
                   column_default                  AS data_default,
                   'NO'                            AS default_on_null,
                   NULL                            AS num_nulls,
                   NULL                            AS num_distinct,
                   NULL                            AS avg_col_len,
                   'NO'                            AS hidden_column,
                   -- VIRTUAL/STORED GENERATED only. A bare '%GENERATED%' also
                   -- matched DEFAULT_GENERATED -- every DEFAULT CURRENT_TIMESTAMP
                   -- column -- and Phase 4c dropped those as computed columns.
                   CASE WHEN extra LIKE '%VIRTUAL GENERATED%'
                          OR extra LIKE '%STORED GENERATED%' THEN 'YES'
                        ELSE 'NO' END              AS virtual_column,
                   generation_expression           AS generation_expression,
                   CASE WHEN extra LIKE '%auto_increment%' THEN 'YES'
                        ELSE 'NO' END              AS identity_column,
                   'YES'                           AS user_generated,
                   -- MySQL-only, read by the MySQL rules:
                   column_key                      AS column_key,
                   extra                           AS extra
            FROM information_schema.columns
            WHERE table_schema IN ({frag})
            ORDER BY table_schema, table_name, ordinal_position""",
        binds,
    )

    comments = s.fetch(
        "tables.comments",
        f"""SELECT table_schema AS owner, table_name, table_comment AS comments
            FROM information_schema.tables
            WHERE table_schema IN ({frag})
              AND table_comment IS NOT NULL AND table_comment <> ''
            ORDER BY table_schema, table_name""",
        binds,
    )

    # The storage-engine audit. One row per table, so a rule can count MyISAM
    # tables and the console can show the split without re-deriving it from
    # `tables`.
    storage = s.fetch(
        "tables.storage_engine",
        f"""SELECT table_schema AS owner, table_name, engine, row_format,
                   table_collation, data_length, index_length, table_rows,
                   create_options, auto_increment,
                   CASE WHEN engine = 'InnoDB' THEN 'NO' ELSE 'YES' END
                        AS needs_engine_change
            FROM information_schema.tables
            WHERE table_schema IN ({frag})
              AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name""",
        binds,
    )

    # Charset and collation, per column, with the two properties that matter
    # called out so a rule does not have to know the charset catalogue:
    #   is_utf8mb3        -- cannot store 4-byte characters (emoji, some CJK)
    #   case_insensitive  -- 'A' = 'a' here, but not on a PostgreSQL target
    charsets = s.fetch(
        "tables.charsets",
        f"""SELECT c.table_schema AS owner, c.table_name, c.column_name,
                   c.character_set_name, c.collation_name,
                   t.table_collation,
                   CASE WHEN c.character_set_name IN ('utf8', 'utf8mb3')
                        THEN 'YES' ELSE 'NO' END AS is_utf8mb3,
                   CASE WHEN c.collation_name LIKE '%\\_ci'
                        THEN 'YES' ELSE 'NO' END AS case_insensitive
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema
             AND t.table_name   = c.table_name
            WHERE c.table_schema IN ({frag})
              AND c.character_set_name IS NOT NULL
            ORDER BY c.table_schema, c.table_name, c.ordinal_position""",
        binds,
    )

    return {
        "source_inventory.tables": tables,
        "source_inventory.columns": columns,
        "source_inventory.table_comments": comments,
        "source_inventory.mysql_table_storage": storage,
        "source_inventory.mysql_charsets": charsets,
    }
