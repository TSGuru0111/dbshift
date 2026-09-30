"""The Connect preflight for a MySQL source -- the sibling of `web/preflight.py`.

Six checks in the same shape and the same order as Oracle's, because the console
renders them from one template and an operator comparing two engines should not
have to learn two layouts:

    1 Network reachable      TCP to host:port
    2 Authentication         the account can log in
    3 Database selected      what the DSN's third field named, if any
    4 Catalogue access       information_schema returns the estate's objects
    5 Row data access        a real SELECT against a real table
    6 CDC readiness          binlog_format, binlog_row_image, REPLICATION CLIENT

**Where MySQL differs from Oracle, and it matters for every check below.** Oracle
refuses a query outright when a grant is missing -- `ORA-00942`, and you know. MySQL's
`information_schema` is readable by anyone and simply **shows fewer rows**. So a
missing grant does not look like an error, it looks like a smaller estate, and a
preflight that only catches exceptions would pass on an account that can see
almost nothing. Checks 4 and 5 therefore compare what was found against what the
account should be able to find, and say which grant is missing by name.

Measured against MySQL 8.0.46, 2026-09-29. The privilege table in
`docs/19-mysql-source.md` is the evidence for the remedies here.
"""

from __future__ import annotations

import socket
import time

from collector.dialect import parse_mysql_dsn

# Schemas the server owns. Listed rather than pattern-matched, because a user
# schema legitimately called `mysql_reports` must not be excluded.
SYSTEM_SCHEMAS = ("information_schema", "performance_schema", "mysql", "sys")


def _check(name, status, detail, remedy=None):
    row = {"name": name, "status": status, "detail": detail}
    if remedy:
        row["remedy"] = remedy
    return row


def parse_dsn(dsn: str) -> tuple[str, int]:
    """host, port -- the two fields check 1 needs."""
    host, port, _db = parse_mysql_dsn(dsn)
    return host, port


