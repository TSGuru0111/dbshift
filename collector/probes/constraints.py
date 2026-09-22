from ..db import in_binds

NAME = "constraints"


def collect(s, owners):
    frag, binds = in_binds("o", owners)
    # search_condition_vc avoids the LONG column on search_condition.
    constraints = s.fetch(
        "constraints.list",
        f"""SELECT owner, constraint_name, constraint_type, table_name,
                   search_condition_vc, r_owner, r_constraint_name, delete_rule,
                   status, deferrable, deferred, validated, generated, rely, index_name
            FROM dba_constraints
            WHERE owner IN ({frag})
            ORDER BY owner, table_name, constraint_name""",
        binds,
    )
    constraint_columns = s.fetch(
        "constraints.columns",
        f"""SELECT owner, constraint_name, table_name, column_name, position
            FROM dba_cons_columns
            WHERE owner IN ({frag})
            ORDER BY owner, constraint_name, NVL(position, 0)""",
        binds,
    )
    return {
        "source_inventory.constraints": constraints,
        "source_inventory.constraint_columns": constraint_columns,
    }
