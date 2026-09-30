"""Phase 8 for a MySQL source: the canonical row text, proven against both engines.

    python -m validate.selftest_mysql            # offline, then the proof if both are reachable
    python -m validate.selftest_mysql --offline

The proof half follows `selftest_crossengine`'s design: the canonical rendering
is REIMPLEMENTED here in Python, independently of the SQL, and MySQL's checksum
SQL and PostgreSQL's must each equal it over the same rows. If the two SQL forms
were only compared with each other, agreement would prove nothing -- both could
be wrong the same way. Then a value is changed and the checksum must move,
including '' against NULL, which MySQL (unlike Oracle) keeps distinct.

The proof uses the local stand-ins: MySQL 8.4 on 127.0.0.1:3384 (dbshiftadm) and
PostgreSQL 16 from DBSHIFT_PG_DSN. It creates one scratch table on each, in a
scratch schema, and drops it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "validate"

from . import mysql as vm

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}" + (f" -- {detail}" if detail else ""))


# The table both engines hold, as Phase 4c would have built it from MySQL.
COLS = [
    {"column_name": "id", "data_type": "BIGINT", "data_type_mod": "bigint unsigned"},
    {"column_name": "amount", "data_type": "DECIMAL", "data_type_mod": "decimal(14,2)"},
    {"column_name": "big", "data_type": "BIGINT", "data_type_mod": "bigint unsigned"},
    {"column_name": "flag", "data_type": "TINYINT", "data_type_mod": "tinyint(1)"},
    {"column_name": "code", "data_type": "CHAR", "data_type_mod": "char(3)"},
    {"column_name": "note", "data_type": "VARCHAR", "data_type_mod": "varchar(40)"},
    {"column_name": "status", "data_type": "ENUM", "data_type_mod": "enum('new','paid')"},
    {"column_name": "opts", "data_type": "SET", "data_type_mod": "set('a','b')"},
    {"column_name": "made", "data_type": "DATETIME", "data_type_mod": "datetime"},
    {"column_name": "seen", "data_type": "TIMESTAMP", "data_type_mod": "timestamp"},
    {"column_name": "born", "data_type": "DATE", "data_type_mod": "date"},
    {"column_name": "attrs", "data_type": "JSON", "data_type_mod": "json"},
]
MYSQL_DDL = ("CREATE TABLE t (id bigint unsigned primary key, amount decimal(14,2), big bigint unsigned, "
             "flag tinyint(1), code char(3), note varchar(40), status enum('new','paid'), "
             "opts set('a','b'), made datetime, seen timestamp NULL, born date, attrs json)")
PG_DDL = ("CREATE TABLE t (id bigint primary key, amount numeric(14,2), big numeric(20,0), flag smallint, "
          "code char(3), note varchar(40), status text, opts text, made timestamp(0), "
          "seen timestamptz(0), born date, attrs jsonb)")
ROWS = [
    (1, Decimal("14.50"), 18446744073709551615, 1, "GB", "", "new", "a,b",
     "2026-01-02 03:04:05", "2026-01-02 03:04:05", "1990-12-31", {"b": 1, "aa": [1, 2], "a": "x"}),
    (2, Decimal("0.00"), 0, 0, "IE ", None, "paid", "", "2026-06-30 23:59:59", None, None, None),
    (3, Decimal("-0.50"), 42, 1, None, "naïve café", None, "b", None, "2025-03-30 01:30:00", "2000-02-29",
     {"k": {"z": True, "y": None}}),
]


# ------------------------------------------------------------- the reference

def _json_text(v):
    """MySQL's and jsonb's shared canonical form: keys by length then bytes, ', ' and ': '."""
    if isinstance(v, dict):
        items = sorted(v.items(), key=lambda kv: (len(kv[0].encode()), kv[0].encode()))
        return "{" + ", ".join(json.dumps(k, ensure_ascii=False) + ": " + _json_text(x)
                               for k, x in items) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_json_text(x) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False)


def reference(rows) -> tuple[int, int]:
    total = 0
    for r in rows:
        parts = []
        for c, v in zip(COLS, r):
            base = c["data_type"].lower()
            if v is None:
                parts.append(vm.NULL_MARKER)
            elif base == "decimal":
                s = str(v)
                parts.append(s.rstrip("0").rstrip(".") if "." in s else s)
            elif base == "char":
                parts.append(v.rstrip())
            elif base == "json":
                parts.append(_json_text(v))
            else:
                parts.append(str(v))
        total += int(hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:12], 16)
    return len(rows), total


# ------------------------------------------------------------- offline

def offline():
    print("canonical expressions")
    dec = vm.mysql_expr(COLS[1])
    check("MySQL DECIMAL strips trailing zeros only when there is a point",
          "LOCATE('.'" in dec and "TRAILING '0'" in dec)
    check("PostgreSQL DECIMAL uses trim_scale", vm.postgres_expr(COLS[1], "amount").startswith("trim_scale("))
    check("TIMESTAMP is compared in UTC on PostgreSQL",
          "AT TIME ZONE 'UTC'" in vm.postgres_expr(COLS[9], "seen"))
    check("CHAR is right-trimmed on both sides",
          vm.mysql_expr(COLS[4]).startswith("RTRIM(") and vm.postgres_expr(COLS[4], "code").startswith("rtrim("))
    check("DATETIME renders to the second on both",
          "%H:%i:%s'" in vm.mysql_expr(COLS[8]) and "HH24:MI:SS'" in vm.postgres_expr(COLS[8], "made"))
    frac = {"column_name": "m", "data_type": "DATETIME", "data_type_mod": "datetime(3)"}
    check("a DATETIME with a fraction keeps it on both", ".%f" in vm.mysql_expr(frac)
          and ".US" in vm.postgres_expr(frac, "m"))
    check("'' is never normalised to NULL on a MySQL source",
          "empty_string_is_null" in vm.NOT_EXPECTED and "empty_string_is_null" not in vm.EXPECTED_DIFFERENCES)
    check("zero dates are listed as NOT expected", "zero_date" in vm.NOT_EXPECTED)
    sql = vm.mysql_checksum_sql("app", "t", COLS[:2])
    check("MySQL checksum: MD5, 12 hex digits, summed", "MD5(CONCAT(" in sql and "CONV(SUBSTR(" in sql)
    pg = vm.postgres_checksum_sql("app", "order_tbl", [(COLS[0], "id")])
    check("PostgreSQL checksum reads the table under 4c's name", '"app"."order_tbl"' in pg)
    try:
        vm.q("x`; DROP")
        check("an unsafe identifier is refused", False)
    except ValueError:
        check("an unsafe identifier is refused", True)
    check("the reference itself is order-independent",
          reference(ROWS) == reference(list(reversed(ROWS))))


# ------------------------------------------------------------- proof

def _mysql():
    import pymysql
    try:
        c = pymysql.connect(host="127.0.0.1", port=3384, user="dbshiftadm", password="local-only-adm",
                            charset="utf8mb4", connect_timeout=5, autocommit=True)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc).splitlines()[0]
    cur = c.cursor()
    cur.execute("SET time_zone = '+00:00'")
    cur.execute("SET NAMES utf8mb4")
    return c, None


def _pg():
    dsn, pw = os.environ.get("DBSHIFT_PG_DSN"), os.environ.get("DBSHIFT_PG_PASSWORD")
    if not (dsn and pw):
        return None, "DBSHIFT_PG_DSN / DBSHIFT_PG_PASSWORD not set"
    import pg8000.dbapi
    host, rest = dsn.split(":", 1)
    port, db = rest.split("/", 1)
    try:
        c = pg8000.dbapi.connect(host=host, port=int(port), database=db,
                                 user=os.environ.get("DBSHIFT_PG_USER", "dbshift"), password=pw, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc).splitlines()[0]
    c.cursor().execute("SET TIME ZONE 'UTC'")
    return c, None


def _mysql_sum(cur):
    cur.execute(vm.mysql_checksum_sql("dbshift_selftest", "t", COLS))
    n, s = cur.fetchone()
    return int(n), int(s)


def _pg_sum(cur):
    cur.execute(vm.postgres_checksum_sql("dbshift_selftest", "t", [(c, c["column_name"]) for c in COLS]))
    n, s = cur.fetchone()
    return int(n), int(s)


def proof():
    my, why_my = _mysql()
    pg, why_pg = _pg()
    if my is None or pg is None:
        print(f"\nthe proof was skipped: {why_my or ''} {why_pg or ''}".rstrip())
        return
    print("\nproof: both engines against an independent Python reference")
    want = reference(ROWS)
    mc, pc = my.cursor(), pg.cursor()
    try:
        mc.execute("CREATE DATABASE IF NOT EXISTS dbshift_selftest")
        mc.execute("USE dbshift_selftest")
        mc.execute("DROP TABLE IF EXISTS t")
        mc.execute(MYSQL_DDL)
        mc.executemany("INSERT INTO t VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       [r[:-1] + (json.dumps(r[-1]) if r[-1] is not None else None,) for r in ROWS])
        pc.execute("CREATE SCHEMA IF NOT EXISTS dbshift_selftest")
        pc.execute("DROP TABLE IF EXISTS dbshift_selftest.t")
        pc.execute(PG_DDL.replace("CREATE TABLE t", "CREATE TABLE dbshift_selftest.t"))
        for r in ROWS:
            pc.execute("INSERT INTO dbshift_selftest.t VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       r[:-1] + (json.dumps(r[-1]) if r[-1] is not None else None,))
        pg.commit()

        got_my, got_pg = _mysql_sum(mc), _pg_sum(pc)
        check("MySQL's checksum equals the independent reference", got_my == want, f"{got_my} vs {want}")
        check("PostgreSQL's checksum equals the independent reference", got_pg == want, f"{got_pg} vs {want}")
        check("...so the two engines agree for the right reason", got_my == got_pg)

        pc.execute("UPDATE dbshift_selftest.t SET amount = 14.51 WHERE id = 1")
        pg.commit()
        check("one cent on the target moves the checksum", _pg_sum(pc) != got_my)
        pc.execute("UPDATE dbshift_selftest.t SET amount = 14.50, note = NULL WHERE id = 1")
        pg.commit()
        check("'' arriving as NULL moves it -- MySQL keeps them distinct", _pg_sum(pc) != got_my)
        pc.execute("UPDATE dbshift_selftest.t SET note = '' WHERE id = 1")
        pg.commit()
        check("and restoring it restores agreement", _pg_sum(pc) == got_my)
        mc.execute("UPDATE t SET seen = '2026-01-02 03:04:06' WHERE id = 1")
        check("a one-second TIMESTAMP change on MySQL moves it too", _mysql_sum(mc) != got_pg)
    finally:
        try:
            mc.execute("DROP DATABASE IF EXISTS dbshift_selftest")
        except Exception:  # noqa: BLE001
            pass
        try:
            pg.rollback()
            pc.execute("DROP SCHEMA IF EXISTS dbshift_selftest CASCADE")
            pg.commit()
        except Exception:  # noqa: BLE001
            pass
        my.close()
        pg.close()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    print("validate selftest -- MySQL source\n")
    offline()
    if "--offline" not in argv:
        proof()
    print(f"\n{PASS}/{PASS + FAIL} checks passed" + (f", {FAIL} FAILED" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
