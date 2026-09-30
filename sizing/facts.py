"""Pull the sizing inputs out of a loaded collector run."""

from __future__ import annotations

import sqlite3

# Hourly snapshots, so roughly one day. Below this there is no distribution to
# take a percentile of, whatever the tooling reports.
MIN_AWR_SNAPSHOTS = 24


def _scalar(conn, sql, default=0):
    row = conn.execute(sql).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,)).fetchone())


def _mysql_facts(conn) -> dict:
    """The evidence a MySQL estate's two paths are weighed on.

    None of this exists in Oracle's feature-usage catalogue, and that is the
    point: a MySQL assessment turns on storage engines, character sets and
    column types, not on licensed options. Every figure is a count read from a
    dataset the collector already produced -- nothing is inferred.

    Zero dates are deliberately NOT here. They are a property of the DATA, not
    the catalogue, and cannot be counted without scanning every date column;
    AWS SCT reports them (action item 8825) and Phase 8's checksum catches them.
    """
    def n(sql: str) -> int:
        try:
            return int(_scalar(conn, sql) or 0)
        except sqlite3.Error:
            return 0

    has = lambda t: _table_exists(conn, t)   # noqa: E731
    try:
        value_names = [f"{r[0]}.{r[1]}" for r in conn.execute(
            "SELECT table_name, column_name FROM v_user_columns "
            "WHERE UPPER(data_type) = 'BIGINT' AND LOWER(data_type_mod) LIKE '%unsigned%' "
            "AND COALESCE(identity_column, 'NO') <> 'YES' "
            "AND NOT EXISTS (SELECT 1 FROM constraint_columns cc "
            "  WHERE cc.table_name = v_user_columns.table_name "
            "    AND cc.column_name = v_user_columns.column_name "
            "    AND cc.referenced_table_name IS NOT NULL) "
            "ORDER BY table_name, column_name")]
    except sqlite3.Error:
        value_names = []
    return {
        # Named, not just counted: the catalogue cannot tell a balance from an id
        # that simply lacks a foreign key, and a client can, in a minute each.
        "unsigned_bigint_value_names": value_names,
        "non_innodb_tables": n("SELECT COUNT(*) FROM mysql_table_storage "
                               "WHERE needs_engine_change = 'YES'") if has("mysql_table_storage") else 0,
        "utf8mb3_columns": n("SELECT COUNT(*) FROM mysql_charsets "
                             "WHERE is_utf8mb3 = 'YES'") if has("mysql_charsets") else 0,
        "case_insensitive_columns": n("SELECT COUNT(*) FROM mysql_charsets "
                                      "WHERE case_insensitive = 'YES'") if has("mysql_charsets") else 0,
        "unsigned_bigint_columns": n("SELECT COUNT(*) FROM v_user_columns "
                                     "WHERE UPPER(data_type) = 'BIGINT' "
                                     "AND LOWER(data_type_mod) LIKE '%unsigned%'"),
        # The unsigned BIGINTs that are NOT auto-increment identities. An identity
        # counts up from 1 and will not reach 2^63 in any plausible lifetime, so it
        # maps to PostgreSQL bigint safely; a value column (a balance, a hash, an
        # external id) can already hold something bigint cannot represent. Counting
        # every identity as a risk put 29 points on the PostgreSQL path for an
        # estate with ONE genuinely risky column.
        "unsigned_bigint_values": n("SELECT COUNT(*) FROM v_user_columns "
                                    "WHERE UPPER(data_type) = 'BIGINT' "
                                    "AND LOWER(data_type_mod) LIKE '%unsigned%' "
                                    "AND COALESCE(identity_column, 'NO') <> 'YES' "
                                    # A foreign key holds only what its parent's identity
                                    # generated, so it is exactly as safe as the identity.
                                    "AND NOT EXISTS (SELECT 1 FROM constraint_columns cc "
                                    "  WHERE cc.table_name = v_user_columns.table_name "
                                    "    AND cc.column_name = v_user_columns.column_name "
                                    "    AND cc.referenced_table_name IS NOT NULL)"),
        "enum_set_columns": n("SELECT COUNT(*) FROM v_user_columns "
                              "WHERE UPPER(data_type) IN ('ENUM','SET')"),
        "json_columns": n("SELECT COUNT(*) FROM v_user_columns WHERE UPPER(data_type) = 'JSON'"),
        "tables_without_pk": n("SELECT COUNT(*) FROM mysql_tables_without_pk")
                             if has("mysql_tables_without_pk") else 0,
        "routines": n("SELECT COUNT(*) FROM mysql_routines") if has("mysql_routines") else 0,
        "triggers": n("SELECT COUNT(*) FROM triggers") if has("triggers") else 0,
        "events": n("SELECT COUNT(*) FROM scheduler_jobs") if has("scheduler_jobs") else 0,
        "fulltext_indexes": n("SELECT COUNT(*) FROM indexes WHERE index_type = 'FULLTEXT'"),
        # A definer RDS can recreate is fine; root@localhost is not -- RDS grants
        # no SUPER, so the object fails at runtime on the target.
        "unrecreatable_definers": (
            (n("SELECT COUNT(*) FROM mysql_routines WHERE security_type = 'DEFINER' "
               "AND (definer LIKE 'root@%' OR definer LIKE '%@localhost')")
             if has("mysql_routines") else 0)
            + (n("SELECT COUNT(*) FROM views WHERE security_type = 'DEFINER' "
                 "AND (definer LIKE 'root@%' OR definer LIKE '%@localhost')")
               if has("views") else 0)),
    }


