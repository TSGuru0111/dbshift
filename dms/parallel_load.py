"""Parallel-load table-settings rules: split one table's full load across
several DMS threads by range, instead of one thread reading it start to finish.

DMS's own MaxFullLoadSubTasks (see task_settings in mappings.py) already loads
several *tables* at once; it does nothing for the one table that dwarfs the
rest of the estate, which is exactly what a per-table "ranges" rule is for --
DMS reads that table with several threads instead of one, each bounded by a
slice of one column's real values, the same mechanism AWS's own DMS docs show:

    {"rule-type": "table-settings", "object-locator": {...},
     "parallel-load": {"type": "ranges", "columns": ["ID"],
                       "boundaries": [[101605055], [203210111], ...]}}

The slice needs real values, not a guess: an evenly-spaced guess over an
estimated row count can put every row of a skewed key in one partition and
leave the rest empty, which loads slower than not partitioning at all. So the
column's actual MIN/MAX is read live -- see `fetch_ranges` -- immediately
before the boundaries are computed, the same "nothing is trusted from an
earlier run" rule provision.deploy already follows for its own render.

Only tables with a single-column, numeric primary key are eligible. A
composite key, a non-numeric key (a business code, a UUID), or no primary key
at all still migrates -- just as one ordinary single-threaded pass, which is
what DMS already does without a rule here.
"""

from __future__ import annotations

import re

# Catalog identifiers only -- never user input -- but interpolated rather than
# bound (Oracle cannot bind an identifier), so this is checked the same way
# collector/probes/dataprofile.py checks its own interpolated names.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_$#]+$")

# The Oracle NUMBER family DMS can range-partition on. A column that is a
# string, a date, or a LOB is never offered as a split column.
_NUMERIC_TYPES = {"NUMBER", "FLOAT", "INTEGER", "BINARY_FLOAT", "BINARY_DOUBLE"}


def _quote(name: str) -> str:
    if not _IDENTIFIER.match(name or ""):
        raise ValueError(f"refusing to interpolate unexpected identifier: {name!r}")
    return f'"{name}"'


def numeric_pk_columns(constraints: list[dict], constraint_columns: list[dict],
                       columns: list[dict], *, schema: str, tables: list[str]) -> dict[str, str]:
    """table -> its primary key column, for every included table whose primary
    key is exactly one NUMBER-family column. Pure and offline: everything it
    reads is already on disk from Discovery (constraints, constraint_columns,
    columns), so this runs on every plan, not only when a live read is asked
    for -- it is what makes a table eligible, before any value is read.
    """
    wanted = set(tables)
    pk_constraint_names = {c["constraint_name"] for c in constraints
                           if c.get("owner") == schema and c.get("constraint_type") == "P"
                           and c.get("table_name") in wanted}
    by_constraint: dict[str, list[dict]] = {}
    for cc in constraint_columns:
        if cc.get("owner") == schema and cc.get("constraint_name") in pk_constraint_names:
            by_constraint.setdefault(cc["constraint_name"], []).append(cc)

    table_pk_col: dict[str, str] = {}
    for c in constraints:
        if (c.get("owner") != schema or c.get("constraint_type") != "P"
                or c.get("table_name") not in wanted):
            continue
        cols = sorted(by_constraint.get(c["constraint_name"], []), key=lambda x: x.get("position") or 0)
        if len(cols) == 1:   # a composite key cannot be a single-column range
            table_pk_col[c["table_name"]] = cols[0]["column_name"]

    numeric = {(col["table_name"], col["column_name"])
              for col in columns
              if col.get("owner") == schema and (col.get("data_type") or "").upper() in _NUMERIC_TYPES}
    return {t: col for t, col in table_pk_col.items() if (t, col) in numeric}


def fetch_ranges(connect, schema: str,
                 table_columns: dict[str, str]) -> dict[str, tuple[str, int, int]]:
    """table -> (column, min, max), read live, one query per table.

    `connect` is a zero-arg callable returning an open DB-API connection; this
    function owns it for the duration of the read and closes it, the same
    lifetime collector/db.py's own Session holds for a run. A table that
    cannot be read -- the account lacks SELECT, the table is now gone, the
    column is entirely NULL -- is left out of the result rather than raising:
    it falls back to a normal single-threaded load, which is not a failure
    worth stopping the whole plan over.
    """
    if not table_columns:
        return {}
    out: dict[str, tuple[str, int, int]] = {}
    conn = connect()
    try:
        with conn.cursor() as cur:
            for table, col in sorted(table_columns.items()):
                sql = (f"SELECT MIN({_quote(col)}), MAX({_quote(col)}) "
                       f"FROM {_quote(schema)}.{_quote(table)}")
                try:
                    cur.execute(sql)
                    lo, hi = cur.fetchone()
                except Exception:  # noqa: BLE001 -- one unreadable table must not sink the rest
                    continue
                if lo is not None and hi is not None:
                    out[table] = (col, int(lo), int(hi))
    finally:
        conn.close()
    return out


def boundaries(low: int, high: int, batches: int) -> list[list[int]] | None:
    """`batches - 1` interior cut points splitting [low, high] into `batches`
    roughly equal ranges -- DMS's own rule shape, one boundary per gap between
    partitions, each a one-element list because `columns` names exactly one.

    None when the range genuinely cannot be split this many ways: fewer than
    `batches` distinct integer values between low and high collapses two cuts
    onto the same value, which DMS would refuse as an invalid rule. The caller
    falls back to a normal single-threaded load for that table rather than
    emitting a rule that would fail, or one whose partitions would mostly sit
    empty.
    """
    if batches < 2 or low is None or high is None or high <= low:
        return None
    span = high - low
    if span < batches:
        return None
    step = span / batches
    cuts = [low + round(step * i) for i in range(1, batches)]
    if len(set(cuts)) != len(cuts):
        return None
    return [[c] for c in cuts]


def build_rules(*, schema: str, table_ranges: dict[str, tuple[str, int, int]],
                batches: int) -> tuple[list[dict], dict]:
    """table-settings rules for every table whose range could actually be
    split into `batches`, plus a report of which were and were not -- shown to
    the user rather than left for them to notice missing parallelism, or
    missing rows, on their own.
    """
    out: list[dict] = []
    split: list[str] = []
    for table, (column, low, high) in sorted(table_ranges.items()):
        b = boundaries(low, high, batches)
        if b is None:
            continue
        out.append({
            "rule-type": "table-settings",
            "object-locator": {"schema-name": schema, "table-name": table},
            "parallel-load": {"type": "ranges", "columns": [column], "boundaries": b},
        })
        split.append(table)
    return out, {"split": sorted(split)}
