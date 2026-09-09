from __future__ import annotations

import os
import re

from ..db import in_binds

NAME = "dataprofile"

# Oracle-managed internals. Rebuilt by recreating the Text index, queue or mview
# log -- profiling them produces noise, not findings.
INTERNAL_PREFIXES = ("DR$", "AQ$", "MLOG$", "RUPD$", "SYS_IOT")

TEXT_TYPES = ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR")

# Stats propose, the scan disposes: a column the optimizer believes is ~unique
# is worth one real duplicate check. Catches a near-key even when stats are stale.
NEAR_UNIQUE_RATIO = 0.95

IDENTIFIER = re.compile(r"^[A-Za-z0-9_$#]+$")


def _max_rows() -> int:
    try:
        return int(os.environ.get("DBSHIFT_PROFILE_MAX_ROWS", "2000000"))
    except ValueError:
        return 2_000_000


def _quote(name: str) -> str:
    # Identifiers come from the catalog, never from user input, but they are
    # interpolated rather than bound -- Oracle cannot bind an identifier.
    if not IDENTIFIER.match(name):
        raise ValueError(f"refusing to interpolate unexpected identifier: {name!r}")
    return f'"{name}"'


def _is_internal(table_name: str) -> bool:
    return any(table_name.startswith(p) for p in INTERNAL_PREFIXES)


def collect(s, owners):
    frag, binds = in_binds("o", owners)
    tables = s.fetch(
        "dataprofile.tables",
        f"""SELECT owner, table_name, num_rows
            FROM dba_tables
            WHERE owner IN ({frag})
            ORDER BY owner, table_name""",
        binds,
    )
    columns = s.fetch(
        "dataprofile.columns",
        f"""SELECT owner, table_name, column_name, data_type, data_scale, nullable,
                   num_distinct, num_nulls
            FROM dba_tab_cols
            WHERE owner IN ({frag}) AND user_generated = 'YES' AND hidden_column = 'NO'
            ORDER BY owner, table_name, column_id""",
        binds,
    )
    keyed_rows = s.fetch(
        "dataprofile.key_columns",
        f"""SELECT cc.owner, cc.table_name, cc.column_name
            FROM dba_cons_columns cc
            JOIN dba_constraints c
              ON c.owner = cc.owner AND c.constraint_name = cc.constraint_name
            WHERE cc.owner IN ({frag}) AND c.constraint_type IN ('P','U')""",
        binds,
    )

    external = {
        (r["owner"], r["table_name"])
        for r in s.fetch(
            "dataprofile.external_tables",
            f"SELECT owner, table_name FROM dba_external_tables WHERE owner IN ({frag})",
            binds,
        )
    }
    # Materialized view containers hold derived data. Duplicates in an aggregate
    # are arithmetic, not a data-quality defect.
    containers = {
        r["container_name"]
        for r in s.fetch(
            "dataprofile.mview_containers",
            f"SELECT container_name FROM dba_mviews WHERE owner IN ({frag})",
            binds,
        )
        if r["container_name"]
    }
    enforced = {(r["owner"], r["table_name"], r["column_name"]) for r in keyed_rows}
    by_table: dict[tuple[str, str], list[dict]] = {}
    for c in columns:
        by_table.setdefault((c["owner"], c["table_name"]), []).append(c)

    max_rows = _max_rows()
    table_profile: list[dict] = []
    column_profile: list[dict] = []
    unreadable: set[str] = set()

    for t in tables:
        owner, table = t["owner"], t["table_name"]
        est_rows = t["num_rows"]
        skip = None
        if _is_internal(table):
            skip = "oracle_internal_object"
        elif table in containers:
            skip = "materialized_view_container"
        elif (owner, table) in external:
            skip = "external_table"
        elif owner in unreadable:
            skip = "no_select_privilege"
        elif est_rows is not None and est_rows > max_rows:
            skip = f"estimated_rows_above_cap_{max_rows}"

        if skip:
            table_profile.append(
                {
                    "owner": owner,
                    "table_name": table,
                    "estimated_rows": est_rows,
                    "actual_rows": None,
                    "profiled": "N",
                    "skip_reason": skip,
                }
            )
            continue

        cols = by_table.get((owner, table), [])
        dup_targets, text_targets = [], []
        for c in cols:
            name, dtype = c["column_name"], c["data_type"]
            if not IDENTIFIER.match(name):
                continue
            if dtype in TEXT_TYPES:
                text_targets.append(name)
            nd, nr = c["num_distinct"], est_rows
            # No statistics -> no cardinality signal, so no duplicate candidates.
            # The non-ASCII scan still runs; it needs no prior knowledge.
            near_unique = bool(nd and nr and (nd / nr) >= NEAR_UNIQUE_RATIO)
            # A natural key is text or a whole number. A decimal is a measure --
            # near-unique money columns are arithmetic coincidence, not a key.
            keyable = dtype in TEXT_TYPES or (dtype == "NUMBER" and c["data_scale"] == 0)
            if keyable and near_unique and (owner, table, name) not in enforced:
                dup_targets.append(name)

        selects = ["COUNT(*) AS row_count"]
        for name in dup_targets:
            q = _quote(name)
            selects.append(f'COUNT({q}) - COUNT(DISTINCT {q}) AS "DUP__{name}"')
        for name in text_targets:
            q = _quote(name)
            selects.append(
                f"SUM(CASE WHEN {q} IS NOT NULL AND INSTR(ASCIISTR({q}), '\\') > 0 "
                f'THEN 1 ELSE 0 END) AS "NA__{name}"'
            )

        sql = f'SELECT {", ".join(selects)} FROM {_quote(owner)}.{_quote(table)}'
        result = s.fetch(f"dataprofile.scan.{owner}.{table}", sql)
        if not result:
            error = s.query_log[-1].error or ""
            # ORA-00942 here means SELECT_CATALOG_ROLE without data access: the
            # account can read metadata about the table but not its rows. Stop
            # scanning this owner rather than emitting one failure per table.
            reason = "no_select_privilege" if "ORA-00942" in error else "scan_failed"
            if reason == "no_select_privilege":
                unreadable.add(owner)
            table_profile.append(
                {
                    "owner": owner,
                    "table_name": table,
                    "estimated_rows": est_rows,
                    "actual_rows": None,
                    "profiled": "N",
                    "skip_reason": reason,
                }
            )
            continue

        row = result[0]
        actual = row["row_count"]
        table_profile.append(
            {
                "owner": owner,
                "table_name": table,
                "estimated_rows": est_rows,
                "actual_rows": actual,
                "profiled": "Y",
                "skip_reason": None,
            }
        )

        measured = {n: None for n in set(dup_targets) | set(text_targets)}
        for name in measured:
            column_profile.append(
                {
                    "owner": owner,
                    "table_name": table,
                    "column_name": name,
                    "actual_rows": actual,
                    "duplicate_count": row.get(f"dup__{name}".lower()),
                    "non_ascii_count": row.get(f"na__{name}".lower()),
                    "checked_duplicates": "Y" if name in dup_targets else "N",
                    "checked_non_ascii": "Y" if name in text_targets else "N",
                }
            )

    column_profile.sort(key=lambda r: (r["owner"], r["table_name"], r["column_name"]))
    return {
        "source_inventory.table_profile": table_profile,
        "source_inventory.column_profile": column_profile,
    }
