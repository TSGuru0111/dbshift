"""Constraints -- MySQL's answer to `dba_constraints` / `dba_cons_columns`.

`constraint_type` uses Oracle's single-letter codes (P, R, U, C) because four
assessment rules and Phase 4c's DDL generator both switch on them. MySQL spells
them PRIMARY KEY / FOREIGN KEY / UNIQUE / CHECK, so the mapping happens here
once rather than in every reader.

Two MySQL facts worth knowing, both visible in the output:

  * **A MySQL foreign key always has a backing index**, created automatically if
    the author did not. So `PERF`-style "unindexed foreign key" findings, which
    are real and common on Oracle, cannot fire here -- and a rule that reported
    them would be reporting a defect the engine makes impossible.
  * **CHECK constraints only exist from MySQL 8.0.16.** On an older server
    information_schema.CHECK_CONSTRAINTS is absent, so that branch is queried
    separately and a failure leaves the dataset empty rather than failing the
    probe. `Session.fetch` already logs and returns [] on a driver error, which
    is exactly the behaviour wanted here.
"""

NAME = "constraints"


def collect(s, owners):
    frag, binds = s.binds("o", owners)
    # PK / FK / UNIQUE from TABLE_CONSTRAINTS, joined to REFERENTIAL_CONSTRAINTS
    # for the FK's referenced table and delete rule.
    constraints = s.fetch(
        "constraints.list",
        f"""SELECT tc.table_schema      AS owner,
                   tc.constraint_name   AS constraint_name,
                   CASE tc.constraint_type
                        WHEN 'PRIMARY KEY' THEN 'P'
                        WHEN 'FOREIGN KEY' THEN 'R'
                        WHEN 'UNIQUE'      THEN 'U'
                        WHEN 'CHECK'       THEN 'C'
                        ELSE tc.constraint_type END AS constraint_type,
                   tc.table_name        AS table_name,
                   NULL                 AS search_condition_vc,
                   rc.unique_constraint_schema AS r_owner,
                   rc.unique_constraint_name   AS r_constraint_name,
                   rc.delete_rule       AS delete_rule,
                   CASE WHEN tc.enforced = 'YES' THEN 'ENABLED'
                        ELSE 'DISABLED' END AS status,
                   'NOT DEFERRABLE'     AS deferrable,
                   'IMMEDIATE'          AS deferred,
                   'VALIDATED'          AS validated,
                   'N'                  AS `generated`,
                   NULL                 AS rely,
                   tc.constraint_name   AS index_name,
                   rc.referenced_table_name AS r_table_name,
                   rc.update_rule       AS update_rule
            FROM information_schema.table_constraints tc
            LEFT JOIN information_schema.referential_constraints rc
                   ON rc.constraint_schema = tc.constraint_schema
                  AND rc.constraint_name   = tc.constraint_name
            WHERE tc.table_schema IN ({frag})
            ORDER BY tc.table_schema, tc.table_name, tc.constraint_name""",
        binds,
    )

    # CHECK constraint expressions, MySQL 8.0.16+. Queried alone so that an
    # older server's missing view empties this dataset instead of losing the
    # PK/FK/UNIQUE rows above.
    checks = s.fetch(
        "constraints.checks",
        f"""SELECT cc.constraint_schema AS owner,
                   cc.constraint_name,
                   cc.check_clause      AS search_condition_vc
            FROM information_schema.check_constraints cc
            WHERE cc.constraint_schema IN ({frag})
            ORDER BY cc.constraint_schema, cc.constraint_name""",
        binds,
    )
    # Fold the expressions back onto their constraint rows, so `constraints`
    # carries search_condition_vc the way Oracle's does.
    if checks:
        by_name = {(c["owner"], c["constraint_name"]): c["search_condition_vc"]
                   for c in checks}
        for row in constraints:
            key = (row["owner"], row["constraint_name"])
            if key in by_name:
                row["search_condition_vc"] = by_name[key]
                row["constraint_type"] = "C"

    constraint_columns = s.fetch(
        "constraints.columns",
        f"""SELECT kcu.constraint_schema AS owner,
                   kcu.constraint_name,
                   kcu.table_name,
                   kcu.column_name,
                   kcu.ordinal_position  AS position,
                   kcu.referenced_table_schema,
                   kcu.referenced_table_name,
                   kcu.referenced_column_name
            FROM information_schema.key_column_usage kcu
            WHERE kcu.constraint_schema IN ({frag})
            ORDER BY kcu.constraint_schema, kcu.constraint_name,
                     kcu.ordinal_position""",
        binds,
    )
    return {
        "source_inventory.constraints": constraints,
        "source_inventory.constraint_columns": constraint_columns,
    }