def extract(conn: sqlite3.Connection, measured: dict | None = None,
            source_engine: str | None = None) -> dict:
    conn.row_factory = sqlite3.Row
    is_mysql = str(source_engine or "").upper() == "MYSQL"

    # On MySQL this dataset holds configuration facts, not licensed options, and
    # is not used -- so it is not read, which also keeps Phase 3 working on a
    # MySQL run collected before the probe emitted Oracle's full column shape.
    features = [] if is_mysql else [
        dict(r)
        for r in conn.execute(
            """SELECT name, detected_usages, currently_used, last_usage_date
               FROM feature_usage WHERE detected_usages > 0 ORDER BY name"""
        )
    ]
    segment_bytes = _scalar(conn, "SELECT SUM(bytes) FROM segments")
    structural = {
        "partitioned_tables": _scalar(conn, "SELECT COUNT(*) FROM partitioned_tables"),
        "bitmap_indexes": _scalar(
            conn, "SELECT COUNT(*) FROM indexes WHERE index_type LIKE '%BITMAP%'"
        ),
        "compressed_tables": _scalar(
            conn, "SELECT COUNT(*) FROM tables WHERE compression = 'ENABLED'"
        ),
    }

    db = conn.execute("SELECT * FROM database").fetchone()
    if is_mysql:
        # MySQL has no nls_parameters; the server character set is on the
        # identity row. Reading NLS_CHARACTERSET here returned UNKNOWN and made
        # the encoding check warn on every MySQL estate.
        charset = (db["character_set_server"]
                   if db is not None and "character_set_server" in db.keys() else None) or "UNKNOWN"
    else:
        charset = _scalar(
            conn,
            "SELECT value FROM nls_parameters WHERE parameter = 'NLS_CHARACTERSET'",
            default="UNKNOWN",
        )

    # Utilization signal, or the honest absence of one. XE leaves v$sysmetric
    # empty, caps itself at 2 threads and reports the host's CPU count rather
    # than the database's, so none of it can size an instance.
    sysmetric_rows = _scalar(conn, "SELECT COUNT(*) FROM system_metrics")
    awr = conn.execute("SELECT * FROM awr_coverage").fetchone()
    awr_snapshots = (awr["snapshot_count"] if awr else 0) or 0

    # Percentile-based sizing needs live metrics AND enough history to have
    # percentiles at all. A handful of AWR snapshots is not a distribution, and
    # AWR on an edition without Diagnostic Pack should not be relied on anyway.
    # Both conditions must hold, or the sizing is capacity-derived and says so.
    # An external measured feed -- an AWS OLA / DB OLA export, Migration
    # Evaluator, AWR or vendor monitoring -- supersedes what the source can tell
    # us about itself, because the source cannot tell us anything about load.
    if measured and measured.get("usable_for_sizing"):
        return_util = {
            "sysmetric_rows": sysmetric_rows,
            "awr_snapshots": awr_snapshots,
            "available": True,
            "basis": "measured",
            "reason": f"external feed -- {measured['reason']}",
            "measured": measured,
        }
    else:
        return_util = None

    has_metrics = sysmetric_rows > 0
    has_history = awr_snapshots >= MIN_AWR_SNAPSHOTS
    if has_metrics and has_history:
        util_reason = f"{sysmetric_rows} live metrics, {awr_snapshots} AWR snapshots"
    elif not has_metrics:
        util_reason = (
            f"v$sysmetric returned 0 rows, so there is no live metric signal "
            f"(AWR holds {awr_snapshots} snapshots, which cannot substitute)"
        )
    else:
        util_reason = (
            f"AWR holds {awr_snapshots} snapshots, below the {MIN_AWR_SNAPSHOTS} "
            "needed before a percentile means anything"
        )
    if is_mysql:
        # v$sysmetric and AWR are Oracle names. On a MySQL run the collector emits
        # both datasets empty, so the Oracle wording above always fired -- and a
        # model sizing the estate repeated "0 rows from v$sysmetric, 0 AWR
        # snapshots" back to a MySQL client (measured 2026-09-29).
        util_reason = (
            "MySQL keeps no workload history this collector reads -- performance_schema "
            "is not sampled over time -- so there is no measured load signal. Supply a "
            "utilization file to size from load rather than capacity"
        )

    return {
        "segment_bytes": int(segment_bytes or 0),
        "segment_gb": round((segment_bytes or 0) / 1024**3, 3),
        "table_count": _scalar(conn, "SELECT COUNT(*) FROM v_user_tables"),
        "total_table_count": _scalar(conn, "SELECT COUNT(*) FROM tables"),
        "object_count": _scalar(conn, "SELECT COUNT(*) FROM objects"),
        "largest_segment_gb": round(
            (_scalar(conn, "SELECT MAX(bytes) FROM segments") or 0) / 1024**3, 3
        ),
        # Oracle's licensed-option catalogue. On MySQL the collector fills the
        # same dataset with a few configuration facts (binary logging, GTID),
        # which have no edition meaning -- so it is emptied rather than fed to
        # the Oracle edition arithmetic.
        "features_detected": [] if is_mysql else features,
        "structural": structural,
        "source_engine": "MYSQL" if is_mysql else "ORACLE",
        "mysql": _mysql_facts(conn) if is_mysql else None,
        "character_set": charset,
        "log_mode": db["log_mode"] if db else None,
        "supplemental_logging": db["supplemental_log_data_min"] if db else None,
        "utilization": return_util
        or {
            "sysmetric_rows": sysmetric_rows,
            "awr_snapshots": awr_snapshots,
            "available": has_metrics and has_history,
            "basis": "capacity",
            "reason": util_reason,
            "measured": measured,
        },
    }
