"""MySQL -> PostgreSQL column mapping, for Phase 4c's DDL and 4b's shadow schema.

The Oracle map (`typemap.py`) works from a rebuilt `NUMBER(p,s)` spec. MySQL's
catalogue already carries the full declared type in `column_type` -- `bigint
unsigned`, `enum('new','paid')`, `decimal(14,2)` -- which the collector emits as
`data_type_mod`, so this map reads that directly rather than reassembling it.

Every choice below is made for the **load**, because Phase 7 moves the data
with DMS into exactly these tables:

  - **Unsigned integers are widened, never narrowed.** PostgreSQL has no
    unsigned types. `int unsigned` reaches 4,294,967,295, which does not fit an
    `integer`, so it becomes `bigint`. `bigint unsigned` reaches 2^64-1, past
    `bigint`'s 2^63-1, so a value column becomes `numeric(20,0)` -- while an
    AUTO_INCREMENT key, or a foreign key to one, stays `bigint`: its values are
    bounded by the identity that generates them, and `numeric` could not be an
    identity nor match the key it references. Phase 3 measured this estate's
    values and flags the columns that exceed that bound.
  - **`tinyint(1)` stays `smallint`.** It is conventionally a boolean, but DMS
    delivers 0/1 and a `boolean` column refuses them. Validation declares the
    difference (`tinyint1_is_boolean`); converting after cutover is a choice.
  - **ENUM and SET become text with a CHECK.** A PostgreSQL enum type would be
    closer, but DMS writes the value as a string and a CHECK is simpler to
    change later. SET arrives as its comma-separated string; the CHECK proves
    every member is one of the declared values.
  - **JSON becomes jsonb**, which does not keep key order or duplicate keys --
    said in a note, because it is a real change.

An unmappable type raises `typemap.Unmappable`, as the Oracle map does: the
column goes to a person, never to a guess.
"""

from __future__ import annotations

import re

from .typemap import Unmappable

_INT = {
    # (signed, unsigned)
    "tinyint": ("smallint", "smallint"),
    "smallint": ("smallint", "integer"),
    "mediumint": ("integer", "integer"),
    "int": ("integer", "bigint"),
    "integer": ("integer", "bigint"),
    "bigint": ("bigint", "numeric(20,0)"),
}

_TEXT = {"tinytext", "text", "mediumtext", "longtext"}
_BINARY = {"tinyblob", "blob", "mediumblob", "longblob", "binary", "varbinary"}
_SPATIAL = {"geometry", "point", "linestring", "polygon", "multipoint",
            "multilinestring", "multipolygon", "geometrycollection", "geomcollection"}


def column_type(col: dict) -> str:
    """The full declared MySQL type, lower-cased: `bigint unsigned`, `enum('a','b')`."""
    return (col.get("data_type_mod") or col.get("column_type")
            or col.get("data_type") or "").strip().lower()


def is_identity(col: dict) -> bool:
    return (col.get("identity_column") == "YES"
            or "auto_increment" in (col.get("extra") or "").lower())


def is_not_null(col: dict) -> bool:
    # MySQL says YES/NO; Oracle says Y/N. Reading only Oracle's 'N' made every
    # MySQL column nullable on the target.
    return str(col.get("nullable") or "").upper() in ("N", "NO")


