"""Checks for cross-engine validation.

The offline half needs nothing. The **proof** half needs the local PostgreSQL
(`scripts/postgres-target/run_pg.ps1`) and is what makes this worth having: it
runs the generated SQL for real and compares the result against Oracle's
documented rendering, computed independently in Python. Without that, a
canonical form that looks right and silently disagrees would pass review.

    python -m validate.selftest_crossengine
    python -m validate.selftest_crossengine --offline      # skip the database half
"""

from __future__ import annotations

import hashlib
import os
import sys
from decimal import Decimal
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "validate"

from . import crossengine as ce


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# Oracle's rendering, implemented independently from the SQL under test. If the
# two agree, the canonical form holds; if they were derived from each other,
# agreement would prove nothing.
def _oracle_text(value, oracle_type: str) -> str:
    if value is None:
        return ce.NULL_MARKER
    t = oracle_type.upper()
    if t in ("NUMBER", "FLOAT"):
        return format(Decimal(str(value)).normalize(), "f")
    if t in ("CHAR", "NCHAR"):
        return str(value).rstrip()
    return str(value)


def main(argv=None) -> int:
    offline = "--offline" in (argv if argv is not None else sys.argv[1:])
    c = _Check()

    print("the null marker")
    c("is printable ASCII", all(32 <= ord(ch) < 127 for ch in ce.NULL_MARKER), repr(ce.NULL_MARKER))
    c("is not empty", len(ce.NULL_MARKER) > 0)
    c("is the same on both sides",
      ce.NULL_MARKER in ce.oracle_checksum_sql("S", "T", [{"column_name": "A", "data_type": "NUMBER"}])
      and ce.NULL_MARKER in ce.postgres_checksum_sql("S", "T", [{"column_name": "A", "data_type": "NUMBER"}]))

    print("names")
    c("the target name is lower case", ce.target_name("CUSTOMER") == "customer")
    c("Oracle SQL quotes upper case", '"CUSTOMER"' in ce.oracle_checksum_sql(
        "DBMIG_APP", "CUSTOMER", [{"column_name": "ID", "data_type": "NUMBER"}]))
    c("PostgreSQL SQL quotes lower case", '"customer"' in ce.postgres_checksum_sql(
        "DBMIG_APP", "CUSTOMER", [{"column_name": "ID", "data_type": "NUMBER"}]))

    print("no engine-specific hash")
    ora = ce.oracle_checksum_sql("S", "T", [{"column_name": "A", "data_type": "NUMBER"}])
    pg = ce.postgres_checksum_sql("S", "T", [{"column_name": "A", "data_type": "NUMBER"}])
    c("Oracle does not use ORA_HASH", "ORA_HASH" not in ora)
    c("Oracle uses MD5", "MD5" in ora)
    c("PostgreSQL uses md5", "md5(" in pg)
    c("both sides count rows", "COUNT(*)" in ora and "count(*)" in pg)

    print("numbers render identically")
    c("PostgreSQL trims scale", "trim_scale" in ce.postgres_expr("AMOUNT", "NUMBER"))
    c("PostgreSQL does not use a format mask that leaves a trailing point",
      "FM9" not in ce.postgres_expr("AMOUNT", "NUMBER"),
      ce.postgres_expr("AMOUNT", "NUMBER"))
    c("Oracle uses TM9", "TM9" in ce.oracle_expr("AMOUNT", "NUMBER"))

    print("types that cannot be compared are excluded, with a reason")
    cols = [{"column_name": "A", "data_type": "NUMBER"},
            {"column_name": "B", "data_type": "LONG"},
            {"column_name": "C", "data_type": "XMLTYPE"},
            {"column_name": "D", "data_type": "TY_ADDRESS", "data_type_owner": "DBMIG_APP"}]
    ok, skipped = ce.comparable_columns(cols)
    c("an ordinary column is compared", [x["column_name"] for x in ok] == ["A"])
    c("LONG is skipped", any(x["column_name"] == "B" for x in skipped))
    c("XMLTYPE is skipped", any(x["column_name"] == "C" for x in skipped))
    c("a user-defined type is skipped", any(x["column_name"] == "D" for x in skipped))
    c("every skip says why", all(len(x["why_skipped"]) > 30 for x in skipped))

    print("differences that are correct are explained, not counted as loss")
    c("Oracle NULL against PostgreSQL empty string is expected",
      (ce.explain_difference("VARCHAR2", None, "") or {}).get("kind") == "empty_string_is_null")
    c("and the other way round",
      (ce.explain_difference("VARCHAR2", "", None) or {}).get("kind") == "empty_string_is_null")
    c("1.50 against 1.5 is expected",
      (ce.explain_difference("NUMBER", "1.50", "1.5") or {}).get("kind") == "number_scale")
    c("CHAR padding is expected",
      (ce.explain_difference("CHAR", "AB  ", "AB") or {}).get("kind") == "char_padding")
    c("1 against true is expected",
      (ce.explain_difference("NUMBER", "1", "true") or {}).get("kind") == "boolean_from_number")
    c("a genuinely different value is NOT explained away",
      ce.explain_difference("VARCHAR2", "Alice", "Bob") is None)
    c("a different number is NOT explained away",
      ce.explain_difference("NUMBER", "10", "11") is None)
    c("a lost date component is NOT explained away",
      ce.explain_difference("DATE", "2026-01-02 03:04:05", "2026-01-02 00:00:00") is None)
    c("every expected difference carries its reason",
      all(len(v) > 60 for v in ce.EXPECTED_DIFFERENCES.values()))

    if offline:
        print(f"\n{c.ok}/{c.n} checks passed (offline; the proof was skipped)")
        return 0 if c.ok == c.n else 1

    print("proof: the generated SQL runs, and both engines agree")
    try:
        import pg8000.dbapi
    except ImportError:
        print("  [skip] pg8000 is not installed")
        print(f"\n{c.ok}/{c.n} checks passed (the proof was skipped)")
        return 0 if c.ok == c.n else 1

    dsn = os.environ.get("DBSHIFT_PG_DSN", "localhost:5432/dbshift")
    host, rest = dsn.split(":", 1)
    port, database = rest.split("/", 1)
    try:
        conn = pg8000.dbapi.connect(
            host=host, port=int(port), database=database,
            user=os.environ.get("DBSHIFT_PG_USER", "dbshift"),
            password=os.environ.get("DBSHIFT_PG_PASSWORD", "dbshift-local-only"))
    except Exception as exc:  # noqa: BLE001
        print(f"  [skip] no PostgreSQL at {dsn}: {str(exc).splitlines()[0]}")
        print("         start it with scripts\\postgres-target\\run_pg.ps1")
        print(f"\n{c.ok}/{c.n} checks passed (the proof was skipped)")
        return 0 if c.ok == c.n else 1

    cur = conn.cursor()
    try:
        cur.execute("DROP SCHEMA IF EXISTS dbshift_xe_selftest CASCADE")
        cur.execute("CREATE SCHEMA dbshift_xe_selftest")
        cur.execute("""
            CREATE TABLE dbshift_xe_selftest.probe (
              id numeric, name text, flag char(4), amount numeric, opened_on timestamp)""")
        # Every row exercises one difference that would otherwise be a mismatch.
        rows = [
            (1, "Alice", "AB  ", "10.00", "2026-01-02 03:04:05"),
            (2, "Bob", "C   ", "1.50", "2026-02-03 04:05:06"),
            (3, None, "    ", "0.001", "2026-03-04 05:06:07"),
            (4, "Dave", "XYZ ", "-42.100", "2026-04-05 06:07:08"),
            (5, "Eve", None, "1000000", "2026-05-06 07:08:09"),
        ]
        for r in rows:
            cur.execute(
                "INSERT INTO dbshift_xe_selftest.probe VALUES (%s,%s,%s,%s,%s::timestamp)", r)
        conn.commit()

        cols = [{"column_name": "ID", "data_type": "NUMBER"},
                {"column_name": "NAME", "data_type": "VARCHAR2"},
                {"column_name": "FLAG", "data_type": "CHAR"},
                {"column_name": "AMOUNT", "data_type": "NUMBER"},
                {"column_name": "OPENED_ON", "data_type": "DATE"}]

        cur.execute(ce.postgres_checksum_sql("DBSHIFT_XE_SELFTEST", "PROBE", cols))
        pg_count, pg_sum = cur.fetchone()
        c("the generated PostgreSQL SQL runs", True)
        c("it counts every row", pg_count == len(rows), f"{pg_count} of {len(rows)}")

        # Oracle's answer, computed independently.
        total = 0
        for rid, name, flag, amount, ts in rows:
            text = "|".join([
                _oracle_text(rid, "NUMBER"), _oracle_text(name, "VARCHAR2"),
                _oracle_text(flag, "CHAR"), _oracle_text(amount, "NUMBER"),
                _oracle_text(ts, "DATE")])
            total += int(hashlib.md5(text.encode()).hexdigest()[:12], 16)

        c("both engines produce the same checksum over the same data",
          pg_sum == total, f"postgres {pg_sum} vs oracle {total}")

        # A changed value must change the checksum -- otherwise the whole
        # comparison proves nothing.
        cur.execute("UPDATE dbshift_xe_selftest.probe SET name = 'CHANGED' WHERE id = 1")
        conn.commit()
        cur.execute(ce.postgres_checksum_sql("DBSHIFT_XE_SELFTEST", "PROBE", cols))
        changed_count, changed_sum = cur.fetchone()
        c("a changed value changes the checksum", changed_sum != pg_sum)
        c("and the row count does not move", changed_count == pg_count)
    finally:
        try:
            cur.execute("DROP SCHEMA IF EXISTS dbshift_xe_selftest CASCADE")
            conn.commit()
        finally:
            conn.close()

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