def run(dsn: str, user: str, password: str, schema: str = "") -> dict:
    """`schema` may be blank, one name, or a comma-separated list.

    **Not upper-cased.** A MySQL schema is a directory on disk, so on Linux
    `Sales` and `SALES` are different databases -- folding the name here would
    send the preflight looking for one that does not exist and report it missing.
    Oracle's equivalent does fold, because Oracle folded it first.
    """
    import pymysql

    checks: list[dict] = []
    facts: dict = {}
    host, port, database = parse_mysql_dsn(dsn)
    requested = [s.strip() for s in (schema or "").split(",") if s.strip()]

    # 1 -- TCP reachability. Distinguishes "wrong password" from "cannot get
    # there at all", which at a client site is a DBA ticket versus a network one.
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=5):
            latency = int((time.perf_counter() - started) * 1000)
        checks.append(_check("Network reachable", "pass",
                             f"{host}:{port} answered in {latency} ms"))
    except OSError as exc:
        checks.append(_check(
            "Network reachable", "fail",
            f"cannot reach {host}:{port} -- {exc}",
            "Open TCP 3306 from this address to the source. On EC2 that is a "
            "security-group rule; bind-address must not be 127.0.0.1.",
        ))
        return {"ok": False, "checks": checks, "facts": facts, "schemas": []}

    # 2 -- authentication.
    conn = None
    try:
        conn = pymysql.connect(host=host, port=port, user=user, password=password,
                               database=database or None, charset="utf8mb4",
                               autocommit=True, connect_timeout=10)
        checks.append(_check("Authentication", "pass", f"connected as {user}"))
    except pymysql.Error as exc:
        code = exc.args[0] if exc.args else 0
        remedy = {
            1045: "Wrong user or password, or the account is not allowed from this "
                  "host. MySQL grants are per user@host: an account created as "
                  "'u'@'localhost' cannot connect from anywhere else.",
            1049: f"No database named {database!r}. The third DSN field is a "
                  "default database and is optional -- host:port alone is valid.",
            2003: "The server refused the connection. Check bind-address and that "
                  "mysqld is running.",
            1044: "The account exists but has no access to that database.",
        }.get(code)
        checks.append(_check("Authentication", "fail",
                            f"{code}: {str(exc.args[-1])[:120]}", remedy))
        return {"ok": False, "checks": checks, "facts": facts, "schemas": []}

    try:
        with conn.cursor() as cur:
            # 3 -- which database the connection landed in. Not an error when
            # none: the schema filter is what scopes a run, and the collector
            # reads information_schema across several schemas anyway.
            cur.execute("SELECT DATABASE(), VERSION(), @@hostname")
            row = cur.fetchone() or (None, None, None)
            current_db, version, hostname = row
            facts["version"] = version
            facts["container"] = None          # MySQL has no container concept
            facts["host_name"] = hostname
            if current_db:
                checks.append(_check("Database selected", "pass",
                                     f"default database is {current_db}"))
            else:
                checks.append(_check(
                    "Database selected", "warn",
                    "the DSN named no database; the schema list decides what is read",
                ))

            # 4 -- catalogue access. The trap: this NEVER raises for a missing
            # grant, it returns fewer rows. So the check is whether the requested
            # schemas were actually found, and whether the objects inside them are
            # visible -- not whether the query succeeded.
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN (%s, %s, %s, %s) ORDER BY schema_name",
                SYSTEM_SCHEMAS,
            )
            present = [r[0] for r in cur.fetchall()]
            missing = [s for s in requested if s not in present]
            if missing:
                checks.append(_check(
                    "Catalogue access", "fail",
                    f"requested schema(s) not present: {', '.join(missing)}",
                    "Check the spelling and the case. MySQL schema names are "
                    "case-sensitive on Linux, so 'Sales' and 'SALES' are "
                    f"different databases. Present: {', '.join(present) or 'none'}.",
                ))
            else:
                checks.append(_check(
                    "Catalogue access", "pass",
                    f"{len(present)} user schema(s) visible: "
                    + (", ".join(present[:6]) + ("..." if len(present) > 6 else ""))))

            scope = requested or present
            counts = _object_counts(cur, scope)
            facts.update(counts)
            # **`facts["schemas_selected"]` is the contract**, matching
            # web/preflight.py. `web/server.py` reads exactly that key to set
            # STATE.schemas; returning the list under a top-level "schemas" key
            # instead left STATE.schemas empty, so the console fell back to
            # `collector_config.DEFAULT_SCHEMAS` -- Oracle's DBMIG_APP,
            # DBMIG_RPT, DBMIG_COLLECTOR -- against a MySQL server.
            #
            # The collector then correctly reported all three missing and
            # collected 0 tables, and the run LOOKED successful: a manifest, no
            # failed queries, 748 rows of server settings. The only sign was
            # `v_user_tables` having no columns three phases later.
            facts["schemas_selected"] = list(scope)

            # Routines, triggers and events are the ones that come back EMPTY
            # rather than denied. Reported as a grant question, because that is
            # what "0 routines" almost always means.
            if scope and counts["tables"] and not counts["routines"] \
                    and not counts["triggers"] and not counts["events"]:
                checks.append(_check(
                    "Stored code visible", "warn",
                    "tables are visible but no routines, triggers or events are",
                    "This is usually a missing grant rather than an estate with no "
                    "stored code: information_schema returns ZERO ROWS, with no "
                    "error, without EXECUTE, TRIGGER and EVENT. Phase 4b also needs "
                    "SHOW_ROUTINE (ON *.*) or routine bodies come back NULL.",
                ))
            elif scope:
                checks.append(_check(
                    "Stored code visible", "pass",
                    f"{counts['routines']} routine(s), {counts['triggers']} trigger(s), "
                    f"{counts['events']} event(s)"))

            # 5 -- row data. A real SELECT against a real table: the catalogue
            # being readable says nothing about SELECT on the data.
            checks.append(_row_access(cur, scope))

            # 6 -- CDC readiness, from the server's own settings.
            checks.append(_cdc(cur, facts))
    finally:
        try:
            conn.close()
        except Exception:      # noqa: BLE001 -- closing a dead socket is not news
            pass

    ok = not any(c["status"] == "fail" for c in checks)
    return {"ok": ok, "checks": checks, "facts": facts,
            "schemas": requested or facts.get("user_schemas", [])}


