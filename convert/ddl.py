"""Table, index and constraint DDL for a PostgreSQL target.

Built from what discovery recorded, not from a live connection, so the same
plan can be reviewed before anything exists and re-rendered identically later.

**Why this exists rather than letting DMS do it.** DMS creates missing target
tables itself, with a fixed type mapping that knows nothing about the estate:
every `NUMBER` becomes `numeric`, whatever its precision. That is correct but
wasteful and lossy in both directions -- a `NUMBER(1)` flag becomes an
arbitrary-precision decimal, and a `NUMBER(10,0)` that fits in a 4-byte integer
does not get one. It also creates no constraints and no indexes, so referential
integrity and every access path arrive only if somebody adds them afterwards.

The order things are emitted in is deliberate and is the same order Phase 7's
Data Pump path uses for the homogeneous case:

    tables -> data loads -> primary and unique keys -> foreign keys -> indexes
                                                                   -> check constraints

Constraints after the load, because validating a foreign key row by row during
a bulk insert is the slowest possible way to do it. Indexes after the load for
the same reason.
"""

from __future__ import annotations

import re

from . import typemap
from .typemap import Unmappable

# Reserved in PostgreSQL but not in Oracle. A column called ORDER or USER is
# legal Oracle and a syntax error in PostgreSQL unless quoted -- and quoting it
# permanently is worse, because every hand-written query then has to quote it
# too. These are renamed with a suffix and the rename is reported.
PG_RESERVED = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric",
    "both", "case", "cast", "check", "collate", "column", "constraint", "create",
    "current_catalog", "current_date", "current_role", "current_time",
    "current_timestamp", "current_user", "default", "deferrable", "desc",
    "distinct", "do", "else", "end", "except", "false", "fetch", "for", "foreign",
    "from", "grant", "group", "having", "in", "initially", "intersect", "into",
    "lateral", "leading", "limit", "localtime", "localtimestamp", "not", "null",
    "offset", "on", "only", "or", "order", "placing", "primary", "references",
    "returning", "select", "session_user", "some", "symmetric", "table", "then",
    "to", "trailing", "true", "union", "unique", "user", "using", "variadic",
    "when", "where", "window", "with",
}

# PostgreSQL truncates identifiers at 63 bytes; Oracle allows 128 since 12.2.
# A silently truncated name can collide with another, so it is reported.
PG_MAX_IDENT = 63


class DdlNote:
    """One thing a reviewer must know about the generated DDL."""

    def __init__(self, kind: str, subject: str, detail: str, severity: str = "info"):
        self.kind, self.subject, self.detail, self.severity = kind, subject, detail, severity

    def as_dict(self) -> dict:
        return {"kind": self.kind, "subject": self.subject, "detail": self.detail,
                "severity": self.severity}


def ident(name: str, notes: list[DdlNote] | None = None, *, context: str = "") -> str:
    """An Oracle name as PostgreSQL should hold it.

    Lower-cased to match what Phase 7's DMS transformation rules do, because a
    table the data lands in as `customer` cannot be created as `CUSTOMER`.
    """
    out = (name or "").lower()
    if out in PG_RESERVED:
        renamed = out + "_col"
        if notes is not None:
            notes.append(DdlNote(
                "reserved_word", f"{context}{name}",
                f"`{out}` is reserved in PostgreSQL but not in Oracle, so it is renamed to "
                f"`{renamed}`. Quoting it instead would work, but then every hand-written query "
                "would have to quote it forever. Application SQL referencing this name must change.",
                "warn"))
        return renamed
    if len(out.encode()) > PG_MAX_IDENT:
        cut = out.encode()[:PG_MAX_IDENT].decode(errors="ignore")
        if notes is not None:
            notes.append(DdlNote(
                "identifier_too_long", f"{context}{name}",
                f"PostgreSQL truncates identifiers at {PG_MAX_IDENT} bytes, so this becomes "
                f"`{cut}`. Check it does not collide with another truncated name.", "warn"))
        return cut
    return out


