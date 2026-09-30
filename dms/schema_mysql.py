"""The schema for MySQL -> RDS for MySQL, copied from the source itself.

    python -m dms.schema_mysql --plan                         # read the source, write the plan
    python -m dms.schema_mysql --apply pre  --approved-by you@x --target-dsn host:3306
    python -m dms.schema_mysql --apply post --approved-by you@x --target-dsn host:3306

**Why this exists.** DMS moves rows. With `TargetTablePrepMode=DO_NOTHING` it
creates nothing, and even when told to create tables it makes bare InnoDB tables
with no secondary indexes, foreign keys, views, routines, triggers or events.
On the heterogeneous path Phase 4c builds the schema; on the homogeneous path
there is nothing to convert, so the schema is the source's own DDL -- read here
with SHOW CREATE, the same statements `mysqldump --no-data` would produce,
without depending on a client binary.

**Three things the plain dump gets wrong on RDS, each handled here:**

  1. `DEFINER=`root`@`localhost`` on every view, routine, trigger and event.
     Creating an object for another definer needs SUPER or SET_USER_ID, which
     RDS never grants, so the statement fails with ERROR 1227. The clause is
     removed and the object is owned by the applying user; SQL SECURITY is kept.
  2. **Triggers must not exist during the load.** `trg_order_audit_upd` inserts
     an audit row on every status change; DMS replaying the source's updates
     would fire it a second time for rows whose audit entries are ALSO being
     copied. Triggers are created after the full load.
  3. **Events must not run during the migration.** With event_scheduler ON (it
     is, carried from the source), `ev_purge_old_audit` would start purging the
     target mid-load. Events are created DISABLED after the load and enabled at
     cutover -- a separate, explicit statement.

A MyISAM table becomes InnoDB, with a note: RDS backups and point-in-time
restore are only crash-consistent for InnoDB, and DMS itself defaults to it.

Nothing is applied without `--approved-by`; the plan is written either way.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "dms"

OUTPUT = Path(__file__).resolve().parent / "output"
PLAN = OUTPUT / "mysql_schema_plan.json"

# `DEFINER=`user`@`host`` or DEFINER=user@host, with any quoting.
_DEFINER = re.compile(r"\s+DEFINER\s*=\s*(`[^`]*`|'[^']*'|\w+)@(`[^`]*`|'[^']*'|[\w.%-]+)", re.I)
_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_$]+$")


def strip_definer(ddl: str) -> tuple[str, bool]:
    out, n = _DEFINER.subn("", ddl, count=1)
    return out, bool(n)


def _q(name: str) -> str:
    if not _SAFE_IDENT.match(name or ""):
        raise ValueError(f"refusing an unsafe identifier: {name!r}")
    return f"`{name}`"


def _rows(cur, sql, args=None):
    cur.execute(sql, args)
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def extract(conn, schema: str) -> dict:
    """Read the source's DDL. Read-only: SHOW CREATE and information_schema."""
    cur = conn.cursor()
    notes: list[str] = []
    s = _q(schema)

    db = _rows(cur, "SELECT default_character_set_name AS cs, default_collation_name AS co "
                    "FROM information_schema.schemata WHERE schema_name = %s", (schema,))
    if not db:
        raise ValueError(f"schema {schema} not found on the source")
    database = (f"CREATE DATABASE IF NOT EXISTS {s} CHARACTER SET {db[0]['cs']} "
                f"COLLATE {db[0]['co']}")

    tables = _rows(cur, "SELECT table_name AS name, engine FROM information_schema.tables "
                        "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
                   (schema,))
    table_ddl = []
    for t in tables:
        cur.execute(f"SHOW CREATE TABLE {s}.{_q(t['name'])}")
        ddl = cur.fetchone()[1]
        if (t.get("engine") or "").upper() != "INNODB":
            ddl = re.sub(r"ENGINE\s*=\s*\w+", "ENGINE=InnoDB", ddl, count=1, flags=re.I)
            notes.append(f"{t['name']}: {t.get('engine')} -> InnoDB. RDS backups and point-in-time "
                         "restore are crash-consistent only for InnoDB; this also gives the table "
                         "transactions and row locks it did not have on the source.")
        table_ddl.append({"kind": "TABLE", "name": t["name"], "sql": ddl})

    views = _rows(cur, "SELECT table_name AS name FROM information_schema.views "
                       "WHERE table_schema = %s ORDER BY table_name", (schema,))
    view_ddl = []
    for v in views:
        cur.execute(f"SHOW CREATE VIEW {s}.{_q(v['name'])}")
        ddl, had = strip_definer(cur.fetchone()[1])
        view_ddl.append({"kind": "VIEW", "name": v["name"], "sql": ddl, "definer_removed": had})
    # A view over another view must come after it. SHOW CREATE qualifies every
    # reference as `schema`.`name`, so the dependency is a plain text match.
    view_ddl = _order_views(view_ddl, schema)

    routine_ddl = []
    for r in _rows(cur, "SELECT routine_name AS name, routine_type AS type FROM "
                        "information_schema.routines WHERE routine_schema = %s "
                        "ORDER BY routine_type, routine_name", (schema,)):
        cur.execute(f"SHOW CREATE {r['type']} {s}.{_q(r['name'])}")
        row = cur.fetchone()
        body = row[2] if len(row) > 2 else None
        if not body:
            raise ValueError(f"SHOW CREATE {r['type']} {r['name']} returned no text: the "
                             "account needs SHOW_ROUTINE (or SELECT on mysql.proc before 8.0)")
        ddl, had = strip_definer(body)
        routine_ddl.append({"kind": r["type"], "name": r["name"], "sql": ddl, "definer_removed": had})

    trigger_ddl = []
    for t in _rows(cur, "SELECT trigger_name AS name FROM information_schema.triggers "
                        "WHERE trigger_schema = %s ORDER BY trigger_name", (schema,)):
        cur.execute(f"SHOW CREATE TRIGGER {s}.{_q(t['name'])}")
        ddl, had = strip_definer(cur.fetchone()[2])
        trigger_ddl.append({"kind": "TRIGGER", "name": t["name"], "sql": ddl, "definer_removed": had})

    event_ddl, enable = [], []
    for e in _rows(cur, "SELECT event_name AS name, status FROM information_schema.events "
                        "WHERE event_schema = %s ORDER BY event_name", (schema,)):
        cur.execute(f"SHOW CREATE EVENT {s}.{_q(e['name'])}")
        ddl, had = strip_definer(cur.fetchone()[3])
        # Created DISABLED whatever the source says; enabled at cutover only if
        # it was enabled on the source.
        ddl = re.sub(r"\bON\s+COMPLETION\s+(NOT\s+)?PRESERVE\s+(ENABLE|DISABLE(\s+ON\s+(SLAVE|REPLICA))?)\b",
                     lambda m: m.group(0).rsplit(" ", 1)[0] + " DISABLE"
                     if m.group(2).upper() == "ENABLE" else m.group(0), ddl, flags=re.I)
        if not re.search(r"\bDISABLE\b", ddl, re.I):
            ddl = re.sub(r"\s+DO\s", " DISABLE DO ", ddl, count=1, flags=re.I)
        event_ddl.append({"kind": "EVENT", "name": e["name"], "sql": ddl, "definer_removed": had})
        if (e.get("status") or "").upper() == "ENABLED":
            enable.append({"kind": "EVENT", "name": e["name"],
                           "sql": f"ALTER EVENT {s}.{_q(e['name'])} ENABLE"})

    removed = sum(1 for x in view_ddl + routine_ddl + trigger_ddl + event_ddl if x["definer_removed"])
    if removed:
        notes.append(f"DEFINER removed from {removed} object(s): creating an object for another "
                     "definer needs SUPER or SET_USER_ID, which RDS does not grant (ERROR 1227). "
                     "They are owned by the applying user; SQL SECURITY is unchanged.")
    if trigger_ddl:
        notes.append(f"{len(trigger_ddl)} trigger(s) are created AFTER the load: DMS replays the "
                     "source's writes, and a trigger present during the load would fire again "
                     "for rows whose effects are already being copied.")
    if event_ddl:
        notes.append(f"{len(event_ddl)} event(s) are created DISABLED after the load and enabled "
                     "at cutover; with event_scheduler ON they would otherwise run on the target "
                     "mid-migration.")

    return {
        "schema": schema,
        "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": database,
        # Tables first, then routines (a view may call a function), then views.
        "pre_load": table_ddl + routine_ddl + view_ddl,
        "post_load": trigger_ddl + event_ddl,
        "at_cutover": enable,
        "counts": {"tables": len(table_ddl), "views": len(view_ddl),
                   "routines": len(routine_ddl), "triggers": len(trigger_ddl),
                   "events": len(event_ddl)},
        "notes": notes,
        "applied": False,
    }


