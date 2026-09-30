"""Phase 8 for a MySQL source: the five levels, against RDS for MySQL or PostgreSQL.

`levels.py` is written against Oracle's catalogue (dba_* views, ORA_HASH, TO_CHAR
masks). None of it applies to a MySQL source, and threading `if mysql` through
it would hide which rules belong to which engine -- so the MySQL levels live
here, and `levels.py` dispatches to them.

**MySQL -> RDS for MySQL** compares like with like. Every level runs the same SQL
on both sides against information_schema, and level 4 hashes one canonical text
per row with MD5, which both servers compute identically. The expected
differences are the ones Phase 7 made on purpose, each named: definers rewritten
for RDS, a MyISAM table now InnoDB, events created disabled until cutover.

**MySQL -> RDS for PostgreSQL** compares values, not definitions. The canonical
text is built per engine and keyed on the **MySQL** type -- the question is
"does the target hold the value the source had", and the source type says how
to render it:

    DECIMAL      trailing zeros stripped on both sides (14.50 = 14.5)
    DATETIME     to the second (or to the declared fraction), zone-less
    TIMESTAMP    in UTC on both sides -- MySQL renders it in the session zone
    CHAR         right-trimmed: MySQL strips padding on read, PostgreSQL char(n) pads
    JSON         MySQL's normalised text and PostgreSQL's jsonb text agree on key
                 order and spacing -- verified on real rows, not assumed
    BINARY/BLOB  lower-case hex

And the differences that are NOT expected are named too, because Oracle's list
does not transfer: MySQL distinguishes '' from NULL exactly as PostgreSQL does,
so `empty_string_is_null` would mask real loss here and is never applied.

Read-only on both sides. Every statement is a SELECT or a session SET.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from .context import EXPECTED, MATCH, MISMATCH, NOT_COMPARABLE, Ctx, finding

_SAFE = re.compile(r"^[A-Za-z0-9_$]+$")

NULL_MARKER = "<NULL>"

# Real differences between MySQL and PostgreSQL that a checksum would otherwise
# report as data loss -- and the ones that ARE loss, listed so nobody adds them
# to the first group later.
EXPECTED_DIFFERENCES = {
    "decimal_scale": "DECIMAL(14,2) renders 14.50 on MySQL and trim_scale gives 14.5 on "
                     "PostgreSQL; the canonical form strips trailing zeros on both sides.",
    "char_padding": "MySQL strips CHAR padding on read; PostgreSQL character(n) keeps it. "
                    "Right-trimmed on both sides.",
    "timestamp_zone": "MySQL TIMESTAMP is stored in UTC and shown in the session zone; the "
                      "target is timestamptz. Both are compared in UTC.",
    "identifier_renames": "Phase 4c renamed PostgreSQL keywords (order -> order_tbl, "
                          "group -> group_col) and Phase 7 loaded into those names.",
    "generated_columns": "Generated columns are omitted by Phase 4c until after the load, so "
                         "they are not in the checksum; their inputs are.",
}
NOT_EXPECTED = {
    "empty_string_is_null": "An Oracle-only behaviour. MySQL keeps '' distinct from NULL, as "
                            "PostgreSQL does, so a '' that arrives as NULL is data loss here.",
    "zero_date": "0000-00-00 has no PostgreSQL representation. A zero date that arrives as NULL "
                 "or as another date is a mismatch, and the fix is on the source.",
    "unsigned_overflow": "A BIGINT UNSIGNED value above 2^63-1 in a bigint column cannot load; "
                         "Phase 4c widened the value columns to numeric(20,0) for this reason.",
}


def q(name: str) -> str:
    if not _SAFE.match(name or ""):
        raise ValueError(f"refusing an unsafe identifier: {name!r}")
    return f"`{name}`"


def _pg_names(ctx: Ctx):
    """4c's own naming, so the table and column looked up are the ones it created."""
    from convert.ddl import ident
    from convert.ddl_mysql import table_ident
    return table_ident, ident


def _is_generated(c: dict) -> bool:
    extra = (c.get("extra") or "").lower()
    return "generated" in extra and "default_generated" not in extra


def _tables(ctx: Ctx) -> list[str]:
    return sorted({t["table_name"] for t in ctx.data("tables")
                   if t.get("owner") == ctx.estate and _SAFE.match(t["table_name"] or "")})