def _object_counts(cur, scope: list[str]) -> dict:
    """What the account can actually see, per object class."""
    out = {"tables": 0, "routines": 0, "triggers": 0, "events": 0,
           "views": 0, "user_schemas": list(scope), "segments": 0, "size_gb": None}
    if not scope:
        return out
    marks = ", ".join(["%s"] * len(scope))
    pairs = (
        ("tables", f"SELECT COUNT(*) FROM information_schema.tables "
                   f"WHERE table_schema IN ({marks}) AND table_type = 'BASE TABLE'"),
        ("views", f"SELECT COUNT(*) FROM information_schema.views "
                  f"WHERE table_schema IN ({marks})"),
        ("routines", f"SELECT COUNT(*) FROM information_schema.routines "
                     f"WHERE routine_schema IN ({marks})"),
        ("triggers", f"SELECT COUNT(*) FROM information_schema.triggers "
                     f"WHERE trigger_schema IN ({marks})"),
        ("events", f"SELECT COUNT(*) FROM information_schema.events "
                   f"WHERE event_schema IN ({marks})"),
    )
    for key, sql in pairs:
        try:
            cur.execute(sql, tuple(scope))
            out[key] = int((cur.fetchone() or [0])[0])
        except Exception:      # noqa: BLE001 -- a missing view is a fact, not a crash
            out[key] = 0
    try:
        cur.execute(
            f"SELECT COUNT(*), SUM(IFNULL(data_length,0) + IFNULL(index_length,0)) "
            f"FROM information_schema.tables WHERE table_schema IN ({marks})",
            tuple(scope))
        n, total = cur.fetchone() or (0, 0)
        out["segments"] = int(n or 0)
        out["size_gb"] = round(int(total or 0) / (1024 ** 3), 2)
    except Exception:          # noqa: BLE001
        pass
    return out


def _row_access(cur, scope: list[str]) -> dict:
    """One real SELECT. Catalogue access is not data access."""
    if not scope:
        return _check("Row data access", "warn", "no schema in scope to sample")
    marks = ", ".join(["%s"] * len(scope))
    try:
        cur.execute(
            f"SELECT table_schema, table_name FROM information_schema.tables "
            f"WHERE table_schema IN ({marks}) AND table_type = 'BASE TABLE' "
            f"ORDER BY table_rows DESC LIMIT 1", tuple(scope))
        row = cur.fetchone()
        if not row:
            return _check("Row data access", "warn", "no base table to sample")
        sch, tbl = row
        # Identifiers cannot be bound in any engine, so they are quoted and the
        # names came from the catalogue rather than from user input.
        cur.execute(f"SELECT COUNT(*) FROM `{sch}`.`{tbl}`")
        n = int((cur.fetchone() or [0])[0])
        return _check("Row data access", "pass",
                      f"read {n} row(s) from {sch}.{tbl}")
    except Exception as exc:   # noqa: BLE001
        return _check(
            "Row data access", "fail", str(exc)[:140],
            "The account can read the catalogue but not the data. Grant SELECT on "
            "the profiled schemas -- and note AWS SCT additionally requires SELECT "
            "and SHOW VIEW at SERVER scope (ON *.*), which schema-scoped grants do "
            "not satisfy.",
        )


