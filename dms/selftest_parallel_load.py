"""Offline checks for range-based parallel loading. No AWS, no Oracle.

    python -m dms.selftest_parallel_load
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "dms"

from . import mappings, parallel_load as pl

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}\n       got  {got!r}\n       want {want!r}")


SCHEMA = "DBMIG_TELCO"


def _pk(table, col, position=1):
    return {"owner": SCHEMA, "constraint_name": f"PK_{table}", "constraint_type": "P",
            "table_name": table}, {"owner": SCHEMA, "constraint_name": f"PK_{table}",
                                    "table_name": table, "column_name": col, "position": position}


def _col(table, col, dtype):
    return {"owner": SCHEMA, "table_name": table, "column_name": col, "data_type": dtype}


class FakeCursor:
    """Answers one canned (lo, hi) per table, and records what SQL it saw."""

    def __init__(self, ranges, deny=()):
        self.ranges, self.deny, self.seen = ranges, deny, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.seen.append(sql)
        self._last = sql

    def fetchone(self):
        for table, (lo, hi) in self.ranges.items():
            if f'"{table}"' in self._last:
                if table in self.deny:
                    raise RuntimeError("ORA-00942: table or view does not exist")
                return (lo, hi)
        raise RuntimeError("unexpected table in test SQL")


class FakeConn:
    def __init__(self, ranges, deny=()):
        self.cur = FakeCursor(ranges, deny)
        self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def main() -> int:
    print("numeric_pk_columns: eligibility from discovery data alone")
    constraints, cols = [], []
    for pk_c, pk_cc in (_pk("CDR", "CDR_ID"), _pk("SUBSCRIBER", "SUBSCRIBER_ID")):
        constraints.append(pk_c)
        cols.append(pk_cc)
    constraint_columns = cols
    # a composite key: two rows under the same constraint
    c3, cc3a = _pk("PLAN_CATALOG", "PLAN_ID")
    _, cc3b = _pk("PLAN_CATALOG", "REGION_ID", position=2)
    constraints.append(c3)
    constraint_columns += [cc3a, cc3b]
    # a non-numeric key
    c4, cc4 = _pk("DEVICE", "IMEI")
    constraints.append(c4)
    constraint_columns.append(cc4)
    # no primary key at all: SESSION has none

    columns = [
        _col("CDR", "CDR_ID", "NUMBER"), _col("SUBSCRIBER", "SUBSCRIBER_ID", "NUMBER"),
        _col("PLAN_CATALOG", "PLAN_ID", "NUMBER"), _col("PLAN_CATALOG", "REGION_ID", "NUMBER"),
        _col("DEVICE", "IMEI", "VARCHAR2"),
    ]
    tables = ["CDR", "SUBSCRIBER", "PLAN_CATALOG", "DEVICE", "SESSION"]
    pk_cols = pl.numeric_pk_columns(constraints, constraint_columns, columns,
                                    schema=SCHEMA, tables=tables)
    check("single numeric PK tables are eligible", pk_cols, {"CDR": "CDR_ID", "SUBSCRIBER": "SUBSCRIBER_ID"})
    check("a composite key is not eligible", "PLAN_CATALOG" in pk_cols, False)
    check("a non-numeric key is not eligible", "DEVICE" in pk_cols, False)
    check("no primary key at all is not eligible", "SESSION" in pk_cols, False)
    check("a table outside `tables` is ignored even with a numeric PK",
          pl.numeric_pk_columns(constraints, constraint_columns, columns, schema=SCHEMA,
                                tables=["CDR"]), {"CDR": "CDR_ID"})
    check("a different schema's matching table is ignored",
          pl.numeric_pk_columns(constraints, constraint_columns, columns, schema="OTHER",
                                tables=tables), {})

    print("boundaries: splitting one column's range")
    check("4 batches -> 3 interior cuts", pl.boundaries(0, 100, 4), [[25], [50], [75]])
    check("2 batches -> 1 cut, the midpoint", pl.boundaries(0, 10, 2), [[5]])
    check("0 or 1 batches is not a split", (pl.boundaries(0, 100, 0), pl.boundaries(0, 100, 1)),
          (None, None))
    check("fewer distinct values than batches refuses rather than collapsing cuts",
          pl.boundaries(0, 3, 10), None)
    check("an empty or inverted range refuses", (pl.boundaries(5, 5, 4), pl.boundaries(10, 5, 4)),
          (None, None))
    check("a very wide range gives batches - 1 boundaries",
          len(pl.boundaries(0, 20_895_744, 8)), 7)

    print("fetch_ranges: one live query per table, a failure excludes just that table")
    conn = FakeConn({"CDR": (1, 20_895_744), "SUBSCRIBER": (1, 802_496)}, deny=())
    out = pl.fetch_ranges(lambda: conn, SCHEMA, {"CDR": "CDR_ID", "SUBSCRIBER": "SUBSCRIBER_ID"})
    check("both tables read", out, {"CDR": ("CDR_ID", 1, 20_895_744),
                                    "SUBSCRIBER": ("SUBSCRIBER_ID", 1, 802_496)})
    check("the connection is closed afterwards", conn.closed, True)
    check("real identifiers, quoted", any('"CDR"."CDR_ID"' not in s and '"CDR_ID"' in s and '"CDR"' in s
                                          for s in conn.cur.seen), True)

    conn2 = FakeConn({"CDR": (1, 100), "SUBSCRIBER": (1, 100)}, deny=("SUBSCRIBER",))
    out2 = pl.fetch_ranges(lambda: conn2, SCHEMA, {"CDR": "CDR_ID", "SUBSCRIBER": "SUBSCRIBER_ID"})
    check("an unreadable table is left out, not raised", out2, {"CDR": ("CDR_ID", 1, 100)})
    check("the connection still gets closed on a partial failure", conn2.closed, True)

    check("no eligible tables means no connection is even opened",
          pl.fetch_ranges(lambda: (_ for _ in ()).throw(AssertionError("should not connect")), SCHEMA, {}),
          {})

    print("an untrusted identifier is refused before it reaches SQL")
    try:
        pl.fetch_ranges(lambda: FakeConn({}), SCHEMA, {"CDR; DROP TABLE x--": "ID"})
        check("a table name with SQL syntax in it is refused", False, True)
    except ValueError:
        check("a table name with SQL syntax in it is refused", True, True)

    print("build_rules: DMS's own table-settings shape, only for tables that could split")
    ranges = {"CDR": ("CDR_ID", 1, 20_895_744), "SUBSCRIBER": ("SUBSCRIBER_ID", 1, 2)}
    rules, report = pl.build_rules(schema=SCHEMA, table_ranges=ranges, batches=4)
    check("only the table with enough range is split", report["split"], ["CDR"])
    check("one table-settings rule, shaped exactly as DMS expects", rules, [{
        "rule-type": "table-settings",
        "object-locator": {"schema-name": SCHEMA, "table-name": "CDR"},
        "parallel-load": {"type": "ranges", "columns": ["CDR_ID"],
                          "boundaries": pl.boundaries(1, 20_895_744, 4)},
    }])

    print("table_mappings: parallel-load rules are numbered into the one rule sequence")
    tm = mappings.table_mappings(schema=SCHEMA, tables=["CDR"], lowercase=False,
                                 parallel_load_rules=rules)
    kinds = [r["rule-type"] for r in tm["rules"]]
    check("selection rule first, then the table-settings rule", kinds, ["selection", "table-settings"])
    check("rule-id is one continuous sequence", [r["rule-id"] for r in tm["rules"]], ["1", "2"])
    check("no batches means no rules and the plain rule list", len(mappings.table_mappings(
        schema=SCHEMA, tables=["CDR"], lowercase=False, parallel_load_rules=[])["rules"]), 1)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