def _columns(ctx: Ctx) -> dict[str, list[dict]]:
    out = defaultdict(list)
    for c in sorted((c for c in ctx.data("columns") if c.get("owner") == ctx.estate),
                    key=lambda c: c.get("column_id") or 0):
        out[c["table_name"]].append(c)
    return out


# --------------------------------------------------------------- canonical text

def mysql_expr(col: dict) -> str:
    """One MySQL column as canonical utf8mb4 text. The same on every MySQL server."""
    name = q(col["column_name"])
    base = (col.get("data_type") or "").lower()
    ctype = (col.get("data_type_mod") or "").lower()
    txt = "CHAR CHARACTER SET utf8mb4"
    if base in ("decimal", "numeric"):
        s = f"CAST({name} AS {txt})"
        return (f"CASE WHEN LOCATE('.', {s}) > 0 THEN "
                f"TRIM(TRAILING '.' FROM TRIM(TRAILING '0' FROM {s})) ELSE {s} END")
    if base == "datetime":
        m = re.search(r"datetime\((\d)\)", ctype)
        fsp = int(m.group(1)) if m else 0
        return f"DATE_FORMAT({name}, '%Y-%m-%d %H:%i:%s{'.%f' if fsp else ''}')"
    if base == "timestamp":
        # The session is set to +00:00, so this renders UTC.
        m = re.search(r"timestamp\((\d)\)", ctype)
        fsp = int(m.group(1)) if m else 0
        return f"DATE_FORMAT({name}, '%Y-%m-%d %H:%i:%s{'.%f' if fsp else ''}')"
    if base == "date":
        return f"DATE_FORMAT({name}, '%Y-%m-%d')"
    if base == "char":
        return f"RTRIM(CONVERT({name} USING utf8mb4))"
    if base in ("binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"):
        return f"LOWER(HEX({name}))"
    if base == "json":
        return f"CAST({name} AS {txt})"
    if base in ("varchar", "text", "tinytext", "mediumtext", "longtext", "enum", "set"):
        return f"CONVERT({name} USING utf8mb4)"
    return f"CAST({name} AS {txt})"


def postgres_expr(col: dict, pg_column: str) -> str:
    """The same canonical text for the PostgreSQL column 4c created from `col`."""
    name = f'"{pg_column}"'
    base = (col.get("data_type") or "").lower()
    ctype = (col.get("data_type_mod") or "").lower()
    if base in ("decimal", "numeric"):
        return f"trim_scale({name}::numeric)::text"
    if base == "datetime":
        fsp = re.search(r"datetime\((\d)\)", ctype)
        return f"to_char({name}, 'YYYY-MM-DD HH24:MI:SS{'.US' if fsp else ''}')"
    if base == "timestamp":
        fsp = re.search(r"timestamp\((\d)\)", ctype)
        return f"to_char({name} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS{'.US' if fsp else ''}')"
    if base == "date":
        return f"to_char({name}, 'YYYY-MM-DD')"
    if base == "char":
        return f"rtrim({name}::text)"
    if base in ("binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"):
        return f"lower(encode({name}, 'hex'))"
    return f"{name}::text"


def mysql_checksum_sql(schema: str, table: str, cols: list[dict]) -> str:
    parts = ", '|', ".join(f"COALESCE({mysql_expr(c)}, '{NULL_MARKER}')" for c in cols)
    return (f"SELECT COUNT(*), COALESCE(SUM(CAST(CONV(SUBSTR(MD5(CONCAT({parts})), 1, 12), 16, 10) "
            f"AS UNSIGNED)), 0) FROM {q(schema)}.{q(table)}")


def postgres_checksum_sql(schema: str, pg_table: str, cols: list[tuple[dict, str]]) -> str:
    parts = " || '|' || ".join(f"coalesce({postgres_expr(c, n)}, '{NULL_MARKER}')" for c, n in cols)
    return (f"SELECT count(*), coalesce(sum(('x' || substr(md5({parts}), 1, 12))::bit(48)::bigint), 0) "
            f'FROM "{schema.lower()}"."{pg_table}"')


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _pair(result):
    rows = list(result) if isinstance(result, (list, tuple)) else None
    if not rows:
        return None
    first = rows[0]
    return tuple(_as_int(x) for x in first)


# --------------------------------------------------------------- level 1

_OBJECTS_SQL = """
SELECT 'TABLE', table_name FROM information_schema.tables
 WHERE table_schema = %s AND table_type = 'BASE TABLE'
UNION ALL SELECT 'VIEW', table_name FROM information_schema.views WHERE table_schema = %s
UNION ALL SELECT routine_type, routine_name FROM information_schema.routines WHERE routine_schema = %s
UNION ALL SELECT 'TRIGGER', trigger_name FROM information_schema.triggers WHERE trigger_schema = %s
UNION ALL SELECT 'EVENT', event_name FROM information_schema.events WHERE event_schema = %s
"""

