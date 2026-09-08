from ..db import in_binds

NAME = "storage"


def collect(s, owners):
    frag, binds = in_binds("o", owners)
    # Physical truth. Target sizing comes from here, never from num_rows.
    segments = s.fetch(
        "storage.segments",
        f"""SELECT owner, segment_name, partition_name, segment_type, tablespace_name,
                   bytes, blocks, extents
            FROM dba_segments
            WHERE owner IN ({frag})
            ORDER BY owner, segment_name, NVL(partition_name,' ')""",
        binds,
    )
    lobs = s.fetch(
        "storage.lobs",
        f"""SELECT owner, table_name, column_name, segment_name, tablespace_name,
                   in_row, securefile, compression, deduplication
            FROM dba_lobs
            WHERE owner IN ({frag})
            ORDER BY owner, table_name, column_name""",
        binds,
    )
    tablespaces = s.fetch(
        "storage.tablespaces",
        """SELECT tablespace_name, block_size, status, contents, extent_management,
                  segment_space_management, encrypted, compress_for
           FROM dba_tablespaces
           ORDER BY tablespace_name""",
    )
    return {
        "source_inventory.segments": segments,
        "source_inventory.lobs": lobs,
        "source_inventory.tablespaces": tablespaces,
    }