def _order_views(views: list[dict], schema: str) -> list[dict]:
    names = {v["name"] for v in views}
    ordered, placed = [], set()
    pending = list(views)
    while pending:
        progressed = False
        for v in list(pending):
            deps = {n for n in names if n != v["name"] and f"`{schema}`.`{n}`" in v["sql"]}
            if deps <= placed:
                ordered.append(v)
                placed.add(v["name"])
                pending.remove(v)
                progressed = True
        if not progressed:        # a cycle cannot exist in MySQL; keep what remains in order
            ordered += pending
            break
    return ordered


def apply(conn, plan: dict, stage: str, *, approved_by: str) -> dict:
    """Run one stage on the target. Stops at the first failure and says which.

    `pre` creates the database and everything the load needs; `post` the
    triggers and (disabled) events; `cutover` enables the events.
    """
    if not approved_by or "@" not in approved_by:
        raise PermissionError("applying schema to a target needs --approved-by <email>")
    stmts = {"pre": [{"kind": "DATABASE", "name": plan["schema"], "sql": plan["database"]}]
                    + plan["pre_load"],
             "post": plan["post_load"], "cutover": plan["at_cutover"]}[stage]
    cur = conn.cursor()
    # The load order does not follow foreign keys, and neither does this list:
    # the tables reference each other, so checks are off for this session only.
    cur.execute("SET SESSION foreign_key_checks = 0")
    cur.execute(f"USE {_q(plan['schema'])}") if stage != "pre" else None
    done, failed = [], None
    for i, st in enumerate(stmts):
        try:
            if st["kind"] != "DATABASE" and stage == "pre" and i == 1:
                cur.execute(f"USE {_q(plan['schema'])}")
            cur.execute(st["sql"])
            done.append(f"{st['kind']} {st['name']}")
        except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
            failed = {"object": f"{st['kind']} {st['name']}", "error": str(exc)[:300]}
            break
    conn.commit()
    return {"stage": stage, "approved_by": approved_by, "applied": done, "failed": failed,
            "ok": failed is None, "at_utc": datetime.now(timezone.utc).isoformat()}