_PG_TABLES_SQL = ("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                  "WHERE n.nspname = %s AND c.relkind IN ('r', 'p')")


def objects(ctx: Ctx) -> list[dict]:
    s = ctx.estate
    if ctx.cross_engine:
        table_ident, _ = _pg_names(ctx)
        src = set(_tables(ctx))
        try:
            tgt = {r[0] for r in ctx.rows(ctx.target(), _PG_TABLES_SQL, (s.lower(),))}
        except Exception as exc:  # noqa: BLE001
            return [finding(1, "tables present", NOT_COMPARABLE, str(exc).splitlines()[0])]
        missing = sorted(t for t in src if table_ident(t) not in tgt)
        return [finding(1, "tables present", MISMATCH if missing else MATCH,
                        (f"{len(missing)} source table(s) are not on the target" if missing
                         else f"all {len(src)} source tables exist on the target, under 4c's names"),
                        why=EXPECTED_DIFFERENCES["identifier_renames"],
                        evidence={"missing": missing}),
                finding(1, "other objects", EXPECTED, "routines, triggers, views and events are "
                        "counted by Phases 4b and 7, not matched by name",
                        why="Across engines a trigger becomes a function plus a trigger, views are "
                            "rewritten, and an EVENT becomes a pg_cron job or nothing (SCT 9994).")]
    src, tgt = ctx.both(_OBJECTS_SQL, (s, s, s, s, s))
    if not isinstance(src, (list, tuple)) or not isinstance(tgt, (list, tuple)):
        return [finding(1, "objects present", NOT_COMPARABLE, f"source {src}; target {tgt}"[:300])]
    a, b = {tuple(r) for r in src}, {tuple(r) for r in tgt}
    missing, extra = sorted(a - b), sorted(b - a)
    out = [finding(1, "objects present", MISMATCH if missing else MATCH,
                   (f"{len(missing)} object(s) on the source are not on the target" if missing
                    else f"all {len(a)} objects exist on the target"),
                   evidence={"missing": [f"{t} {n}" for t, n in missing],
                             "by_type": dict(Counter(t for t, _ in a))})]
    if extra:
        out.append(finding(1, "objects added", MISMATCH,
                           f"{len(extra)} object(s) exist on the target only",
                           evidence={"extra": [f"{t} {n}" for t, n in extra]}))
    return out


# --------------------------------------------------------------- level 2

_COLUMNS_SQL = """SELECT table_name, column_name, column_type, is_nullable,
       COALESCE(column_default, '<none>'), extra, COALESCE(collation_name, '')
  FROM information_schema.columns WHERE table_schema = %s"""
_KEYS_SQL = """SELECT tc.table_name, tc.constraint_type, tc.constraint_name,
       GROUP_CONCAT(k.column_name ORDER BY k.ordinal_position SEPARATOR ',')
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage k
    ON k.constraint_schema = tc.constraint_schema AND k.constraint_name = tc.constraint_name
   AND k.table_name = tc.table_name
 WHERE tc.table_schema = %s GROUP BY tc.table_name, tc.constraint_type, tc.constraint_name"""
_INDEXES_SQL = """SELECT table_name, index_name, non_unique, index_type,
       GROUP_CONCAT(column_name ORDER BY seq_in_index SEPARATOR ',')
  FROM information_schema.statistics WHERE table_schema = %s
 GROUP BY table_name, index_name, non_unique, index_type"""
_ENGINES_SQL = """SELECT table_name, engine FROM information_schema.tables
 WHERE table_schema = %s AND table_type = 'BASE TABLE'"""


def structure(ctx: Ctx) -> list[dict]:
    if ctx.cross_engine:
        return [finding(2, "structure", NOT_COMPARABLE, "structure is not compared across engines",
                        why="Phase 4c chose the target types deliberately -- int unsigned became "
                            "bigint, ENUM became text with a CHECK -- so a definition comparison "
                            "reports a difference for every such column. Levels 3 and 4 measure "
                            "whether the values survived; 4c's own compile proved the DDL.")]
    s, out = ctx.estate, []
    for label, sql in (("columns", _COLUMNS_SQL), ("keys and constraints", _KEYS_SQL),
                       ("indexes", _INDEXES_SQL)):
        src, tgt = ctx.both(sql, (s,))
        if not isinstance(src, (list, tuple)) or not isinstance(tgt, (list, tuple)):
            out.append(finding(2, label, NOT_COMPARABLE, f"source {src}; target {tgt}"[:300]))
            continue
        a, b = Counter(tuple(r) for r in src), Counter(tuple(r) for r in tgt)
        only_s, only_t = sorted(a - b), sorted(b - a)
        out.append(finding(2, label, MISMATCH if (only_s or only_t) else MATCH,
                           f"{sum(a.values())} on the source, {sum(b.values())} on the target"
                           + ("; they differ" if (only_s or only_t) else "; identical"),
                           evidence={"source_only": [" ".join(map(str, r)) for r in only_s][:30],
                                     "target_only": [" ".join(map(str, r)) for r in only_t][:30]}))
    src, tgt = ctx.both(_ENGINES_SQL, (s,))
    if isinstance(src, (list, tuple)) and isinstance(tgt, (list, tuple)):
        se, te = dict(src), dict(tgt)
        changed = {t: (se[t], te.get(t)) for t in se if se[t] != te.get(t)}
        converted = {t: v for t, v in changed.items() if v[1] == "InnoDB"}
        other = {t: v for t, v in changed.items() if t not in converted}
        out.append(finding(2, "storage engines", MISMATCH if other else (EXPECTED if converted else MATCH),
                           f"{len(se)} tables; {len(converted)} converted to InnoDB"
                           + (f", {len(other)} otherwise different" if other else ""),
                           why=("Phase 7's schema copy converts non-InnoDB tables: RDS backups and "
                                "point-in-time restore are crash-consistent only for InnoDB.")
                           if converted else "",
                           evidence={"converted": converted, "other": other}))
    return out


# --------------------------------------------------------------- level 3

def row_counts(ctx: Ctx) -> list[dict]:
    s, out = ctx.estate, []
    table_ident = _pg_names(ctx)[0] if ctx.cross_engine else None
    matched, differ, unreadable = [], [], []
    for t in _tables(ctx):
        tsql = (f'SELECT count(*) FROM "{s.lower()}"."{table_ident(t)}"' if ctx.cross_engine
                else None)
        src, tgt = ctx.both(f"SELECT COUNT(*) FROM {q(s)}.{q(t)}", target_sql=tsql)
        a, b = _pair(src), _pair(tgt)
        if a and b:
            (matched if a[0] == b[0] else differ).append((t, a[0], b[0]))
        else:
            unreadable.append(f"{t}: source {src if not a else a[0]}, target {tgt if not b else b[0]}")
        ctx.log(f"{t:<26} source {a[0] if a else '?'}  target {b[0] if b else '?'}")
    if differ:
        out.append(finding(3, "row counts", MISMATCH, f"{len(differ)} table(s) differ",
                           evidence={"differ": [f"{t}: source {x}, target {y}" for t, x, y in differ]}))
    out.append(finding(3, "row counts", MATCH if matched and not differ else NOT_COMPARABLE,
                       f"{len(matched)} of {len(matched) + len(differ)} tables match exactly"
                       if matched else "no table could be counted on both sides",
                       why="Exact COUNT(*) on both sides -- never information_schema's table_rows, "
                           "which is an InnoDB estimate that was 49,882 for a 50,000-row table.",
                       evidence={"rows": {t: x for t, x, _ in matched}}))
    if unreadable:
        out.append(finding(3, "row counts", NOT_COMPARABLE,
                           f"{len(unreadable)} table(s) could not be counted on one side",
                           evidence={"tables": unreadable}))
    return out


# --------------------------------------------------------------- level 4

def content(ctx: Ctx) -> list[dict]:
    s, out = ctx.estate, []
    if not ctx.opts.checksum:
        return [finding(4, "data content", NOT_COMPARABLE, "checksums were switched off for this run")]
    table_ident, col_ident = _pg_names(ctx) if ctx.cross_engine else (None, None)
    cols_by_table = _columns(ctx)
    same, differ, unreadable, skipped = [], [], [], []
    for t in _tables(ctx):
        cols = cols_by_table.get(t, [])
        if ctx.cross_engine:
            gen = [c["column_name"] for c in cols if _is_generated(c)]
            if gen:
                skipped.append(f"{t}: {', '.join(gen)} (generated; added after the load)")
            cols = [c for c in cols if not _is_generated(c)]
        if not cols:
            continue
        sql = mysql_checksum_sql(s, t, cols)
        tsql = (postgres_checksum_sql(s, table_ident(t), [(c, col_ident(c["column_name"]))
                                                          for c in cols])
                if ctx.cross_engine else None)
        src, tgt = ctx.both(sql, target_sql=tsql)
        a, b = _pair(src), _pair(tgt)
        if not a or not b:
            unreadable.append(f"{t}: source {src if not a else 'ok'}, target {tgt if not b else 'ok'}"[:300])
            continue
        ok = a == b
        (same if ok else differ).append(f"{t}: {a[0]} rows, checksum {a[1]}"
                                        + ("" if ok else f" vs target {b[0]} rows, checksum {b[1]}"))
        ctx.log(f"{t:<26} {a[0]:>9} rows  checksum {'match' if ok else 'DIFFER'}")
    if differ:
        out.append(finding(4, "data content", MISMATCH, f"{len(differ)} table(s) hold different values",
                           evidence={"differ": differ}))
    out.append(finding(4, "data content", MATCH if same and not differ else NOT_COMPARABLE,
                       f"{len(same)} table(s) match on a checksum of every row" if same
                       else "no table could be checksummed on both sides",
                       why=("MD5 over a canonical text of each row, summed; the same SQL on both "
                            "servers." if not ctx.cross_engine else
                            "MD5 over a canonical text per engine, keyed on the MySQL type: "
                            "DECIMAL trailing zeros, CHAR padding and TIMESTAMP zones are "
                            "normalised on both sides; '' and NULL are NOT treated as equal."),
                       evidence={"checked": same}))
    if ctx.cross_engine:
        out.append(finding(4, "differences that are correct", EXPECTED,
                           f"{len(EXPECTED_DIFFERENCES)} normalised; {len(NOT_EXPECTED)} deliberately not",
                           why="Oracle's list does not transfer: MySQL keeps '' distinct from NULL, so "
                               "declaring it expected here would hide data loss.",
                           evidence={"normalised": EXPECTED_DIFFERENCES, "never_normalised": NOT_EXPECTED}))
    if skipped:
        out.append(finding(4, "columns not checksummed", EXPECTED,
                           f"{len(skipped)} table(s) with generated columns not on the target yet",
                           why=EXPECTED_DIFFERENCES["generated_columns"], evidence={"skipped": skipped}))
    if unreadable:
        out.append(finding(4, "data content", NOT_COMPARABLE,
                           f"{len(unreadable)} table(s) unreadable on one side", evidence={"tables": unreadable}))
    return out


# --------------------------------------------------------------- level 5

_SERVER_SQL = ("SELECT @@sql_mode, @@character_set_server, @@collation_server, "
               "@@event_scheduler, @@lower_case_table_names, @@explicit_defaults_for_timestamp")
_COUNTERS_SQL = """SELECT table_name, auto_increment FROM information_schema.tables
 WHERE table_schema = %s AND auto_increment IS NOT NULL"""
_EVENTS_SQL = "SELECT event_name, status FROM information_schema.events WHERE event_schema = %s"


def behaviour(ctx: Ctx) -> list[dict]:
    s, out = ctx.estate, []
    if ctx.cross_engine:
        return _identity_positions(ctx)

    src, tgt = ctx.both(_SERVER_SQL)
    names = ("sql_mode", "character_set_server", "collation_server", "event_scheduler",
             "lower_case_table_names", "explicit_defaults_for_timestamp")
    if isinstance(src, (list, tuple)) and isinstance(tgt, (list, tuple)) and src and tgt:
        a, b = dict(zip(names, map(str, src[0]))), dict(zip(names, map(str, tgt[0])))
        diff = {k: (a[k], b[k]) for k in names if a[k] != b[k]}
        out.append(finding(5, "server settings", MISMATCH if diff else MATCH,
                           "the target runs with the source's settings" if not diff
                           else f"{len(diff)} setting(s) differ from the source",
                           why="Carried by Phase 6's parameter group; a difference changes what the "
                               "application's SQL does.", evidence={"differ": diff}))

    src, tgt = ctx.both(_COUNTERS_SQL, (s,))
    if isinstance(src, (list, tuple)) and isinstance(tgt, (list, tuple)):
        a, b = {r[0]: _as_int(r[1]) for r in src}, {r[0]: _as_int(r[1]) for r in tgt}
        behind = {t: (a[t], b.get(t)) for t in a if (b.get(t) or 0) < (a[t] or 0)}
        out.append(finding(5, "AUTO_INCREMENT counters", MISMATCH if behind else MATCH,
                           f"{len(a)} counter(s); " + ("all at or ahead of the source" if not behind
                                                      else f"{len(behind)} behind the source"),
                           why="A counter behind the source reissues ids the source already handed "
                               "out. Phase 7's residue sets them." if behind else "",
                           evidence={"behind": behind}))

    src, tgt = ctx.both(_EVENTS_SQL, (s,))
    if isinstance(src, (list, tuple)) and isinstance(tgt, (list, tuple)):
        a, b = dict(src), dict(tgt)
        for name, status in a.items():
            if name not in b:
                out.append(finding(5, "event", MISMATCH, f"{name} is not on the target"))
            elif status == "ENABLED" and b[name] != "ENABLED":
                out.append(finding(5, "event", EXPECTED, f"{name} is {b[name]} on the target",
                                   why="Created disabled by Phase 7 so it cannot run during the "
                                       "migration; enabled at cutover (dms.schema_mysql --apply cutover)."))
            else:
                out.append(finding(5, "event", MATCH, f"{name} {b[name]}"))

    # A routine must still run, not just exist.
    for name, sql in (("routine executes", f"SELECT {q(s)}.`fn_legacy_tax_rate`('GB')"),):
        a, b = ctx.both(sql)
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            out.append(finding(5, name, MATCH if list(a) == list(b) else MISMATCH,
                               f"fn_legacy_tax_rate('GB'): source {a[0][0]}, target {b[0][0]}"))
        else:
            out.append(finding(5, name, NOT_COMPARABLE, f"source {a}; target {b}"[:200]))

    out.append(finding(5, "definers", EXPECTED,
                       "views, routines, triggers and events are owned by the applying user",
                       why="RDS grants no SUPER or SET_USER_ID, so an object cannot be created for "
                           "root@localhost; Phase 7 removed the DEFINER clause. SQL SECURITY is "
                           "unchanged."))
    return out


def _identity_positions(ctx: Ctx) -> list[dict]:
    """Each identity on PostgreSQL must be at or past the source's AUTO_INCREMENT."""
    table_ident, col_ident = _pg_names(ctx)
    s = ctx.estate
    counters = [r for r in ctx.data("sequences") if (r.get("sequence_owner") or r.get("owner")) == s
                and r.get("table_name") and r.get("column_name")]
    behind, ok, unreadable = [], [], []
    for r in counters:
        src_next = _as_int(r.get("last_number"))
        tbl, col = table_ident(r["table_name"]), col_ident(r["column_name"])
        try:
            # The value the identity will hand out next. Not pg_sequence_last_value:
            # after RESTART WITH n the sequence is "not yet called" and that
            # function returns NULL -- which read a correctly restarted identity
            # as sitting at 1. last_value and is_called together are exact.
            seq = ctx.rows(ctx.target(), "SELECT pg_get_serial_sequence(%s, %s)",
                           (f"{s.lower()}.{tbl}", col))
            seq_name = seq[0][0] if seq else None
            if not seq_name:
                raise RuntimeError("no identity sequence")
            last, called = ctx.rows(ctx.target(),
                                    f"SELECT last_value, is_called FROM {seq_name}")[0]
            tgt_next = _as_int(last) + (1 if called else 0)
        except Exception as exc:  # noqa: BLE001
            unreadable.append(f"{tbl}.{col}: {str(exc).splitlines()[0][:80]}")
            try:
                ctx.target().rollback()
            except Exception:  # noqa: BLE001
                pass
            continue
        # It must not be below the value the source would have handed out.
        if src_next and tgt_next < src_next:
            behind.append(f"{tbl}.{col}: target next {tgt_next}, source next {src_next}")
        else:
            ok.append(f"{tbl}.{col}")
    out = [finding(5, "identity positions", MISMATCH if behind else (MATCH if ok else NOT_COMPARABLE),
                   f"{len(counters)} identity column(s); "
                   + (f"{len(behind)} would reissue a migrated key" if behind else "all at or past the source"),
                   why="An identity starts at 1 however many rows DMS loads. Phase 7's residue "
                       "restarts each one; until it runs, the first insert after cutover collides.",
                   evidence={"behind": behind})]
    if unreadable:
        out.append(finding(5, "identity positions", NOT_COMPARABLE,
                           f"{len(unreadable)} could not be read", evidence={"columns": unreadable}))
    return out