def _default_expr(oracle_default: str | None, data_type: str) -> tuple[str | None, str | None]:
    """Translate a column default, or decline it.

    Most defaults are literals and carry across untouched. The handful that are
    Oracle function calls need translating, and anything else is declined rather
    than copied into DDL that would fail to parse -- a default that silently
    disappears is worse than one that is reported.
    """
    if oracle_default is None:
        return None, None
    d = str(oracle_default).strip().rstrip(";").strip()
    if not d or d.upper() == "NULL":
        return None, None

    u = d.upper()
    direct = {
        "SYSDATE": "CURRENT_TIMESTAMP",
        "SYSTIMESTAMP": "CURRENT_TIMESTAMP",
        "CURRENT_DATE": "CURRENT_DATE",
        "CURRENT_TIMESTAMP": "CURRENT_TIMESTAMP",
        "USER": "CURRENT_USER",
        "SYS_GUID()": "gen_random_uuid()",
    }
    if u in direct:
        return direct[u], (
            "gen_random_uuid() needs the pgcrypto extension on PostgreSQL below 13"
            if u == "SYS_GUID()" else None)

    # A sequence default. Oracle writes it as "OWNER"."SEQ"."NEXTVAL";
    # PostgreSQL spells the same thing nextval('owner.seq'). Declining this
    # would leave the column with no default at all, so every insert that
    # relied on it would fail -- and Phase 7's residue already resets the
    # sequence to the source's current value, so the two fit together.
    m = re.fullmatch(r'"?(\w+)"?\."?(\w+)"?\."?NEXTVAL"?', d, re.IGNORECASE)
    if m:
        return f"nextval('{m.group(1).lower()}.{m.group(2).lower()}')", None
    m = re.fullmatch(r'"?(\w+)"?\.NEXTVAL', d, re.IGNORECASE)
    if m:
        return f"nextval('{m.group(1).lower()}')", None

    # A quoted string or a plain number carries across as it is.
    if re.fullmatch(r"'(?:[^']|'')*'", d) or re.fullmatch(r"-?\d+(\.\d+)?", d):
        return d, None

    return None, f"default {d!r} is not a literal or a known function; it is not carried across"


def column_ddl(col: dict, notes: list[DdlNote], *, table: str) -> str | None:
    """One column definition, or None when its type cannot be mapped."""
    name = ident(col["column_name"], notes, context=f"{table}.")
    spec = col["data_type"]
    # Rebuild the Oracle type with its precision, the way typemap expects it.
    if spec in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR") and col.get("char_length"):
        spec = f"{spec}({col['char_length']})"
    elif spec == "NUMBER" and col.get("data_precision") is not None:
        spec = (f"NUMBER({col['data_precision']},{col['data_scale']})"
                if col.get("data_scale") else f"NUMBER({col['data_precision']})")
    elif spec == "RAW" and col.get("data_length"):
        spec = f"RAW({col['data_length']})"

    try:
        pg_type, note = typemap.map_type(spec, owner=col.get("owner"))
    except Unmappable as exc:
        notes.append(DdlNote("unmappable_type", f"{table}.{col['column_name']}",
                             f"{exc}. The column is omitted, so this table is incomplete until "
                             "somebody decides what it should be.", "error"))
        return None
    if note:
        notes.append(DdlNote("type_mapping", f"{table}.{col['column_name']}",
                             f"{col['data_type']} -> {pg_type}: {note}"))

    parts = [f"  {name} {pg_type}"]

    if col.get("identity_column") == "YES":
        # Oracle's identity and PostgreSQL's are the same standard feature.
        parts.append("GENERATED BY DEFAULT AS IDENTITY")
    else:
        default, why = _default_expr(col.get("data_default"), col["data_type"])
        if default:
            parts.append(f"DEFAULT {default}")
        elif why:
            notes.append(DdlNote("default_not_carried", f"{table}.{col['column_name']}", why, "warn"))

    if (col.get("nullable") or "Y") == "N":
        parts.append("NOT NULL")
    return " ".join(parts)


