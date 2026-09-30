"""Stored code -- MySQL's answer to `probes/plsql.py`.

Emits `plsql_source` under its existing name, because Phase 4b's inventory,
Phase 3's effort arithmetic and the report all read it. The dataset holds
SQL/PSM rather than PL/SQL on this path; the name is the schema, not a claim
about the dialect, and `source_engine` on the run says which it is.

Oracle stores stored code one row per *line* (`dba_source`), and Phase 4b's
inventory reassembles it. MySQL returns the whole body in one `routine_definition`
column, so this probe splits it into lines to match -- rather than making Phase
4b handle two shapes. The split is on the body only; the CREATE header MySQL
omits from `routine_definition` is reconstructed so the text compiles when
Phase 4b hands it to a parser.

**`security_type` is the finding that bites on RDS.** A `DEFINER`-rights routine
names an account that must exist on the target, and RDS grants no `SUPER`
privilege, so a definer of `root@localhost` fails at runtime rather than at
migration time. Collected explicitly, not inferred.
"""

import re

from ..db import sha256_text

NAME = "plsql"

# Long source text moves out of the dataset file into plsql_source/<sha>.txt,
# exactly as the Oracle probe does, keeping an 800-character excerpt in the row.
# `collector/run.py` reads this attribute; convert/inventory.py follows the file.
EXTERNALIZE = [("source_inventory.plsql_source", "source_text", "source_sha256", 800)]

_IDENT = re.compile(r"^[A-Za-z0-9_$]+$")

# SHOW CREATE returns the full statement under a column named for the object
# type. Session.fetch lower-cases column names, so these are lower case.
_SHOW = {
    "PROCEDURE": ("SHOW CREATE PROCEDURE", "create procedure"),
    "FUNCTION": ("SHOW CREATE FUNCTION", "create function"),
    "TRIGGER": ("SHOW CREATE TRIGGER", "sql original statement"),
}


def _show_create(s, owner: str, name: str, kind: str) -> str | None:
    """The full CREATE statement, or None.

    **Why SHOW CREATE and not information_schema.ROUTINES.ROUTINE_DEFINITION.**
    ROUTINE_DEFINITION is the body alone -- no CREATE, no name, no parameter list,
    no RETURNS clause. A body without its signature cannot be converted or
    compiled, so Phase 4b would have had to reassemble it from PARAMETERS and get
    every DETERMINISTIC / READS SQL DATA / SQL SECURITY clause right by hand.
    SHOW CREATE is MySQL's own rendering and round-trips.

    Needs SHOW_ROUTINE (8.0.20+) for another user's routines -- without it the
    statement column comes back NULL, and the row falls back to the body.
    Identifiers cannot be bound, so they are guarded and backtick-quoted; they
    come from the catalogue, never from input.
    """
    if not (_IDENT.match(owner or "") and _IDENT.match(name or "")) or kind not in _SHOW:
        return None
    stmt, col = _SHOW[kind]
    rows = s.fetch(f"plsql.show_create.{kind.lower()}.{owner}.{name}",
                   f"{stmt} `{owner}`.`{name}`")
    if not rows:
        return None
    return rows[0].get(col)


def _source_rows(s, routines: list[dict], triggers: list[dict]) -> list[dict]:
    """One row per stored object, in the Oracle `plsql_source` shape.

    **Per object, not per line.** The Oracle dataset is one row per object with
    `object_type`, `object_name` and `source_text`, and `convert/inventory.py`
    builds Phase 4b's inventory from exactly those fields. The first version of
    this probe emitted one row per LINE under different column names, which 4b
    would have read as an estate with no stored code at all -- and the
    dataset-parity selftest passed it, because it compared dataset NAMES rather
    than the columns inside them. Found 2026-09-29 by diffing a real Oracle run
    against a real MySQL run, column by column.

    Triggers are included because Oracle's dataset includes them and Phase 4b
    converts them. Events are not: they have no PL/pgSQL equivalent and are
    Phase 7 residue (pg_cron or out of the database).
    """
    out: list[dict] = []
    objects = [(r["owner"], r["name"], str(r["type"]).upper(), r.get("body")) for r in routines]
    objects += [(t["owner"], t["trigger_name"], "TRIGGER", t.get("trigger_body")) for t in triggers]
    for owner, name, kind, body in objects:
        text = _show_create(s, owner, name, kind) or body or ""
        out.append({
            "owner": owner,
            "object_type": kind,
            "object_name": name,
            "line_count": len(text.splitlines()),
            "char_length": len(text),
            "source_sha256": sha256_text(text),
            "source_text": text,
            # Recorded so a reader can tell a full statement from a bare body.
            "source_origin": ("show_create" if text and text != (body or "")
                              else "routine_definition"),
        })
    return out