def enum_values(col: dict) -> list[str]:
    """The declared members of an ENUM or SET, unquoted."""
    m = re.match(r"^(?:enum|set)\s*\((.*)\)\s*$", column_type(col), re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    return [v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", m.group(1))]


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def map_column(col: dict, *, fk_columns: set[str] | None = None) -> tuple[str, str | None]:
    """Return (postgres_type, note). Raises Unmappable rather than guessing.

    `fk_columns` names the columns (lower case) that are foreign keys; a
    `bigint unsigned` among them stays `bigint` to match the key it references.
    """
    ct = column_type(col)
    base = (col.get("data_type") or "").lower()
    unsigned = " unsigned" in f" {ct}"
    name = (col.get("column_name") or "").lower()

    if base in _INT:
        if base == "tinyint" and re.match(r"^tinyint\(1\)", ct) and not unsigned:
            return "smallint", ("tinyint(1) is conventionally a boolean; kept as smallint so "
                                "DMS loads 0/1 unchanged (validation declares the difference)")
        if base == "bigint" and unsigned:
            if is_identity(col):
                return "bigint", ("bigint unsigned AUTO_INCREMENT -> bigint identity: values are "
                                  "bounded by the identity, and 2^63-1 is the ceiling")
            if fk_columns and name in fk_columns:
                return "bigint", ("bigint unsigned foreign key -> bigint, to match the identity key "
                                  "it references")
            return "numeric(20,0)", ("bigint unsigned can exceed bigint's 2^63-1, so it is widened "
                                     "to numeric(20,0); Phase 3 names the columns whose values need it")
        pg = _INT[base][1 if unsigned else 0]
        note = f"{ct} has no unsigned form in PostgreSQL; widened to {pg}" if unsigned and pg != _INT[base][0] else None
        return pg, note

    if base in ("decimal", "numeric"):
        p, s = col.get("data_precision"), col.get("data_scale")
        if p is None:
            return "numeric", None
        note = "unsigned is not carried; a CHECK (>= 0) would restore it" if unsigned else None
        return (f"numeric({p},{s})" if s else f"numeric({p})"), note
    if base == "float":
        return "real", None
    if base in ("double", "real", "double precision"):
        return "double precision", None
    if base == "bit":
        m = re.match(r"bit\((\d+)\)", ct)
        return f"bit({m.group(1) if m else 1})", None
    if base == "boolean" or base == "bool":
        return "boolean", None

    if base == "char":
        return f"char({col.get('char_length') or 1})", None
    if base == "varchar":
        return f"varchar({col.get('char_length') or 255})", None
    if base in _TEXT:
        return "text", None
    if base in ("enum", "set"):
        vals = enum_values(col)
        if not vals:
            raise Unmappable(f"{ct}: the declared members could not be read")
        return "text", (f"{base.upper()} -> text with a CHECK on its {len(vals)} declared "
                        "member(s); DMS writes the value as a string")
    if base == "json":
        return "jsonb", "jsonb does not keep key order or duplicate keys"
    if base in _BINARY:
        return "bytea", None

    if base == "date":
        return "date", None
    if base == "datetime":
        m = re.match(r"datetime\((\d)\)", ct)
        return (f"timestamp({m.group(1)})" if m else "timestamp(0)"), None
    if base == "timestamp":
        m = re.match(r"timestamp\((\d)\)", ct)
        return ((f"timestamptz({m.group(1)})" if m else "timestamptz(0)"),
                "MySQL TIMESTAMP is stored in UTC and shown in the session zone -- timestamptz")
    if base == "time":
        return "interval", ("MySQL TIME spans -838:59:59 to 838:59:59, which a PostgreSQL time "
                            "cannot hold; interval can")
    if base == "year":
        return "smallint", None

    if base in _SPATIAL:
        raise Unmappable(f"{ct}: spatial types need PostGIS, which is a decision, not a mapping")
    raise Unmappable(f"no mapping for MySQL {ct or base}")


def check_for(col: dict, column_sql: str) -> str | None:
    """The CHECK condition an ENUM or SET column needs, or None."""
    base = (col.get("data_type") or "").lower()
    vals = enum_values(col)
    if not vals:
        return None
    members = ", ".join(_lit(v) for v in vals)
    if base == "enum":
        return f"{column_sql} IN ({members})"
    if base == "set":
        # '' is a legal SET value (no members); string_to_array('', ',') is {}.
        return f"string_to_array({column_sql}, ',') <@ ARRAY[{members}]::text[]"
    return None


_NUMERIC_TYPES = ("smallint", "integer", "bigint", "numeric", "real", "double precision")


def default_expr(col: dict, pg_type: str) -> tuple[str | None, str | None]:
    """Translate a MySQL column default, or decline it with the reason.

    MySQL 8 reports a literal default **unquoted** (`GB`, `new`, `0.00`) and an
    expression default with `DEFAULT_GENERATED` in `extra`. Copying `GB` into
    DDL unquoted would read as a column reference and fail.
    """
    d = col.get("data_default")
    if d is None:
        return None, None
    d = str(d)
    extra = (col.get("extra") or "").lower()

    if "default_generated" in extra:
        e = d.strip()
        if re.fullmatch(r"(?i)current_timestamp(\(\d?\))?|now\(\)", e):
            return ("LOCALTIMESTAMP" if pg_type.startswith("timestamp(") or pg_type == "timestamp"
                    else "CURRENT_TIMESTAMP"), None
        if re.fullmatch(r"(?i)curdate\(\)|current_date", e):
            return "CURRENT_DATE", None
        if re.fullmatch(r"(?i)\(?uuid\(\)\)?", e):
            return "gen_random_uuid()::text", None
        return None, f"expression default {e!r} is MySQL SQL; it is not carried across"

    if pg_type.startswith(_NUMERIC_TYPES):
        if re.fullmatch(r"-?\d+(\.\d+)?", d.strip()):
            return d.strip(), None
        return None, f"default {d!r} is not a number for a {pg_type} column; not carried across"
    if pg_type == "jsonb":
        return None, "a JSON default is not carried across"
    if pg_type.startswith(("bit", "bytea", "boolean")):
        return None, f"default {d!r} on {pg_type} is not carried across"
    # Text, char, date, timestamp and the ENUM/SET text columns: a quoted literal.
    return _lit(d), None


def on_update_now(col: dict) -> bool:
    """`ON UPDATE CURRENT_TIMESTAMP` -- a column-level behaviour PostgreSQL lacks."""
    return "on update current_timestamp" in (col.get("extra") or "").lower()


def is_generated(col: dict) -> bool:
    return "generated" in (col.get("extra") or "").lower() and "default_generated" not in (
        col.get("extra") or "").lower()