def table_ddl(*, owner: str, table: str, columns: list[dict],
              notes: list[DdlNote]) -> str | None:
    """CREATE TABLE, columns only. Keys and indexes come after the load."""
    defs = []
    for c in sorted(columns, key=lambda c: c.get("column_id") or 0):
        if c.get("hidden_column") == "YES" or c.get("user_generated") == "NO":
            continue
        if c.get("virtual_column") == "YES":
            notes.append(DdlNote(
                "virtual_column", f"{table}.{c['column_name']}",
                "a virtual column is computed, not stored. PostgreSQL has generated columns, but "
                "the expression is Oracle SQL and needs translating, so the column is omitted "
                "rather than guessed at.", "warn"))
            continue
        d = column_ddl(c, notes, table=table)
        if d:
            defs.append(d)
    if not defs:
        notes.append(DdlNote("empty_table", table,
                             "no column could be mapped; no table is emitted", "error"))
        return None
    return (f"CREATE TABLE {ident(owner)}.{ident(table)} (\n"
            + ",\n".join(defs) + "\n);")


def _cols_for(constraint_columns: list[dict], owner: str, constraint: str,
              notes: list[DdlNote]) -> list[str]:
    rows = [r for r in constraint_columns
            if r.get("owner") == owner and r.get("constraint_name") == constraint]
    rows.sort(key=lambda r: r.get("position") or 0)
    return [ident(r["column_name"], notes) for r in rows]


def constraint_ddl(*, owner: str, constraints: list[dict], constraint_columns: list[dict],
                   notes: list[DdlNote]) -> dict[str, list[str]]:
    """Keys and checks, grouped by when they may safely be applied.

    Primary and unique keys go on before the foreign keys that reference them.
    Everything goes on **after** the data load: validating a foreign key row by
    row during a bulk insert is the slowest way to do it, and a check
    constraint on an empty table proves nothing.
    """
    out: dict[str, list[str]] = {"primary_unique": [], "foreign": [], "check": []}

    for c in sorted(constraints, key=lambda c: (c.get("table_name") or "", c.get("constraint_name") or "")):
        if c.get("owner") != owner:
            continue
        kind, table = c.get("constraint_type"), c.get("table_name")
        name = ident(c["constraint_name"], notes)
        qualified = f"{ident(owner)}.{ident(table)}"

        if kind in ("P", "U"):
            cols = _cols_for(constraint_columns, owner, c["constraint_name"], notes)
            if not cols:
                notes.append(DdlNote("constraint_no_columns", c["constraint_name"],
                                     "discovery recorded no columns for this constraint; it is "
                                     "not emitted", "warn"))
                continue
            word = "PRIMARY KEY" if kind == "P" else "UNIQUE"
            out["primary_unique"].append(
                f"ALTER TABLE {qualified} ADD CONSTRAINT {name} {word} ({', '.join(cols)});")

        elif kind == "R":
            cols = _cols_for(constraint_columns, owner, c["constraint_name"], notes)
            parent = next((p for p in constraints
                           if p.get("constraint_name") == c.get("r_constraint_name")
                           and p.get("owner") == (c.get("r_owner") or owner)), None)
            if not cols or not parent:
                notes.append(DdlNote("foreign_key_unresolved", c["constraint_name"],
                                     "the referenced constraint is not in this run's discovery, so "
                                     "the foreign key is not emitted", "warn"))
                continue
            pcols = _cols_for(constraint_columns, c.get("r_owner") or owner,
                              parent["constraint_name"], notes)
            rule = ""
            if (c.get("delete_rule") or "NO ACTION").upper() == "CASCADE":
                rule = " ON DELETE CASCADE"
            elif (c.get("delete_rule") or "").upper() == "SET NULL":
                rule = " ON DELETE SET NULL"
            out["foreign"].append(
                f"ALTER TABLE {qualified} ADD CONSTRAINT {name} FOREIGN KEY "
                f"({', '.join(cols)}) REFERENCES {ident(c.get('r_owner') or owner)}."
                f"{ident(parent['table_name'])} ({', '.join(pcols)}){rule};")

        elif kind == "C":
            cond = (c.get("search_condition_vc") or "").strip()
            # Oracle generates a NOT NULL check for every NOT NULL column. The
            # column definition already carries it; emitting both would be
            # duplicate and confusing.
            if re.fullmatch(r'"?\w+"?\s+IS\s+NOT\s+NULL', cond, re.IGNORECASE):
                continue
            translated, why = _translate_check(cond)
            if translated is None:
                notes.append(DdlNote("check_not_translated", c["constraint_name"],
                                     f"{why}. The rule is not enforced on the target until "
                                     "somebody writes it.", "warn"))
                continue
            out["check"].append(
                f"ALTER TABLE {qualified} ADD CONSTRAINT {name} CHECK ({translated});")

    return out