def collect(s, owners):
    frag, binds = s.binds("o", owners)

    routines = s.fetch(
        "plsql.routines",
        f"""SELECT routine_schema        AS owner,
                   routine_name          AS name,
                   routine_type          AS type,
                   routine_definition    AS body,
                   dtd_identifier        AS return_type,
                   is_deterministic,
                   sql_data_access,
                   security_type,
                   definer,
                   character_set_client,
                   collation_connection,
                   created,
                   last_altered
            FROM information_schema.routines
            WHERE routine_schema IN ({frag})
            ORDER BY routine_schema, routine_type, routine_name""",
        binds,
    )

    parameters = s.fetch(
        "plsql.parameters",
        f"""SELECT specific_schema  AS owner,
                   specific_name    AS name,
                   routine_type     AS type,
                   ordinal_position AS position,
                   parameter_name,
                   parameter_mode,
                   dtd_identifier   AS data_type
            FROM information_schema.parameters
            WHERE specific_schema IN ({frag})
            ORDER BY specific_schema, specific_name, ordinal_position""",
        binds,
    )

    triggers = s.fetch(
        "plsql.triggers",
        f"""SELECT trigger_schema        AS owner,
                   trigger_name          AS trigger_name,
                   event_object_schema   AS table_owner,
                   event_object_table    AS table_name,
                   event_manipulation    AS triggering_event,
                   action_timing         AS trigger_type,
                   action_orientation    AS orientation,
                   action_statement      AS trigger_body,
                   action_order,
                   definer,
                   'ENABLED'             AS status
            FROM information_schema.triggers
            WHERE trigger_schema IN ({frag})
            ORDER BY trigger_schema, event_object_table, action_order""",
        binds,
    )

    views = s.fetch(
        "plsql.views",
        f"""SELECT table_schema     AS owner,
                   table_name       AS view_name,
                   view_definition  AS text,
                   check_option,
                   is_updatable,
                   definer,
                   security_type
            FROM information_schema.views
            WHERE table_schema IN ({frag})
            ORDER BY table_schema, table_name""",
        binds,
    )

    # The scheduler. Oracle's equivalent is `scheduler_jobs`, and the dataset
    # keeps that name so the two rules reading it still resolve.
    events = s.fetch(
        "plsql.events",
        f"""SELECT event_schema       AS owner,
                   event_name         AS job_name,
                   definer,
                   event_definition   AS job_action,
                   event_type,
                   interval_value,
                   interval_field,
                   starts,
                   ends,
                   status,
                   on_completion,
                   last_executed
            FROM information_schema.events
            WHERE event_schema IN ({frag})
            ORDER BY event_schema, event_name""",
        binds,
    )

    return {
        "source_inventory.plsql_source": _source_rows(s, routines, triggers),
        "source_inventory.mysql_routines": routines,
        "source_inventory.mysql_routine_parameters": parameters,
        "source_inventory.triggers": triggers,
        "source_inventory.views": views,
        "source_inventory.scheduler_jobs": events,
        # MySQL refuses to create a routine that does not parse, so there is no
        # standing population of broken objects the way Oracle has. Shaped and
        # empty rather than omitted -- see EMPTY_DATASET_COLUMNS.
        "source_inventory.plsql_errors": [],
        "source_inventory.invalid_objects": [],
    }
