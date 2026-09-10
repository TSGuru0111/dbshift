from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# Tables Oracle manages on the user's behalf. They are rebuilt by recreating the
# Text index, queue or mview log -- never migrated, and never assessed. Without
# this exclusion every structural rule fires on DR$ internals as a false positive.
INTERNAL_PATTERNS = ("DR$%", "AQ$%", "MLOG$%", "RUPD$%", "SYS_IOT%")

# Datasets that can come back empty. Without a declared schema their table would
# have no columns and every rule referencing them would fail to parse.
EMPTY_DATASET_COLUMNS = {
    "column_profile": [
        "collector_run_id", "owner", "table_name", "column_name", "actual_rows",
        "scanned_rows", "sampled", "sample_pct", "duplicate_count", "non_ascii_count",
        "checked_duplicates", "checked_non_ascii",
    ],
    "table_profile": [
        "collector_run_id", "owner", "table_name", "estimated_rows", "actual_rows",
        "scanned_rows", "sampled", "sample_pct", "profiled", "skip_reason",
    ],
    "plsql_errors": [
        "collector_run_id", "owner", "name", "type", "sequence", "line", "position",
        "text", "attribute", "message_number",
    ],
    "invalid_objects": [
        "collector_run_id", "owner", "object_name", "object_type", "status", "last_ddl_time",
    ],
    "external_tables": [
        "collector_run_id", "owner", "table_name", "type_owner", "type_name",
        "default_directory_owner", "default_directory_name", "reject_limit", "access_type",
    ],
}


SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1


def _sqlite_value(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return 1 if v else 0
    # Oracle NUMBER outruns a 64-bit integer -- a sequence MAXVALUE defaults to
    # 28 nines. Keep it as text rather than losing or truncating the value.
    if isinstance(v, int) and not (SQLITE_INT_MIN <= v <= SQLITE_INT_MAX):
        return str(v)
    return v


def _table_name(dataset: str) -> str:
    return dataset.split(".")[-1]


def load_run(run_dir: Path, db_path: Path) -> dict:
    """Load one collector run into SQLite -- the local stand-in for Aurora.

    Rules are SQL against these tables, so the same predicates port to the
    metadata repository later with little more than a dialect change.
    """
    if db_path.exists():
        db_path.unlink()
    # Closed before returning. A leaked handle is invisible in a CLI that exits
    # straight after, but in a long-running server it keeps the file locked and
    # the next run's unlink() fails on Windows.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _load(conn, run_dir)
    finally:
        conn.close()


def _load(conn: sqlite3.Connection, run_dir: Path) -> dict:

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    loaded: dict[str, int] = {}

    for entry in manifest["datasets"]:
        payload = json.loads((run_dir / entry["file"]).read_text(encoding="utf-8"))
        table = _table_name(payload["dataset"])
        rows = payload["rows"]

        seen: list[str] = []
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.append(key)
        if not seen:
            # A dataset can be legitimately empty -- nothing profilable, or the
            # privilege was missing. Rules must still parse against it, so fall
            # back to the declared schema rather than a bare stub.
            seen = EMPTY_DATASET_COLUMNS.get(table, ["collector_run_id"])

        cols = ", ".join(f'"{c}"' for c in seen)
        conn.execute(f'CREATE TABLE "{table}" ({cols})')
        if rows:
            placeholders = ", ".join("?" for _ in seen)
            # Generator, not a list comprehension: executemany consumes it lazily,
            # so the converted copy never coexists with the parsed rows.
            conn.executemany(
                f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders})',
                ([_sqlite_value(r.get(c)) for c in seen] for r in rows),
            )
        loaded[table] = len(rows)
        del payload, rows

    _create_indexes(conn, loaded)
    _create_views(conn, loaded)
    conn.commit()
    return {
        "collector_run_id": manifest["collector_run_id"],
        "tables": loaded,
        "total_rows": sum(loaded.values()),
        "source": manifest.get("source", {}),
        "schemas": manifest.get("schemas", {}),
    }


# Join keys the rule catalogue actually uses. Without these, the NOT EXISTS and
# self-join rules (DQ-001, PERF-001, PERF-008) degrade to quadratic scans -- fine
# at 90 objects, minutes per rule at 50,000.
RULE_INDEXES = {
    "objects": [("owner", "object_type"), ("object_name",)],
    "tables": [("owner", "table_name")],
    "columns": [("owner", "table_name"), ("data_type",)],
    "indexes": [("table_owner", "table_name"), ("owner", "index_name")],
    "index_columns": [
        ("table_owner", "table_name", "column_name", "column_position"),
        ("index_owner", "index_name"),
        ("table_name", "column_name", "column_position"),
    ],
    "constraints": [
        ("owner", "table_name", "constraint_type"),
        ("owner", "constraint_name"),
    ],
    "constraint_columns": [("owner", "constraint_name"), ("owner", "table_name")],
    "segments": [("owner", "segment_name", "segment_type")],
    "lobs": [("owner", "table_name")],
    "partitioned_tables": [("owner", "table_name")],
    "table_profile": [("owner", "table_name")],
    "column_profile": [("owner", "table_name")],
    "materialized_views": [("container_name",)],
    "queues": [("queue_table",)],
    "external_tables": [("owner", "table_name")],
    "table_privileges": [("grantee",)],
    "feature_usage": [("name",)],
}


def _create_indexes(conn: sqlite3.Connection, loaded: dict[str, int]) -> None:
    for table, keysets in RULE_INDEXES.items():
        if not loaded.get(table):
            continue
        existing = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        for i, keys in enumerate(keysets):
            if not set(keys).issubset(existing):
                continue
            cols = ", ".join(f'"{k}"' for k in keys)
            conn.execute(f'CREATE INDEX IF NOT EXISTS "ix_{table}_{i}" ON "{table}" ({cols})')
    conn.execute("ANALYZE")


def _create_views(conn: sqlite3.Connection, loaded: dict[str, int]) -> None:
    """Analysis views. Rules query these, not the raw tables.

    Centralising the 'what counts as a user object' definition here keeps it out
    of 48 separate rule predicates, where it would drift.
    """
    not_internal = " AND ".join(f"t.table_name NOT LIKE '{p}'" for p in INTERNAL_PATTERNS)
    obj_not_internal = " AND ".join(f"o.object_name NOT LIKE '{p}'" for p in INTERNAL_PATTERNS)

    container_clause = ""
    if loaded.get("materialized_views"):
        container_clause = (
            " AND t.table_name NOT IN "
            "(SELECT container_name FROM materialized_views WHERE container_name IS NOT NULL)"
        )
    queue_clause = ""
    if loaded.get("queues"):
        queue_clause = (
            " AND t.table_name NOT IN "
            "(SELECT queue_table FROM queues WHERE queue_table IS NOT NULL)"
        )
    external_clause = ""
    if loaded.get("external_tables"):
        external_clause = (
            " AND t.table_name NOT IN "
            "(SELECT table_name FROM external_tables WHERE table_name IS NOT NULL)"
        )

    conn.execute(
        f"""CREATE VIEW v_user_tables AS
            SELECT t.* FROM tables t
            WHERE {not_internal}{container_clause}{queue_clause}{external_clause}"""
    )
    conn.execute(
        f"""CREATE VIEW v_user_objects AS
            SELECT o.* FROM objects o WHERE {obj_not_internal}"""
    )
    conn.execute(
        """CREATE VIEW v_user_columns AS
           SELECT c.* FROM columns c
           WHERE c.table_name IN (SELECT table_name FROM v_user_tables)"""
    )
