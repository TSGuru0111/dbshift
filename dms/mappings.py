"""Table mappings and task settings: what DMS is told to move, and how.

Two JSON documents drive every DMS task, and both are built here from the
records the earlier phases produced rather than hand-written:

  table mappings   which schemas and tables, and how names are transformed
  task settings    LOB handling, logging, error behaviour, target preparation

The transformation rules matter more than they look. Oracle folds unquoted
identifiers to UPPER CASE; PostgreSQL folds them to lower case. A table Oracle
calls CUSTOMER is `customer` in PostgreSQL, and code that says `SELECT * FROM
customer` works in both. Move the name across unchanged and PostgreSQL ends up
with a table called "CUSTOMER" that only quoted SQL can reach -- every
converted PL/pgSQL function from Phase 4b would fail to find it.

So on the heterogeneous path every schema, table and column name is lowercased.
On the homogeneous path nothing is transformed, because both sides fold the
same way.
"""

from __future__ import annotations

from . import policy

# Object types DMS cannot move and should never be asked to. Listing them keeps
# the mapping honest: they are excluded by name, not silently absent.
NOT_MOVED_BY_DMS = {
    "PROCEDURE": "stored code -- Phase 4b converts it, DMS moves only data",
    "FUNCTION": "stored code -- Phase 4b converts it, DMS moves only data",
    "PACKAGE": "stored code -- Phase 4b converts it, DMS moves only data",
    "PACKAGE BODY": "stored code -- Phase 4b converts it, DMS moves only data",
    "TRIGGER": "stored code -- Phase 4b converts it, DMS moves only data",
    "TYPE": "stored code -- Phase 4b converts it, DMS moves only data",
    "SEQUENCE": "DMS does not migrate sequences; their current values are set after the load",
    "VIEW": "a view is a query, not data; it is recreated on the target",
    "MATERIALIZED VIEW": "recreated and refreshed on the target rather than replicated",
    "SYNONYM": "no PostgreSQL equivalent; resolved during conversion",
    "DATABASE LINK": "a connection to elsewhere, not data",
    "JOB": "scheduler jobs are Phase 4 runbook steps, not replicated objects",
}


# Tables Oracle manages on the user's behalf. Migrating them is always wrong:
# they are an implementation detail of a feature, and the feature is rebuilt on
# the target rather than copied. The same prefixes the Phase 10 report uses, so
# "how many tables does this estate have" has one answer everywhere.
INTERNAL_PREFIXES = ("DR$", "AQ$", "MLOG$", "RUPD$", "SYS_")

# Why each excluded table is excluded, in the user's terms. A table silently
# missing from a migration is indistinguishable from a bug.
EXCLUDED_TABLE_REASONS = {
    "DR$": "an Oracle Text index internal table; the index is rebuilt on the target",
    "AQ$": "an Advanced Queuing internal table; the queue has no PostgreSQL equivalent",
    "MLOG$": "a materialized view log; the view is refreshed on the target, not replicated",
    "RUPD$": "an updatable materialized view internal table",
    "SYS_": "an Oracle-generated internal table",
}


def _internal_reason(name: str) -> str | None:
    upper = (name or "").upper()
    for prefix in INTERNAL_PREFIXES:
        if upper.startswith(prefix):
            return EXCLUDED_TABLE_REASONS[prefix]
    return None


