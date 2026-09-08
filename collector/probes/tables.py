from ..db import in_binds

NAME = "tables"


def collect(s, owners):
    frag, binds = in_binds("o", owners)
    tables = s.fetch(
        "tables.list",
        f"""SELECT owner, table_name, tablespace_name, status, num_rows, blocks,
                   empty_blocks, avg_row_len, chain_cnt, partitioned, temporary,
                   iot_type, nested, compression, compress_for, degree,
                   last_analyzed, logging, row_movement
            FROM dba_tables
            WHERE owner IN ({frag})
            ORDER BY owner, table_name""",
        binds,
    )
    # dba_tab_cols, not dba_tab_columns: the virtual/hidden flags exist only on the
    # former, and a virtual column cannot be inserted into on the target.
    # user_generated='YES' drops Oracle's internal LOB and object-type columns.
    columns = s.fetch(
        "tables.columns",
        f"""SELECT owner, table_name, column_name, column_id, data_type, data_type_mod,
                   data_type_owner, data_length, data_precision, data_scale,
                   char_length, char_used, character_set_name, collation, nullable,
                   default_length, data_default, default_on_null, num_nulls,
                   num_distinct, avg_col_len, hidden_column, virtual_column,
                   identity_column, user_generated
            FROM dba_tab_cols
            WHERE owner IN ({frag}) AND user_generated = 'YES'
            ORDER BY owner, table_name, column_id""",
        binds,
    )
    comments = s.fetch(
        "tables.comments",
        f"""SELECT owner, table_name, comments
            FROM dba_tab_comments
            WHERE owner IN ({frag}) AND comments IS NOT NULL
            ORDER BY owner, table_name""",
        binds,
    )
    return {
        "source_inventory.tables": tables,
        "source_inventory.columns": columns,
        "source_inventory.table_comments": comments,
    }
