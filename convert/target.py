"""The PostgreSQL compile target, and the shadow schema it needs.

PostgreSQL DDL is transactional, which Oracle's is not. That single property
is what makes this gate safe: every converted object is created inside one
transaction that is rolled back at the end, so the target is left exactly as it
was found -- and, unlike Phase 4's syntax gate, a real parse is possible here
without applying anything.

What creating a PL/pgSQL function actually proves, and what it does not: the
body is parsed and every declaration (including %TYPE) is resolved, but SQL
statements inside the body are not planned until the function runs. The gate
says so in its detail rather than claiming more. If the plpgsql_check extension
is available on the target it is used as well, and that does check the
embedded SQL.

The shadow schema exists because %TYPE and trigger bindings need the tables to
exist. It is built from discovery's column catalogue for the tables the
converted code references -- columns and NOT NULL only, no data, no
constraints -- and it is not the migrated schema, only enough of one to
compile against."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import inventory, policy, typemap
from .typemap import Unmappable


@dataclass
class PgTarget:
    host: str
    port: int
    database: str
    user: str
    password: str

    @classmethod
    def parse(cls, dsn: str, user: str, password: str) -> "PgTarget":
        m = re.fullmatch(r"([^:/]+)(?::(\d+))?/(\w+)", (dsn or "").strip())
        if not m:
            raise ValueError(f"a PostgreSQL DSN looks like host:5432/dbname, not {dsn!r}")
        return cls(m.group(1), int(m.group(2) or 5432), m.group(3), user or "dbshift", password)

    @classmethod
    def from_env(cls, dsn: str | None = None) -> "PgTarget | None":
        dsn = dsn or os.environ.get("DBSHIFT_PG_DSN")
        password = os.environ.get("DBSHIFT_PG_PASSWORD")
        if not dsn or not password:
            return None
        try:
            return cls.parse(dsn, os.environ.get("DBSHIFT_PG_USER", "dbshift"), password)
        except ValueError as exc:
            raise SystemExit(f"DBSHIFT_PG_DSN: {exc}") from exc

    @property
    def dsn(self) -> str:
        return f"{self.host}:{self.port}/{self.database}"

    def describe(self) -> str:
        return f"{self.user}@{self.dsn}"

    def connect(self):
        import pg8000.dbapi

        conn = pg8000.dbapi.connect(user=self.user, password=self.password, host=self.host,
                                    port=self.port, database=self.database, timeout=10)
        conn.autocommit = False
        return conn


def _error(exc: Exception) -> dict:
    arg = exc.args[0] if exc.args else {}
    if isinstance(arg, dict):
        return {"sqlstate": arg.get("C"), "message": arg.get("M"), "position": arg.get("P"),
                "detail": arg.get("D"), "hint": arg.get("H"), "line": arg.get("L")}
    return {"sqlstate": None, "message": str(exc), "position": None, "detail": None, "hint": None}


def check_target(t: PgTarget) -> dict:
    try:
        conn = t.connect()
    except Exception as exc:  # noqa: BLE001 -- the reason belongs in the report
        return {"ok": False, "detail": f"cannot connect to {t.describe()}: {_error(exc)['message']}"}
    try:
        cur = conn.cursor()
        cur.execute("SELECT version()")
        version = cur.fetchone()[0]
        cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'plpgsql_check'")
        has_check = cur.fetchone() is not None
        conn.rollback()
    finally:
        conn.close()
    short = re.match(r"PostgreSQL\s+(\S+)", version)
    return {"ok": True, "version": short.group(1) if short else version, "plpgsql_check_available": has_check,
            "detail": f"{t.describe()} -- PostgreSQL {short.group(1) if short else '?'}"
                      + (", plpgsql_check available" if has_check else ", plpgsql_check not installed")}


# ---------------------------------------------------------------- shadow schema

def _column_type(col: dict, owner: str, known_types: set[str]) -> tuple[str, str | None]:
    dt = (col.get("data_type") or "").upper()
    if col.get("data_type_owner"):
        if col["data_type_owner"].upper() == owner.upper() and dt in known_types:
            return f"{owner.lower()}.{dt.lower()}", None
        if col["data_type_owner"].upper() == owner.upper():
            return "TEXT", f"{dt} is not converted in this run, so the shadow has no such type; TEXT placeholder"
        return "TEXT", f"{dt} owned by {col['data_type_owner']} has no shadow; TEXT placeholder"
    if dt in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
        length = col.get("char_length") or col.get("data_length") or 1
        return (f"VARCHAR({length})" if "VARCHAR" in dt else f"CHAR({length})"), None
    if dt == "NUMBER":
        p, s = col.get("data_precision"), col.get("data_scale")
        spec = "NUMBER" if p is None else (f"NUMBER({p})" if not s else f"NUMBER({p},{s})")
        try:
            return typemap.map_type(spec)
        except Unmappable as exc:
            return "NUMERIC", str(exc)
    try:
        return typemap.map_type(dt)
    except Unmappable as exc:
        return "TEXT", f"{dt}: {exc}; TEXT placeholder"


def shadow_statements(inv: dict, owner: str, tables: set[str], type_statements: list[str]) -> tuple[list[str], list[str]]:
    """DDL for the shadow: schema, the converted types, then each referenced table.

    A column may only take a user-defined type the shadow itself creates -- the
    ones in type_statements -- not every type the inventory knows. A type routed
    MANUAL (or otherwise not converted this run) exists on Oracle but not here,
    and a table that named it would fail and take the whole owner's compile
    with it. Such a column gets a TEXT placeholder and a note instead."""
    known_types = {
        m.group(1).upper()
        for stmt in type_statements
        for m in re.finditer(r'CREATE\s+(?:TYPE|DOMAIN)\s+(?:"?\w+"?\.)?"?(\w+)"?', stmt, re.IGNORECASE)
    }
    stmts = [f"CREATE SCHEMA IF NOT EXISTS {owner.lower()}"]
    stmts += type_statements
    notes: list[str] = []
    for tbl in sorted(tables):
        cols = inventory.columns_of(inv, owner, tbl)
        if not cols:
            notes.append(f"{tbl}: no columns in discovery; not shadowed")
            continue
        defs = []
        for c in cols:
            pg, note = _column_type(c, owner, known_types)
            # Only what a reader must know about the shadow: a column stood in
            # for by a placeholder. Routine type notes belong to the real
            # conversion, not to a compile scaffold that is rolled back.
            if note and "placeholder" in note:
                notes.append(f"{tbl}.{c['column_name']}: {note}")
            null = "" if (c.get("nullable") or "Y") == "Y" else " NOT NULL"
            defs.append(f"  {c['column_name'].lower()} {pg}{null}")
        stmts.append(f"CREATE TABLE {owner.lower()}.{tbl.lower()} (\n" + ",\n".join(defs) + "\n)")
    return stmts, notes


# ---------------------------------------------------------------- compile

CHECKED = ("body parsed and every declaration resolved (check_function_bodies = on); SQL inside the "
           "body is not planned until it runs")


def _plpgsql_check(cur, schema: str, created: list[str], trigger_table: str | None) -> dict:
    findings = []
    for name in created:
        cur.execute(
            "SELECT p.oid, p.prorettype = 'trigger'::regtype FROM pg_proc p JOIN pg_namespace n "
            "ON n.oid = p.pronamespace WHERE n.nspname = %s AND p.proname = %s",
            (schema, name),
        )
        rows = cur.fetchall()
        for oid, is_trigger in rows:
            if is_trigger:
                if not trigger_table:
                    continue
                cur.execute("SELECT * FROM plpgsql_check_function(%s::oid, %s::regclass)", (oid, trigger_table))
            else:
                cur.execute("SELECT * FROM plpgsql_check_function(%s::oid)", (oid,))
            findings += [r[0] for r in cur.fetchall()]
    return {"ran": True, "findings": findings}


def compile_all(t: PgTarget, owner: str, shadow: list[str], conversions: list[tuple[str, list[str], list[str]]],
                type_conversions: list[tuple[str, list[str], list[str]]]) -> dict:
    """Run everything inside one transaction and roll it back.

    conversions / type_conversions: (key, statements, created_names). Types run
    first, each under a savepoint, so a failed type does not take the shadow
    tables with it; then the shadow tables; then everything else in order, so
    a trigger can bind to a function converted just before it."""
    results: dict[str, dict] = {}
    schema = owner.lower()
    conn = t.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'plpgsql_check'")
        has_check = cur.fetchone() is not None
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        cur.execute(f"SET LOCAL search_path TO {schema}, public")
        cur.execute("SET LOCAL check_function_bodies = on")
        if has_check:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS plpgsql_check")
            except Exception:  # noqa: BLE001
                has_check = False
                conn.rollback()
                cur = conn.cursor()
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                cur.execute(f"SET LOCAL search_path TO {schema}, public")
                cur.execute("SET LOCAL check_function_bodies = on")

        def run_one(key, statements, created, trigger_table=None):
            cur.execute("SAVEPOINT obj")
            for i, stmt in enumerate(statements, 1):
                try:
                    cur.execute(stmt)
                except Exception as exc:  # noqa: BLE001 -- recorded, never swallowed
                    err = _error(exc)
                    cur.execute("ROLLBACK TO SAVEPOINT obj")
                    results[key] = {"ok": False, "statement": i, **err, "checked": CHECKED,
                                    "plpgsql_check": {"ran": False, "findings": []}}
                    return
            check = {"ran": False, "findings": []}
            if has_check:
                try:
                    check = _plpgsql_check(cur, schema, created, trigger_table)
                except Exception as exc:  # noqa: BLE001
                    check = {"ran": False, "findings": [], "error": _error(exc)["message"]}
                    cur.execute("ROLLBACK TO SAVEPOINT obj")
                    results[key] = {"ok": True, "checked": CHECKED, "plpgsql_check": check}
                    return
            cur.execute("RELEASE SAVEPOINT obj")
            results[key] = {"ok": not check["findings"] or all("warning" in f.lower() for f in check["findings"]),
                            "checked": CHECKED + (", plus plpgsql_check on the embedded SQL" if check["ran"] else ""),
                            "plpgsql_check": check}

        for key, statements, created in type_conversions:
            run_one(key, statements, created)
        for stmt in shadow[1:]:  # the schema itself is already there
            if stmt.upper().startswith("CREATE TYPE") or stmt.upper().startswith("CREATE DOMAIN"):
                continue  # converted types ran above
            try:
                cur.execute(stmt)
            except Exception as exc:  # noqa: BLE001
                err = _error(exc)
                raise RuntimeError(f"shadow schema failed: {err['message']} -- statement: {stmt[:120]}") from exc
        for key, statements, created in conversions:
            trig = None
            for s in statements:
                m = re.search(r"\bCREATE\s+TRIGGER\b[\s\S]*?\bON\s+([\w.]+)", s, re.IGNORECASE)
                if m:
                    trig = m.group(1)
            run_one(key, statements, created, trig)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
    return results
