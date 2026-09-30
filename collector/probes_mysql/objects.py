"""The object census -- MySQL's answer to `dba_objects`.

MySQL has no single catalogue of every object, so this is a UNION over the five
information_schema views that hold them. The column names match Oracle's
`objects` dataset exactly, because `v_user_objects` is built over this table and
`RDS-001` (reserved words) and the console's object tiles read it by column.

`object_type` uses Oracle's vocabulary -- TABLE, VIEW, INDEX, PROCEDURE,
FUNCTION, TRIGGER, EVENT -- because the assessment rules, the report and the
console all group by it. The one MySQL type with no Oracle equivalent is EVENT
(the scheduler), and it keeps its own name rather than being mapped onto
Oracle's JOB: they are different enough that a client reading the report should
see which one their estate actually uses.
"""

NAME = "objects"


def collect(s, owners):
    frag, binds = s.binds("o", owners)
    # Five UNION branches, one per catalogue. `created` and `last_ddl_time` are
    # genuinely available for tables and routines and genuinely absent for
    # indexes and columns -- NULL rather than a substituted value, so nothing
    # downstream mistakes "MySQL does not record this" for a real timestamp.
    objects = s.fetch(
        "objects.census",
        f"""SELECT table_schema  AS owner,
                   table_name    AS object_name,
                   NULL          AS subobject_name,
                   NULL          AS object_id,
                   CASE WHEN table_type = 'VIEW' THEN 'VIEW' ELSE 'TABLE' END AS object_type,
                   create_time   AS created,
                   IFNULL(update_time, create_time) AS last_ddl_time,
                   create_time   AS `timestamp`,
                   'VALID'       AS status,
                   CASE WHEN table_type LIKE '%TEMPORARY%' THEN 'Y' ELSE 'N' END AS temporary,
                   'N'           AS `generated`,
                   'N'           AS secondary
            FROM information_schema.tables
            WHERE table_schema IN ({frag})

            UNION ALL
            SELECT routine_schema, routine_name, NULL, NULL,
                   routine_type,                      -- PROCEDURE or FUNCTION
                   created, last_altered, created,
                   'VALID', 'N', 'N', 'N'
            FROM information_schema.routines
            WHERE routine_schema IN ({frag})

            UNION ALL
            SELECT trigger_schema, trigger_name, NULL, NULL,
                   'TRIGGER', created, created, created,
                   'VALID', 'N', 'N', 'N'
            FROM information_schema.triggers
            WHERE trigger_schema IN ({frag})

            UNION ALL
            SELECT event_schema, event_name, NULL, NULL,
                   'EVENT', created, last_altered, created,
                   CASE WHEN status = 'ENABLED' THEN 'VALID' ELSE 'DISABLED' END,
                   'N', 'N', 'N'
            FROM information_schema.events
            WHERE event_schema IN ({frag})

            UNION ALL
            -- One row per index, not per indexed column: an index named in five
            -- STATISTICS rows is one object. PRIMARY is included because Oracle
            -- counts its backing index as an object too.
            SELECT DISTINCT index_schema, index_name, NULL, NULL,
                   'INDEX', NULL, NULL, NULL,
                   'VALID', 'N', 'N', 'N'
            FROM information_schema.statistics
            WHERE index_schema IN ({frag})

            ORDER BY owner, object_type, object_name""",
        binds * 5,
    )
    return {"source_inventory.objects": objects}
