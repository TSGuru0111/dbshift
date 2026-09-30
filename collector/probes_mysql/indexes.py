"""Indexes and their columns -- MySQL's answer to `dba_indexes`.

information_schema.STATISTICS is one row per indexed *column*, where Oracle's
`dba_indexes` is one row per index. So `indexes` aggregates and `index_columns`
does not, which matches the two Oracle datasets exactly.

`index_expressions` is emitted empty-but-shaped. MySQL 8 does support functional
indexes, but it records them as a generated column behind the scenes rather than
as an expression on the index -- so there is nothing for this dataset to hold,
and the honest answer is no rows rather than a guess. Declared in
`EMPTY_DATASET_COLUMNS` so the rule reading it still parses.
"""

NAME = "indexes"


def collect(s, owners):
    frag, binds = s.binds("o", owners)
    indexes = s.fetch(
        "indexes.list",
        f"""SELECT index_schema                       AS owner,
                   index_name                         AS index_name,
                   CASE WHEN index_type = 'FULLTEXT' THEN 'FULLTEXT'
                        WHEN index_type = 'SPATIAL'  THEN 'SPATIAL'
                        ELSE 'NORMAL' END             AS index_type,
                   table_schema                       AS table_owner,
                   table_name                         AS table_name,
                   'TABLE'                            AS table_type,
                   CASE WHEN MAX(non_unique) = 0 THEN 'UNIQUE'
                        ELSE 'NONUNIQUE' END          AS uniqueness,
                   'DISABLED'                         AS compression,
                   NULL                               AS tablespace_name,
                   'VALID'                            AS status,
                   'NO'                               AS partitioned,
                   'N'                                AS temporary,
                   'N'                                AS `generated`,
                   NULL                               AS num_rows,
                   MAX(cardinality)                   AS distinct_keys,
                   NULL                               AS clustering_factor,
                   NULL                               AS leaf_blocks,
                   NULL                               AS blevel,
                   NULL                               AS degree,
                   CASE WHEN MAX(is_visible) = 'NO' THEN 'INVISIBLE'
                        ELSE 'VISIBLE' END            AS visibility,
                   NULL                               AS last_analyzed,
                   COUNT(*)                           AS column_count,
                   MAX(index_type)                    AS mysql_index_type
            FROM information_schema.statistics
            WHERE index_schema IN ({frag})
            GROUP BY index_schema, index_name, table_schema, table_name, index_type
            ORDER BY index_schema, table_name, index_name""",
        binds,
    )
    index_columns = s.fetch(
        "indexes.columns",
        f"""SELECT index_schema   AS index_owner,
                   index_name     AS index_name,
                   table_schema   AS table_owner,
                   table_name     AS table_name,
                   column_name    AS column_name,
                   seq_in_index   AS column_position,
                   CASE WHEN collation = 'D' THEN 'DESC' ELSE 'ASC' END AS descend,
                   sub_part       AS prefix_length
            FROM information_schema.statistics
            WHERE index_schema IN ({frag})
            ORDER BY index_schema, index_name, seq_in_index""",
        binds,
    )
    return {
        "source_inventory.indexes": indexes,
        "source_inventory.index_columns": index_columns,
        # Shaped-but-empty: see the module docstring.
        "source_inventory.index_expressions": [],
    }
