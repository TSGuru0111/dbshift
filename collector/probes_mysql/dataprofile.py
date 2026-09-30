"""Row-level profiling -- MySQL's answer to `probes/dataprofile.py`.

The only probe that reads user data rather than the catalogue, and the only one
whose cost scales with the estate. Emits `table_profile` and `column_profile`
under their Oracle names, because three rules read `table_profile` and two read
`column_profile`.

**The exact row count comes from here, never from `information_schema`.**
`TABLES.table_rows` is InnoDB's sampled estimate and is routinely 20-50% wrong;
Phase 8 compares counts and would report a mismatch on a perfect migration if it
trusted it. `tables.py` keeps the estimate in `num_rows` because Oracle's column
means the same kind of thing, and this probe produces `actual_rows` by counting.

**Sampling.** Same bound and the same environment overrides as the Oracle probe,
so an operator who knows `DBSHIFT_PROFILE_MAX_ROWS` does not have to learn a
second name. MySQL has no `SAMPLE` clause, so a sampled scan uses a modulus on
the primary key where there is a single integer PK, and falls back to `LIMIT`
otherwise -- which is a *prefix*, not a sample, and is labelled as one. A prefix
reported as a sample would be a lie about coverage, and the duplicate counts
built on it would be worthless.
"""

from __future__ import annotations

import os
import re

NAME = "dataprofile"

TEXT_TYPES = ("VARCHAR", "CHAR", "TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT")

# Stats propose, the scan disposes -- same reasoning as the Oracle probe.
NEAR_UNIQUE_RATIO = 0.95

# MySQL identifiers allow rather more than Oracle's, including spaces inside
# backticks. Deliberately restrictive: these come from the catalogue, but they
# are interpolated rather than bound (no engine can bind an identifier), so the
# guard is what keeps that safe.
IDENTIFIER = re.compile(r"^[A-Za-z0-9_$]+$")


def _max_rows() -> int:
    try:
        return int(os.environ.get("DBSHIFT_PROFILE_MAX_ROWS", "2000000"))
    except ValueError:
        return 2_000_000


def _sample_target() -> int:
    try:
        return int(os.environ.get("DBSHIFT_PROFILE_SAMPLE_ROWS", "1000000"))
    except ValueError:
        return 1_000_000


def _quote(name: str) -> str:
    if not IDENTIFIER.match(str(name or "")):
        raise ValueError(f"refusing to interpolate unexpected identifier: {name!r}")
    return f"`{name}`"