def _connect(host, port, user, password, database=None):
    import pymysql
    return pymysql.connect(host=host, port=int(port), user=user, password=password,
                           database=database, connect_timeout=15, charset="utf8mb4",
                           autocommit=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", action="store_true", help="read the source and write the plan")
    ap.add_argument("--apply", choices=["pre", "post", "cutover"])
    ap.add_argument("--approved-by", default="")
    ap.add_argument("--schema", default="dbmig_mysql_app")
    ap.add_argument("--source-dsn", default=os.environ.get("DBSHIFT_MYSQL_DSN", "3.108.190.1:3306"))
    ap.add_argument("--source-user", default="dbmig_collector")
    ap.add_argument("--target-dsn", default=None, help="host:port of the target")
    ap.add_argument("--target-user", default="dbshiftadm")
    args = ap.parse_args(argv)

    if args.plan or not PLAN.exists():
        host, port = args.source_dsn.split("/")[0].split(":")
        pw = os.environ.get("DBSHIFT_MYSQL_PASSWORD")
        if not pw:
            print("set DBSHIFT_MYSQL_PASSWORD for the source account", file=sys.stderr)
            return 2
        conn = _connect(host, port, args.source_user, pw)
        try:
            plan = extract(conn, args.schema)
        finally:
            conn.close()
        OUTPUT.mkdir(parents=True, exist_ok=True)
        PLAN.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        c = plan["counts"]
        print(f"{args.schema}: {c['tables']} tables, {c['routines']} routines, {c['views']} views "
              f"before the load; {c['triggers']} triggers, {c['events']} events after it")
        for n in plan["notes"]:
            print("  - " + n)
        print(f"plan: {PLAN}")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))

    if args.apply:
        if not args.target_dsn:
            print("--apply needs --target-dsn", file=sys.stderr)
            return 2
        host, port = args.target_dsn.split("/")[0].split(":")
        pw = os.environ.get("DBSHIFT_TARGET_PASSWORD")
        if not pw:
            print("set DBSHIFT_TARGET_PASSWORD for the target account", file=sys.stderr)
            return 2
        conn = _connect(host, port, args.target_user, pw)
        try:
            result = apply(conn, plan, args.apply, approved_by=args.approved_by)
        finally:
            conn.close()
        plan.setdefault("applications", []).append(result)
        plan["applied"] = True
        PLAN.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        print(f"{args.apply}: {len(result['applied'])} statement(s) applied"
              + (f"; FAILED at {result['failed']['object']}: {result['failed']['error']}"
                 if result["failed"] else ""))
        return 0 if result["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
