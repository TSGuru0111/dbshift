"""Size and partitioning -- MySQL's answer to `probes/storage.py` and `partitions.py`.

`segments` keeps its Oracle name because two rules and Phase 3's sizing read it
for the estate's size. On Oracle a segment is a physical allocation; on MySQL the
equivalent figures are `data_length` and `index_length` per table, so a segment
row here is one table's storage rather than one extent map. The numbers answer
the same question -- how big is this thing, and what will it cost on the target.

Sequences do not exist in MySQL. `AUTO_INCREMENT` is the analogue and it is a
*column property*, not an object, which is why Phase 7's residue handling differs:
there is no sequence to create on the target, but every auto-increment column
needs its current high-water mark carried across or the first insert after cutover
collides. Collected here as `sequences` so the existing readers resolve, with the
table and column named.
"""

NAME = "storage"


def collect(s, owners):
    frag, binds = s.binds("o", owners)

    segments = s.fetch(
        "storage.segments",
        f"""SELECT table_schema AS owner,
                   table_name   AS segment_name,
                   'TABLE'      AS segment_type,
                   NULL         AS tablespace_name,
                   (IFNULL(data_length, 0) + IFNULL(index_length, 0)) AS bytes,
                   NULL         AS blocks,
                   NULL         AS extents,
                   data_length,
                   index_length,
                   data_free,
                   engine
            FROM information_schema.tables
            WHERE table_schema IN ({frag})
              AND table_type = 'BASE TABLE'
            ORDER BY (IFNULL(data_length,0) + IFNULL(index_length,0)) DESC""",
        binds,
    )

    partitioned = s.fetch(
        "storage.partitioned_tables",
        f"""SELECT table_schema AS owner, table_name,
                   COUNT(*)                       AS partition_count,
                   MAX(partition_method)          AS partitioning_type,
                   MAX(subpartition_method)       AS subpartitioning_type,
                   MAX(partition_expression)      AS partitioning_key
            FROM information_schema.partitions
            WHERE table_schema IN ({frag})
              AND partition_name IS NOT NULL
            GROUP BY table_schema, table_name
            ORDER BY table_schema, table_name""",
        binds,
    )

    table_partitions = s.fetch(
        "storage.table_partitions",
        f"""SELECT table_schema AS owner, table_name,
                   partition_name, subpartition_name,
                   partition_ordinal_position AS partition_position,
                   partition_method, partition_expression,
                   partition_description, table_rows,
                   data_length, index_length
            FROM information_schema.partitions
            WHERE table_schema IN ({frag})
              AND partition_name IS NOT NULL
            ORDER BY table_schema, table_name, partition_ordinal_position""",
        binds,
    )

    partition_key_columns = s.fetch(
        "storage.partition_key_columns",
        f"""SELECT DISTINCT table_schema AS owner, table_name,
                   partition_expression AS column_name,
                   1 AS column_position
            FROM information_schema.partitions
            WHERE table_schema IN ({frag})
              AND partition_expression IS NOT NULL
            ORDER BY table_schema, table_name""",
        binds,
    )

    # AUTO_INCREMENT as MySQL's sequence analogue -- see the module docstring.
    sequences = s.fetch(
        "storage.auto_increment",
        f"""SELECT c.table_schema   AS sequence_owner,
                   CONCAT(c.table_name, '.', c.column_name) AS sequence_name,
                   1                AS min_value,
                   NULL             AS max_value,
                   1                AS increment_by,
                   'N'              AS cycle_flag,
                   'N'              AS order_flag,
                   0                AS cache_size,
                   IFNULL(t.auto_increment, 1) AS last_number,
                   c.table_name,
                   c.column_name,
                   c.data_type,
                   -- An unsigned auto-increment near its ceiling is a real
                   -- migration risk: PostgreSQL bigint is signed, so an
                   -- unsigned BIGINT above 2^63-1 has nowhere to land.
                   CASE WHEN c.column_type LIKE '%unsigned%'
                        THEN 'YES' ELSE 'NO' END AS is_unsigned
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema
             AND t.table_name   = c.table_name
            WHERE c.table_schema IN ({frag})
              AND c.extra LIKE '%auto_increment%'
            ORDER BY c.table_schema, c.table_name""",
        binds,
    )

    return {
        "source_inventory.segments": segments,
        "source_inventory.partitioned_tables": partitioned,
        "source_inventory.table_partitions": table_partitions,
        "source_inventory.partition_key_columns": partition_key_columns,
        "source_inventory.sequences": sequences,
        # Server configuration, Oracle's `parameters` equivalent. The whole
        # global variable set, so a rule can read any setting without a new probe.
        "source_inventory.parameters": s.fetch(
            "storage.global_variables",
            """SELECT variable_name AS name, variable_value AS value,
                      'TRUE' AS isdefault
               FROM performance_schema.global_variables
               ORDER BY variable_name""",
        ),
    }
