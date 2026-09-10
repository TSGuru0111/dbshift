"""Gate 4 -- prove a fix on a copy before it goes near production.

Applies the fix, then applies its rollback, and requires both to succeed. A fix
whose rollback fails is worse than no fix, and this is the only place that can
be found out safely.

Nothing here ever touches the source. The connection is to the rehearsal target
only, and the harness refuses to run if the schema it is about to modify is the
one the estate lives in.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import oracledb


class RehearsalError(RuntimeError):
    pass


@dataclass
class RehearsalTarget:
    dsn: str
    user: str
    password: str
    schema: str          # the schema holding the restored copy
    source_schema: str   # the schema the fix was written against

    @classmethod
    def from_env(cls, dsn: str | None = None) -> "RehearsalTarget | None":
        dsn = dsn or os.environ.get("DBSHIFT_REHEARSAL_DSN")
        password = os.environ.get("DBSHIFT_REHEARSAL_PASSWORD")
        if not dsn or not password:
            return None
        return cls(
            dsn=dsn,
            user=os.environ.get("DBSHIFT_REHEARSAL_USER", "dbmig_rehearsal"),
            password=password,
            schema=os.environ.get("DBSHIFT_REHEARSAL_SCHEMA", "DBMIG_REHEARSAL"),
            source_schema=os.environ.get("DBSHIFT_PRIMARY_SCHEMA", "DBMIG_APP"),
        )


def remap(sql: str, source_schema: str, rehearsal_schema: str) -> str:
    """Point a fix at the rehearsal copy instead of the source.

    Templates emit fully-qualified quoted identifiers (`"OWNER"."OBJECT"`), and
    DBMS_STATS calls carry the owner as a quoted string, so both forms are
    rewritten. The result is then checked to confirm the source schema is gone --
    a partial rewrite would run half the statement against production.
    """
    if source_schema.upper() == rehearsal_schema.upper():
        raise RehearsalError(
            "rehearsal schema is the same as the source schema; refusing to run"
        )

    out = re.sub(rf'"{re.escape(source_schema)}"', f'"{rehearsal_schema}"', sql, flags=re.IGNORECASE)
    out = re.sub(rf"'{re.escape(source_schema)}'", f"'{rehearsal_schema}'", out, flags=re.IGNORECASE)

    if re.search(rf"\b{re.escape(source_schema)}\b", out, re.IGNORECASE):
        raise RehearsalError(
            f"could not fully remap the statement off {source_schema} -- it still references it. "
            "Refusing rather than running a partially-remapped statement."
        )
    return out


def _execute(cur, sql: str) -> int:
    started = time.perf_counter()
    cur.execute(sql)
    return int((time.perf_counter() - started) * 1000)


def dry_run(fix: dict, target: RehearsalTarget) -> dict:
    """Apply, then roll back. Both must succeed."""
    try:
        fix_sql = remap(fix["sql"], target.source_schema, target.schema)
        rollback_sql = remap(fix["rollback_sql"], target.source_schema, target.schema)
    except RehearsalError as exc:
        return {"ok": False, "stage": "remap", "detail": str(exc), "dirty": False}

    try:
        conn = oracledb.connect(user=target.user, password=target.password, dsn=target.dsn)
    except oracledb.Error as exc:
        return {
            "ok": False,
            "stage": "connect",
            "detail": str(exc).splitlines()[0],
            "dirty": False,
        }

    applied_ms = rollback_ms = None
    try:
        cur = conn.cursor()
        try:
            applied_ms = _execute(cur, fix_sql)
        except oracledb.Error as exc:
            return {
                "ok": False,
                "stage": "apply",
                "detail": str(exc).splitlines()[0],
                "dirty": False,          # it did not apply, so nothing to undo
                "sql": fix_sql,
            }

        try:
            rollback_ms = _execute(cur, rollback_sql)
        except oracledb.Error as exc:
            # The serious case. The fix applied and its rollback did not, so the
            # rehearsal copy is now changed and the rollback is proven wrong.
            return {
                "ok": False,
                "stage": "rollback",
                "detail": str(exc).splitlines()[0],
                "dirty": True,
                "sql": fix_sql,
                "rollback_sql": rollback_sql,
                "applied_ms": applied_ms,
            }
    finally:
        conn.close()

    return {
        "ok": True,
        "stage": "complete",
        "detail": (
            f"applied in {applied_ms} ms and rolled back in {rollback_ms} ms "
            f"on {target.schema}"
        ),
        "dirty": False,
        "sql": fix_sql,
        "rollback_sql": rollback_sql,
        "applied_ms": applied_ms,
        "rollback_ms": rollback_ms,
    }


def check_target(target: RehearsalTarget) -> dict:
    """Confirm the rehearsal copy exists and looks like the source before trusting it."""
    try:
        conn = oracledb.connect(user=target.user, password=target.password, dsn=target.dsn)
    except oracledb.Error as exc:
        return {"ok": False, "detail": str(exc).splitlines()[0]}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM all_objects WHERE owner = :o", {"o": target.schema.upper()}
        )
        objects = cur.fetchone()[0]
        cur.close()
    except oracledb.Error as exc:
        conn.close()
        return {"ok": False, "detail": str(exc).splitlines()[0]}
    conn.close()

    if objects == 0:
        return {
            "ok": False,
            "detail": f"{target.schema} exists but owns no objects -- the copy was not imported",
        }
    return {"ok": True, "detail": f"{target.schema} holds {objects} objects", "objects": objects}
