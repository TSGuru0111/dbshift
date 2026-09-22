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


def extract(conn: sqlite3.Connection, measured: dict | None = None) -> dict:
    conn.row_factory = sqlite3.Row

    features = [
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

    return {
        "segment_bytes": int(segment_bytes or 0),
        "segment_gb": round((segment_bytes or 0) / 1024**3, 3),
        "table_count": _scalar(conn, "SELECT COUNT(*) FROM v_user_tables"),
        "total_table_count": _scalar(conn, "SELECT COUNT(*) FROM tables"),
        "object_count": _scalar(conn, "SELECT COUNT(*) FROM objects"),
        "largest_segment_gb": round(
            (_scalar(conn, "SELECT MAX(bytes) FROM segments") or 0) / 1024**3, 3
        ),
        "features_detected": features,
        "structural": structural,
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