def _cdc(cur, facts: dict) -> dict:
    """Is the source configured for change data capture, on its own evidence?

    Reported as facts plus a verdict, never as a decision. A client may declare
    CDC against a server that is not ready -- that is a remediation task with a
    restart attached, not a reason to refuse the declaration. The same division
    of labour `collector/mode.py` documents for Oracle.
    """
    try:
        cur.execute("SELECT @@log_bin, @@binlog_format, @@binlog_row_image")
        log_bin, fmt, image = cur.fetchone() or (None, None, None)
    except Exception as exc:   # noqa: BLE001
        return _check("CDC readiness", "warn", f"could not read binlog settings: {exc}"[:140])

    on = str(log_bin) in ("1", "ON", "on", "True")
    row_fmt = str(fmt or "").upper() == "ROW"
    full = str(image or "").upper() == "FULL"

    # Mapped onto Oracle's vocabulary so mode.readiness(), the gate and the
    # console's existing Log-mode row work with no MySQL branch. The native
    # readings stay alongside, unaltered.
    facts["log_mode"] = "ARCHIVELOG" if on else "NOARCHIVELOG"
    facts["supplemental_logging"] = "YES" if (on and row_fmt and full) else "NO"
    facts["binlog_format"] = fmt
    facts["binlog_row_image"] = image

    # Does the account hold REPLICATION CLIENT? SHOW MASTER STATUS is the check
    # DMS itself would fail on.
    repl = True
    try:
        cur.execute("SHOW MASTER STATUS")
        cur.fetchall()
    except Exception:          # noqa: BLE001
        repl = False
    facts["replication_client"] = repl

    unmet = []
    if not on:
        unmet.append("log_bin is off (enabling it needs a restart)")
    if not row_fmt:
        unmet.append(f"binlog_format is {fmt}, not ROW")
    if not full:
        unmet.append(f"binlog_row_image is {image}, not FULL")
    if not repl:
        unmet.append("the account lacks REPLICATION CLIENT")

    if not unmet:
        return _check("CDC readiness", "pass",
                      "log_bin on, binlog_format=ROW, binlog_row_image=FULL")
    return _check(
        "CDC readiness", "warn", "; ".join(unmet),
        "A full load into an outage window needs none of this and is unaffected. "
        "For change data capture, set binlog_format=ROW and binlog_row_image=FULL "
        "(no restart) and enable log_bin (restart). GRANT REPLICATION CLIENT ON *.* "
        "for the position read.",
    )


# What a client environment has to provide. Rendered on Connect beside Oracle's,
# because the first item blocks everything else at a customer site.
NETWORK_REQUIREMENTS = [
    {"title": "TCP 3306 from the migration host to the source",
     "detail": "DMS and this console are both clients: they open the connection, "
               "the source never dials out. On EC2 this is a security-group rule "
               "and bind-address must not be 127.0.0.1."},
    {"title": "A read-only account, reachable from the right host",
     "detail": "MySQL grants are per user@host, so an account created as "
               "'u'@'localhost' cannot connect from anywhere else. Needs SELECT, "
               "SHOW VIEW, EXECUTE, TRIGGER and EVENT on the schemas, plus PROCESS, "
               "REPLICATION CLIENT and SHOW_ROUTINE on *.*."},
    {"title": "SELECT and SHOW VIEW at server scope, for AWS SCT",
     "detail": "SCT checks these ON *.* and refuses to connect without them, even "
               "when the schema-scoped grants are in place. Measured against SCT "
               "1.0.677."},
    {"title": "sql_mode without ONLY_FULL_GROUP_BY, for AWS SCT",
     "detail": "SCT's own partition-metadata query is invalid under it and the "
               "assessment abandons after three retries. It is in MySQL 8's "
               "default sql_mode, so a stock server cannot be assessed until it is "
               "relaxed."},
    {"title": "binlog_format=ROW and binlog_row_image=FULL, for CDC only",
     "detail": "Only needed for a low-downtime cutover. A full load reads tables "
               "directly. Enabling log_bin itself needs a server restart."},
]
