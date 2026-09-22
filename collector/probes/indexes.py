from ..db import in_binds

NAME = "indexes"


def collect(s, owners):
    frag, binds = in_binds("o", owners)
    indexes = s.fetch(
        "indexes.list",
        f"""SELECT owner, index_name, index_type, table_owner, table_name, table_type,
                   uniqueness, compression, tablespace_name, status, partitioned,
                   temporary, generated, num_rows, distinct_keys, clustering_factor,
                   leaf_blocks, blevel, degree, visibility, last_analyzed
            FROM dba_indexes
            WHERE owner IN ({frag})
            ORDER BY owner, index_name""",
        binds,
    )
    index_columns = s.fetch(
        "indexes.columns",
        f"""SELECT index_owner, index_name, table_owner, table_name, column_name,
                   column_position, descend
            FROM dba_ind_columns
            WHERE index_owner IN ({frag})
            ORDER BY index_owner, index_name, column_position""",
        binds,
    )
    expressions = s.fetch(
        "indexes.expressions",
        f"""SELECT index_owner, index_name, table_owner, table_name,
                   column_expression, column_position
            FROM dba_ind_expressions
            WHERE index_owner IN ({frag})
            ORDER BY index_owner, index_name, column_position""",
        binds,
    )
    return {
        "source_inventory.indexes": indexes,
        "source_inventory.index_columns": index_columns,
        "source_inventory.index_expressions": expressions,
    }