def _translate_check(condition: str) -> tuple[str | None, str | None]:
    """Translate a CHECK condition, or decline it.

    Deliberately conservative. A check constraint is a business rule, and one
    translated wrongly either rejects valid data or admits invalid data --
    both worse than one reported as needing a person.
    """
    if not condition:
        return None, "the condition is empty"
    c = condition.strip()

    # Oracle quotes identifiers in the stored text; PostgreSQL would read those
    # as case-sensitive names that do not exist after lower-casing.
    c = re.sub(r'"([A-Za-z_][A-Za-z0-9_$#]*)"', lambda m: m.group(1).lower(), c)

    upper = c.upper()
    for oracle_only in ("SYSDATE", "NVL(", "DECODE(", "TO_DATE(", "REGEXP_LIKE(",
                        "SUBSTR(", "INSTR(", "TRUNC("):
        if oracle_only in upper:
            return None, (f"the condition uses {oracle_only.rstrip('(')}, which has a different "
                          "name or different semantics in PostgreSQL")
    return c, None


def index_ddl(*, owner: str, indexes: list[dict], index_columns: list[dict],
              constraints: list[dict], notes: list[DdlNote]) -> list[str]:
    """Indexes, minus the ones a constraint already creates.

    PostgreSQL builds an index for every primary key and unique constraint
    automatically. Emitting those separately would create a duplicate index
    that costs write throughput and storage for nothing.
    """
    constraint_indexes = {c.get("index_name") for c in constraints
                          if c.get("owner") == owner and c.get("constraint_type") in ("P", "U")}
    out = []
    for ix in sorted(indexes, key=lambda i: i.get("index_name") or ""):
        if ix.get("owner") != owner:
            continue
        name = ix["index_name"]
        if name in constraint_indexes:
            continue
        if name.upper().startswith(("SYS_", "DR$")):
            notes.append(DdlNote("index_skipped", name,
                                 "an Oracle-generated index; the feature that owns it rebuilds "
                                 "its own indexes on the target"))
            continue
        itype = (ix.get("index_type") or "").upper()
        if itype not in ("NORMAL", "NORMAL/REV", "FUNCTION-BASED NORMAL"):
            notes.append(DdlNote("index_type", name,
                                 f"{itype} has no direct PostgreSQL equivalent. A bitmap index "
                                 "becomes a B-tree, and domain indexes belong to a feature that "
                                 "is not migrated. Review the access path.", "warn"))
            continue
        if itype.startswith("FUNCTION-BASED"):
            notes.append(DdlNote("index_expression", name,
                                 "a function-based index: the expression is Oracle SQL and needs "
                                 "translating, so the index is not emitted", "warn"))
            continue

        cols = [r for r in index_columns
                if r.get("index_owner") == owner and r.get("index_name") == name]
        cols.sort(key=lambda r: r.get("column_position") or 0)
        if not cols:
            continue
        col_list = ", ".join(
            ident(r["column_name"], notes)
            + (" DESC" if (r.get("descend") or "ASC").upper() == "DESC" else "")
            for r in cols)
        unique = "UNIQUE " if (ix.get("uniqueness") or "").upper() == "UNIQUE" else ""
        out.append(f"CREATE {unique}INDEX {ident(name, notes)} ON "
                   f"{ident(owner)}.{ident(ix['table_name'])} ({col_list});")
    return out


