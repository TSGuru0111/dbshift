from ..db import in_binds

NAME = "partitions"


def collect(s, owners):
    frag, binds = in_binds("o", owners)
    part_tables = s.fetch(
        "partitions.tables",
        f"""SELECT owner, table_name, partitioning_type, subpartitioning_type,
                   partition_count, def_tablespace_name, partitioning_key_count,
                   interval, autolist
            FROM dba_part_tables
            WHERE owner IN ({frag})
            ORDER BY owner, table_name""",
        binds,
    )
    tab_partitions = s.fetch(
        "partitions.table_partitions",
        f"""SELECT table_owner, table_name, partition_name, partition_position,
                   high_value, tablespace_name, num_rows, blocks, compression,
                   last_analyzed
            FROM dba_tab_partitions
            WHERE table_owner IN ({frag})
            ORDER BY table_owner, table_name, partition_position""",
        binds,
    )
    part_keys = s.fetch(
        "partitions.key_columns",
        f"""SELECT owner, name, object_type, column_name, column_position
            FROM dba_part_key_columns
            WHERE owner IN ({frag})
            ORDER BY owner, name, column_position""",
        binds,
    )
    return {
        "source_inventory.partitioned_tables": part_tables,
        "source_inventory.table_partitions": tab_partitions,
        "source_inventory.partition_key_columns": part_keys,
    }