def collect(s, owners):
    frag, binds = s.binds("o", owners)

    candidates = s.fetch(
        "dataprofile.candidates",
        f"""SELECT t.table_schema AS owner, t.table_name, t.table_rows AS est_rows,
                   t.engine
            FROM information_schema.tables t
            WHERE t.table_schema IN ({frag})
              AND t.table_type = 'BASE TABLE'
            ORDER BY t.table_schema, t.table_name""",
        binds,
    )

    # Single-column integer primary keys, which are what make a modulus sample
    # possible. A composite or non-integer PK falls back to a labelled prefix.
    pk_rows = s.fetch(
        "dataprofile.integer_primary_keys",
        f"""SELECT k.table_schema AS owner, k.table_name, k.column_name
            FROM information_schema.key_column_usage k
            JOIN information_schema.table_constraints tc
              ON tc.table_schema   = k.table_schema
             AND tc.table_name     = k.table_name
             AND tc.constraint_name = k.constraint_name
             AND tc.constraint_type = 'PRIMARY KEY'
            JOIN information_schema.columns c
              ON c.table_schema = k.table_schema
             AND c.table_name   = k.table_name
             AND c.column_name  = k.column_name
            WHERE k.table_schema IN ({frag})
              AND c.data_type IN ('int','bigint','smallint','mediumint','tinyint')
            GROUP BY k.table_schema, k.table_name, k.column_name
            HAVING COUNT(*) = 1
            ORDER BY k.table_schema, k.table_name""",
        binds,
    )
    single_int_pk = {}
    seen = {}
    for r in pk_rows:
        key = (r["owner"], r["table_name"])
        seen[key] = seen.get(key, 0) + 1
        single_int_pk[key] = r["column_name"]
    # A table appearing twice had a composite PK; drop it.
    for key, n in seen.items():
        if n > 1:
            single_int_pk.pop(key, None)

    columns = s.fetch(
        "dataprofile.columns",
        f"""SELECT table_schema AS owner, table_name, column_name,
                   UPPER(data_type) AS data_type, column_type,
                   character_maximum_length, is_nullable, column_key
            FROM information_schema.columns
            WHERE table_schema IN ({frag})
            ORDER BY table_schema, table_name, ordinal_position""",
        binds,
    )
    cols_by_table: dict[tuple, list] = {}
    for c in columns:
        cols_by_table.setdefault((c["owner"], c["table_name"]), []).append(c)

    max_rows = _max_rows()
    target = _sample_target()

    table_profile: list[dict] = []
    column_profile: list[dict] = []
    zero_dates: list[dict] = []

    for t in candidates:
        owner, table = t["owner"], t["table_name"]
        est = int(t["est_rows"] or 0)
        try:
            q_owner, q_table = _quote(owner), _quote(table)
        except ValueError as exc:
            table_profile.append({
                "owner": owner, "table_name": table, "estimated_rows": est,
                "actual_rows": None, "scanned_rows": None, "sampled": "NO",
                "sample_pct": None, "profiled": "NO", "skip_reason": str(exc),
            })
            continue

        # The exact count. This is the number Phase 8 compares.
        counted = s.fetch(
            f"dataprofile.count.{owner}.{table}",
            f"SELECT COUNT(*) AS n FROM {q_owner}.{q_table}",
        )
        actual = int(counted[0]["n"]) if counted else None
        if actual is None:
            table_profile.append({
                "owner": owner, "table_name": table, "estimated_rows": est,
                "actual_rows": None, "scanned_rows": None, "sampled": "NO",
                "sample_pct": None, "profiled": "NO",
                "skip_reason": "row count failed; see the query log",
            })
            continue

        pk = single_int_pk.get((owner, table))
        sampled = actual > max_rows
        prefix_only = False
        sample_pct = None
        if sampled:
            sample_pct = round(max(0.01, min(99.0, target / actual * 100)), 4)
            if not pk:
                prefix_only = True

        table_profile.append({
            "owner": owner, "table_name": table,
            "estimated_rows": est, "actual_rows": actual,
            "scanned_rows": min(actual, target) if sampled else actual,
            "sampled": "YES" if sampled else "NO",
            "sample_pct": sample_pct,
            "profiled": "YES",
            # An honest label. A LIMIT is the first N rows in storage order, not
            # a sample, and any duplicate count built on one describes a prefix.
            "skip_reason": ("sampled by prefix (LIMIT): no single-column integer "
                            "primary key to sample on, so coverage is the first "
                            f"{target} rows rather than a spread")
                           if prefix_only else None,
            "engine": t.get("engine"),
        })

        # Zero dates: '0000-00-00', or a zero month or day. They are a property of
        # the DATA, so the catalogue cannot see them, and they decide whether a
        # load can finish at all: PostgreSQL has no such date, and a NOT NULL
        # column cannot take the NULL it would otherwise become. Compared as
        # text, because the literal itself is refused under NO_ZERO_DATE. Every
        # row is read (dates only), not a sample: one zero date suspends a table.
        date_cols = [c for c in cols_by_table.get((owner, table), [])
                     if c["data_type"] in ("DATE", "DATETIME", "TIMESTAMP")]
        if date_cols and actual:
            parts = []
            for i, c in enumerate(date_cols):
                try:
                    qc = _quote(c["column_name"])
                except ValueError:
                    continue
                parts.append(f"SUM(CAST({qc} AS CHAR) LIKE '0000-00-00%') AS z{i}")
                parts.append(f"SUM(CAST({qc} AS CHAR) REGEXP '^[0-9]{{4}}-(00-|[0-9]{{2}}-00)' "
                             f"AND CAST({qc} AS CHAR) NOT LIKE '0000-00-00%') AS p{i}")
            got = s.fetch(f"dataprofile.zero_dates.{owner}.{table}",
                          f"SELECT {', '.join(parts)} FROM {q_owner}.{q_table}") if parts else []
            if got:
                for i, c in enumerate(date_cols):
                    z, part = int(got[0].get(f"z{i}") or 0), int(got[0].get(f"p{i}") or 0)
                    if z or part:
                        zero_dates.append({
                            "owner": owner, "table_name": table, "column_name": c["column_name"],
                            "data_type": c["data_type"], "nullable": c.get("is_nullable"),
                            "zero_date_count": z, "zero_in_date_count": part,
                            "scanned_rows": actual,
                        })

        # Per-column checks, on the same tables and with the same intent as the
        # Oracle probe: near-unique columns get a real duplicate check, text
        # columns get a non-ASCII count.
        for c in cols_by_table.get((owner, table), []):
            dtype = c["data_type"]
            is_text = dtype in TEXT_TYPES
            # Only worth scanning a column that might be a key, or text that
            # might not survive a charset change.
            interesting = is_text or c.get("column_key") in ("PRI", "UNI")
            if not interesting or actual == 0:
                continue
            try:
                q_col = _quote(c["column_name"])
            except ValueError:
                continue

            if sampled and pk:
                where = f" WHERE MOD({_quote(pk)}, {max(1, actual // target)}) = 0"
            elif sampled:
                where = ""
            else:
                where = ""
            limit = f" LIMIT {target}" if (sampled and not pk) else ""

            checks = []
            checks.append(f"COUNT(*) AS scanned")
            checks.append(f"COUNT(DISTINCT {q_col}) AS distinct_count")
            checks.append(f"SUM(CASE WHEN {q_col} IS NULL THEN 1 ELSE 0 END) AS null_count")
            if is_text:
                # Non-ASCII is what a utf8mb3 -> utf8mb4 or -> UTF8 target
                # change actually turns on. CONVERT ... USING ascii is lossy by
                # design here; the comparison detects the loss.
                checks.append(
                    f"SUM(CASE WHEN {q_col} IS NOT NULL AND "
                    f"HEX({q_col}) <> HEX(CONVERT(CONVERT({q_col} USING ascii) USING utf8mb4)) "
                    "THEN 1 ELSE 0 END) AS non_ascii_count"
                )
                # 4-byte characters specifically: these are what utf8mb3 cannot
                # hold, and they are the ones that truncate silently.
                checks.append(
                    f"SUM(CASE WHEN {q_col} IS NOT NULL AND "
                    f"CHAR_LENGTH({q_col}) <> LENGTH({q_col}) AND "
                    f"LENGTH({q_col}) - CHAR_LENGTH({q_col}) >= 3 "
                    "THEN 1 ELSE 0 END) AS four_byte_count"
                )

            inner = (f"SELECT {q_col} FROM {q_owner}.{q_table}{where}{limit}"
                     if limit else None)
            sql = (f"SELECT {', '.join(checks)} FROM ({inner}) x"
                   if inner else
                   f"SELECT {', '.join(checks)} FROM {q_owner}.{q_table}{where}")

            got = s.fetch(f"dataprofile.column.{owner}.{table}.{c['column_name']}", sql)
            if not got:
                continue
            g = got[0]
            scanned = int(g.get("scanned") or 0)
            distinct = int(g.get("distinct_count") or 0)
            nulls = int(g.get("null_count") or 0)
            non_null = max(0, scanned - nulls)
            column_profile.append({
                "owner": owner, "table_name": table, "column_name": c["column_name"],
                "actual_rows": actual, "scanned_rows": scanned,
                "sampled": "YES" if sampled else "NO",
                "sample_pct": sample_pct,
                "duplicate_count": max(0, non_null - distinct),
                "non_ascii_count": int(g.get("non_ascii_count") or 0) if is_text else None,
                "four_byte_count": int(g.get("four_byte_count") or 0) if is_text else None,
                "checked_duplicates": "YES",
                "checked_non_ascii": "YES" if is_text else "NO",
                "data_type": dtype,
                "character_set": c.get("character_maximum_length") and None,
            })

    return {
        "source_inventory.table_profile": table_profile,
        "source_inventory.column_profile": column_profile,
        # MySQL-only: what Phase 7 must know before a load that cannot finish.
        "source_inventory.mysql_zero_dates": zero_dates,
    }