def build(*, owner: str, datasets: dict) -> dict:
    """Every statement needed to create this schema on PostgreSQL, in order."""
    notes: list[DdlNote] = []
    columns = [c for c in datasets.get("columns", []) if c.get("owner") == owner]
    by_table: dict[str, list[dict]] = {}
    for c in columns:
        by_table.setdefault(c["table_name"], []).append(c)

    # Only real tables: Oracle internals, materialized views and external
    # tables are excluded for the same reasons Phase 7 excludes them.
    from dms import mappings as dms_mappings
    selection = dms_mappings.select_tables(
        datasets.get("tables", []), datasets.get("objects", []), owner,
        external_tables=datasets.get("external_tables", []),
        queues=datasets.get("queues", []))

    tables = []
    for t in selection["include"]:
        ddl = table_ddl(owner=owner, table=t, columns=by_table.get(t, []), notes=notes)
        if ddl:
            tables.append(ddl)

    included = set(selection["include"])
    cons = constraint_ddl(
        owner=owner,
        constraints=[c for c in datasets.get("constraints", [])
                     if c.get("table_name") in included],
        constraint_columns=datasets.get("constraint_columns", []), notes=notes)
    idx = index_ddl(owner=owner,
                    indexes=[i for i in datasets.get("indexes", [])
                             if i.get("table_name") in included],
                    index_columns=datasets.get("index_columns", []),
                    constraints=datasets.get("constraints", []), notes=notes)

    # Columns typed as a user-defined type need that type created first, and
    # Phase 4b is what converts one. Saying which types this DDL depends on is
    # better than emitting a CREATE TABLE that fails on a missing type.
    # data_type_owner is set for XMLTYPE too, because Oracle implements it as an
    # object type owned by SYS -- but PostgreSQL has a native xml type and
    # typemap already maps it, so listing it as something Phase 4b must create
    # would send a reader looking for a conversion that does not exist.
    builtin_object_types = {"XMLTYPE", "ANYDATA", "ANYTYPE", "SDO_GEOMETRY", "URITYPE"}
    udt = sorted({(c.get("data_type") or "").upper() for c in columns
                  if c.get("data_type_owner")
                  and (c.get("data_type") or "").upper() not in builtin_object_types
                  and c.get("table_name") in set(selection["include"])})
    if udt:
        notes.append(DdlNote(
            "depends_on_types", ", ".join(udt),
            "these columns are typed as user-defined types. Phase 4b converts those to PostgreSQL "
            "composite types or domains, and they must be created before these tables. Run 4b's "
            "type conversions first, or the CREATE TABLE fails on a missing type.", "warn"))

    return {
        "schema": f"CREATE SCHEMA IF NOT EXISTS {ident(owner)};",
        "depends_on_types": udt,
        "tables": tables,
        "primary_unique": cons["primary_unique"],
        "foreign": cons["foreign"],
        "check": cons["check"],
        "indexes": idx,
        "notes": [n.as_dict() for n in notes],
        "order": [
            ("schema", "the schema itself"),
            ("tables", "tables, columns only -- so the load has somewhere to go"),
            ("primary_unique", "primary and unique keys, AFTER the load"),
            ("foreign", "foreign keys, after the keys they reference exist"),
            ("check", "check constraints"),
            ("indexes", "indexes last: building one during a bulk load is the slowest way"),
        ],
        "counts": {"tables": len(tables), "primary_unique": len(cons["primary_unique"]),
                   "foreign": len(cons["foreign"]), "check": len(cons["check"]),
                   "indexes": len(idx),
                   "notes_error": sum(1 for n in notes if n.severity == "error"),
                   "notes_warn": sum(1 for n in notes if n.severity == "warn")},
        "nothing_applied": True,
    }


def statements_in_order(plan: dict) -> list[str]:
    """Flatten the plan into the sequence a target would actually run."""
    out = [plan["schema"]]
    for key, _ in plan["order"][1:]:
        out += plan[key]
    return out