def select_tables(table_rows: list[dict], objects: list[dict], schema: str,
                  external_tables: list[dict] | None = None,
                  queues: list[dict] | None = None) -> dict:
    """Which of the schema's tables DMS should actually be given.

    Returns both halves, because the excluded half is what a person needs to see:
    a migration that quietly moves 9 of 21 tables looks broken unless the other
    12 are named with a reason.

    Five kinds are held back:

      internal      Oracle's own tables behind Text, AQ and materialized views
      external      the data lives in a file, not the database (see RDS-004)
      mview         a materialized view is a query result; it is refreshed, not copied
      queue         an Advanced Queuing payload table -- an ordinary TABLE in the
                    catalogue, but its columns are Oracle AQ object types that
                    exist nowhere else, so neither its data nor its DDL can move
      iot           index-organized tables are migrated, with a note about how

    One subtlety: a materialized view appears in DBA_OBJECTS **twice**, once as
    MATERIALIZED VIEW and once as the TABLE holding its rows. Keying a dict by
    name keeps whichever came last, which silently let MV_LOAN_SUMMARY through
    as an ordinary table. Every type per object is collected instead.
    """
    types: dict[tuple, set] = {}
    for o in objects or []:
        key = (o.get("owner"), (o.get("object_name") or "").upper())
        types.setdefault(key, set()).add(o.get("object_type"))

    external = {(e.get("table_name") or "").upper() for e in external_tables or []}
    # A queue table is named by the queue that owns it. It does not carry an
    # AQ$ prefix -- LOAN_EVENT_QTAB looks like an ordinary application table --
    # so the prefix rule alone lets it through, and its AQ object-type columns
    # then fail to create.
    queue_tables = {(q.get("queue_table") or "").upper()
                    for q in queues or [] if q.get("owner") == schema}
    include, exclude = [], []

    for r in table_rows or []:
        if r.get("owner") != schema:
            continue
        name = r.get("table_name")
        upper = (name or "").upper()

        reason = _internal_reason(name)
        if reason:
            exclude.append({"table": name, "kind": "internal", "why": reason})
            continue
        if "MATERIALIZED VIEW" in types.get((schema, upper), set()):
            exclude.append({"table": name, "kind": "mview",
                            "why": "a materialized view holds a query result; it is recreated and "
                                   "refreshed on the target rather than replicated"})
            continue
        if upper in queue_tables:
            exclude.append({"table": name, "kind": "queue",
                            "why": "an Advanced Queuing payload table. Its columns are Oracle AQ "
                                   "object types with no equivalent anywhere else, so the table "
                                   "cannot be created or loaded. The queue itself has no "
                                   "PostgreSQL equivalent either -- it becomes SQS or an "
                                   "application change."})
            continue
        if upper in external:
            exclude.append({"table": name, "kind": "external",
                            "why": "an external table reads a file on the database server, and "
                                   "there is no such file on a managed target. The file moves to "
                                   "S3 and the table is recreated over it -- finding RDS-004"})
            continue
        if r.get("iot_type"):
            # Not silently dropped: DMS can move an IOT, but PostgreSQL has no
            # equivalent storage organisation, so it becomes an ordinary table
            # with an index. Worth saying rather than discovering later.
            include.append(name)
            exclude.append({"table": name, "kind": "iot_note",
                            "why": "index-organized on the source; it migrates as an ordinary "
                                   "table with a primary key index, which is the PostgreSQL "
                                   "equivalent", "included": True})
            continue
        include.append(name)

    return {"include": sorted(include),
            "exclude": sorted(exclude, key=lambda x: x["table"] or "")}


def table_mappings(*, schema: str, tables: list[str] | None = None,
                   lowercase: bool, exclude: list[str] | None = None) -> dict:
    """Which tables move, and under what names.

    `tables` None means every table in the schema -- a % wildcard, which is what
    DMS understands. Naming them explicitly is better when the set is known,
    because a table added to the source between planning and running would
    otherwise be picked up silently.
    """
    rules: list[dict] = []
    rule_id = 1

    def add(rule: dict) -> None:
        nonlocal rule_id
        rules.append({**rule, "rule-id": str(rule_id), "rule-name": str(rule_id)})
        rule_id += 1

    if tables:
        for t in sorted(tables):
            add({"rule-type": "selection",
                 "object-locator": {"schema-name": schema, "table-name": t},
                 "rule-action": "include", "filters": []})
    else:
        add({"rule-type": "selection",
             "object-locator": {"schema-name": schema, "table-name": "%"},
             "rule-action": "include", "filters": []})

    for t in sorted(exclude or []):
        add({"rule-type": "selection",
             "object-locator": {"schema-name": schema, "table-name": t},
             "rule-action": "exclude", "filters": []})

    if lowercase:
        # Schema, then table, then column. DMS applies all three; without the
        # column rule, lower-cased tables would still have UPPER CASE columns,
        # which breaks unquoted SQL just as thoroughly.
        for target in ("schema", "table", "column"):
            locator = {"schema-name": schema}
            if target in ("table", "column"):
                locator["table-name"] = "%"
            if target == "column":
                locator["column-name"] = "%"
            add({"rule-type": "transformation", "rule-target": target,
                 "object-locator": locator, "rule-action": "convert-lowercase",
                 "value": None, "old-value": None})

    return {"rules": rules}


