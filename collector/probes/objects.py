from ..db import in_binds

NAME = "objects"


def collect(s, owners):
    frag, binds = in_binds("o", owners)
    rows = s.fetch(
        "objects.census",
        f"""SELECT owner, object_name, subobject_name, object_id, object_type,
                   created, last_ddl_time, timestamp, status, temporary,
                   generated, secondary
            FROM dba_objects
            WHERE owner IN ({frag})
            ORDER BY owner, object_type, object_name, NVL(subobject_name,' ')""",
        binds,
    )
    return {"source_inventory.objects": rows}