def task_settings(*, migration_type: str, cloudwatch: bool = policy.CLOUDWATCH_LOGS) -> dict:
    """How DMS behaves while it runs.

    The values that matter, and why:

    FullLobMode           a LOB is copied in one pass with a declared ceiling
                          rather than streamed, because streaming needs a second
                          pass per row and is far slower. See LOB_MAX_KB.
    TargetTablePrepMode   DO_NOTHING -- never destroy target data. See policy.
    StopTaskCachedChangesApplied / NotApplied
                          false for full-load-and-cdc: the task keeps replicating
                          after the load, which is the whole point. Stopping is a
                          cutover decision a person makes.
    """
    settings = {
        "TargetMetadata": {
            "TargetSchema": "",
            "SupportLobs": True,
            # Limited LOB mode: one pass, with a ceiling. Anything past the
            # ceiling is truncated, which is why the ceiling is generous and
            # Phase 8 compares LOB columns explicitly.
            "FullLobMode": False,
            "LobChunkSize": 64,
            "LimitedSizeLobMode": True,
            "LobMaxSize": policy.LOB_MAX_KB,
            "LoadMaxFileSize": 0,
            "ParallelLoadThreads": 0,
            "BatchApplyEnabled": False,
        },
        "FullLoadSettings": {
            "TargetTablePrepMode": policy.TARGET_TABLE_PREP,
            "CreatePkAfterFullLoad": False,
            "StopTaskCachedChangesApplied": False,
            "StopTaskCachedChangesNotApplied": False,
            "MaxFullLoadSubTasks": 8,
            "TransactionConsistencyTimeout": 600,
            "CommitRate": 10000,
        },
        "Logging": {
            "EnableLogging": cloudwatch,
            "LogComponents": [
                # SOURCE_UNLOAD and TARGET_APPLY at DEFAULT are what explain a
                # suspended table. Anything more verbose fills CloudWatch fast.
                {"Id": "SOURCE_UNLOAD", "Severity": "LOGGER_SEVERITY_DEFAULT"},
                {"Id": "TARGET_LOAD", "Severity": "LOGGER_SEVERITY_DEFAULT"},
                {"Id": "SOURCE_CAPTURE", "Severity": "LOGGER_SEVERITY_DEFAULT"},
                {"Id": "TARGET_APPLY", "Severity": "LOGGER_SEVERITY_DEFAULT"},
                {"Id": "TASK_MANAGER", "Severity": "LOGGER_SEVERITY_DEFAULT"},
            ],
        },
        "ErrorBehavior": {
            # A table that fails to load is suspended, not silently skipped, and
            # the task carries on with the rest. Phase 8 will not validate a
            # suspended table as matching, so a failure cannot pass unnoticed.
            "DataErrorPolicy": "LOG_ERROR",
            "TableErrorPolicy": "SUSPEND_TABLE",
            "TableErrorEscalationPolicy": "STOP_TASK",
            "TableErrorEscalationCount": 0,
            "RecoverableErrorCount": -1,
            "RecoverableErrorInterval": 5,
            "RecoverableErrorThrottling": True,
            "RecoverableErrorThrottlingMax": 1800,
            "ApplyErrorDeletePolicy": "IGNORE_RECORD",
            "ApplyErrorInsertPolicy": "LOG_ERROR",
            "ApplyErrorUpdatePolicy": "LOG_ERROR",
            "ApplyErrorEscalationPolicy": "LOG_ERROR",
            "ApplyErrorEscalationCount": 0,
            "FullLoadIgnoreConflicts": False,
        },
        "ValidationSettings": {
            # DMS's own row validation, which is not a substitute for Phase 8 --
            # it compares as it replicates and cannot see what it never selected.
            # Cheap to leave on, and it catches an apply failure early.
            "EnableValidation": True,
            "ValidationMode": "ROW_LEVEL",
            "ThreadCount": 5,
            "FailureMaxCount": 10000,
            "HandleCollationDiff": False,
        },
        "ControlTablesSettings": {
            # DMS writes its own bookkeeping tables. Put them in their own schema
            # so they never appear in the migrated schema and confuse validation.
            "ControlSchema": "awsdms_control",
            "HistoryTimeslotInMinutes": 5,
            "StatusTableEnabled": True,
            "SuspendedTablesTableEnabled": True,
            "HistoryTableEnabled": True,
        },
    }

    if migration_type == policy.CDC_ONLY:
        # A CDC-only task has no full load to configure, and leaving the section
        # in with a prep mode would be misleading about what it does.
        settings.pop("FullLoadSettings")

    return settings


def excluded_objects(objects: list[dict], schema: str) -> list[dict]:
    """What DMS will not move, named rather than silently absent.

    This is shown to the user, because "DMS migrated the database" is not true
    and the difference is exactly what Phase 4b and the runbook exist to cover.
    """
    out: list[dict] = []
    for o in objects or []:
        if o.get("owner") != schema:
            continue
        kind = (o.get("object_type") or "").upper()
        if kind in NOT_MOVED_BY_DMS:
            out.append({"object_type": kind, "object_name": o.get("object_name"),
                        "why": NOT_MOVED_BY_DMS[kind]})
    return sorted(out, key=lambda x: (x["object_type"], x["object_name"] or ""))
