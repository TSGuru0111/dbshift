"""Checks for table, index and constraint DDL generation.

The offline half needs nothing. The **proof** half runs every generated
statement against the local PostgreSQL inside a transaction that is rolled
back, because a foreign key that references a table created later, or a check
constraint carrying Oracle syntax, only fails when executed. Reading the SQL
would have passed all of it.

    python -m convert.selftest_ddl
    python -m convert.selftest_ddl --offline
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "convert"

from . import ddl

OWNER = "DBMIG_APP"


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _col(name, dtype, **kw):
    return {"owner": OWNER, "table_name": kw.pop("table", "CUSTOMER"), "column_name": name,
            "data_type": dtype, "column_id": kw.pop("pos", 1), **kw}


def _datasets():
    return {
        "tables": [{"owner": OWNER, "table_name": "CUSTOMER"},
                   {"owner": OWNER, "table_name": "LOAN"},
                   {"owner": OWNER, "table_name": "Q_TAB"},
                   {"owner": OWNER, "table_name": "DR$IX$I"}],
        "objects": [{"owner": OWNER, "object_name": "CUSTOMER", "object_type": "TABLE"},
                    {"owner": OWNER, "object_name": "LOAN", "object_type": "TABLE"}],
        "queues": [{"owner": OWNER, "name": "MY_Q", "queue_table": "Q_TAB"}],
        "external_tables": [],
        "columns": [
            _col("CUSTOMER_ID", "NUMBER", data_precision=10, data_scale=0, nullable="N", pos=1),
            _col("EMAIL", "VARCHAR2", char_length=120, pos=2),
            _col("STATUS", "VARCHAR2", char_length=10, pos=3, data_default="'ACTIVE'"),
            _col("ORDER", "NUMBER", data_precision=5, pos=4),
            _col("NEXT_ID", "NUMBER", data_precision=10, pos=5,
                 data_default='"DBMIG_APP"."SEQ_C"."NEXTVAL"'),
            _col("OPENED_ON", "DATE", pos=6, data_default="SYSDATE"),
            _col("SECRET", "ROWID", pos=7),
            _col("COMPUTED", "NUMBER", pos=8, virtual_column="YES"),
            _col("LOAN_ID", "NUMBER", data_precision=10, nullable="N", table="LOAN", pos=1),
            _col("CUSTOMER_ID", "NUMBER", data_precision=10, table="LOAN", pos=2),
        ],
        "constraints": [
            {"owner": OWNER, "table_name": "CUSTOMER", "constraint_name": "PK_CUST",
             "constraint_type": "P", "index_name": "PK_CUST"},
            {"owner": OWNER, "table_name": "CUSTOMER", "constraint_name": "CK_STATUS",
             "constraint_type": "C", "search_condition_vc": "status IN ('ACTIVE','CLOSED')"},
            {"owner": OWNER, "table_name": "CUSTOMER", "constraint_name": "CK_NN",
             "constraint_type": "C", "search_condition_vc": '"EMAIL" IS NOT NULL'},
            {"owner": OWNER, "table_name": "CUSTOMER", "constraint_name": "CK_ORACLE",
             "constraint_type": "C", "search_condition_vc": "opened_on < SYSDATE"},
            {"owner": OWNER, "table_name": "LOAN", "constraint_name": "FK_LOAN_CUST",
             "constraint_type": "R", "r_constraint_name": "PK_CUST", "r_owner": OWNER,
             "delete_rule": "CASCADE"},
        ],
        "constraint_columns": [
            {"owner": OWNER, "constraint_name": "PK_CUST", "column_name": "CUSTOMER_ID",
             "position": 1, "table_name": "CUSTOMER"},
            {"owner": OWNER, "constraint_name": "FK_LOAN_CUST", "column_name": "CUSTOMER_ID",
             "position": 1, "table_name": "LOAN"},
        ],
        "indexes": [
            {"owner": OWNER, "table_name": "CUSTOMER", "index_name": "PK_CUST",
             "index_type": "NORMAL", "uniqueness": "UNIQUE"},
            {"owner": OWNER, "table_name": "CUSTOMER", "index_name": "IX_EMAIL",
             "index_type": "NORMAL", "uniqueness": "NONUNIQUE"},
            {"owner": OWNER, "table_name": "CUSTOMER", "index_name": "IX_FUNC",
             "index_type": "FUNCTION-BASED NORMAL", "uniqueness": "NONUNIQUE"},
            {"owner": OWNER, "table_name": "CUSTOMER", "index_name": "IX_TEXT",
             "index_type": "DOMAIN", "uniqueness": "NONUNIQUE"},
        ],
        "index_columns": [
            {"index_owner": OWNER, "index_name": "IX_EMAIL", "column_name": "EMAIL",
             "column_position": 1, "table_name": "CUSTOMER"},
        ],
        "sequences": [{"owner": OWNER, "sequence_name": "SEQ_C", "last_number": 100}],
    }


def main(argv=None) -> int:
    offline = "--offline" in (argv if argv is not None else sys.argv[1:])
    c = _Check()
    plan = ddl.build(owner=OWNER, datasets=_datasets())
    all_sql = "\n".join(ddl.statements_in_order(plan))
    notes = {n["kind"]: n for n in plan["notes"]}

    print("identifiers")
    c("names are lower-cased for PostgreSQL", "dbmig_app.customer" in all_sql)
    c("a PostgreSQL reserved word is renamed", "order_col" in all_sql and '"order"' not in all_sql)
    c("the rename is reported", "reserved_word" in notes)
    c("the rename warns that application SQL must change",
      "must change" in notes.get("reserved_word", {}).get("detail", ""))
    long_name = "x" * 70
    c("an over-long identifier is truncated", len(ddl.ident(long_name)) == ddl.PG_MAX_IDENT)

    print("types")
    c("NUMBER(10) narrows to a real integer type",
      "customer_id BIGINT" in all_sql or "customer_id INTEGER" in all_sql, all_sql[:200])
    c("VARCHAR2 becomes VARCHAR", "email VARCHAR(120)" in all_sql)
    c("an unmappable type is an error, not a guess",
      notes.get("unmappable_type", {}).get("severity") == "error")
    c("the unmappable column is left out", "secret" not in all_sql.lower())
    c("a LONG column still maps, to TEXT",
      ddl.typemap.map_type("LONG")[0] == "TEXT")
    c("a virtual column is not invented", "computed" not in all_sql.lower())
    c("and the omission is explained", "virtual_column" in notes)

    print("defaults")
    c("a literal default carries across", "DEFAULT 'ACTIVE'" in all_sql)
    c("SYSDATE becomes CURRENT_TIMESTAMP", "DEFAULT CURRENT_TIMESTAMP" in all_sql)
    c("a sequence default becomes nextval", "nextval('dbmig_app.seq_c')" in all_sql)
    c("NOT NULL is carried", "NOT NULL" in all_sql)

    print("order of operations")
    order = [k for k, _ in plan["order"]]
    c("tables come before keys", order.index("tables") < order.index("primary_unique"))
    c("primary keys come before foreign keys",
      order.index("primary_unique") < order.index("foreign"))
    c("indexes come last", order[-1] == "indexes")
    c("the order explains why", all(len(w) > 15 for _, w in plan["order"]))

    print("constraints")
    c("a primary key is emitted", "PRIMARY KEY (customer_id)" in all_sql)
    c("a foreign key is emitted", "FOREIGN KEY (customer_id)" in all_sql)
    c("ON DELETE CASCADE is carried", "ON DELETE CASCADE" in all_sql)
    c("a plain check is emitted", "status IN ('ACTIVE','CLOSED')" in all_sql)
    c("Oracle's NOT NULL check is not duplicated", "IS NOT NULL" not in all_sql)
    c("a check using Oracle functions is declined", "SYSDATE" not in all_sql)
    c("and declining it is reported", "check_not_translated" in notes)
    c("the decline says the rule is unenforced",
      "not enforced" in notes.get("check_not_translated", {}).get("detail", ""))

    print("indexes")
    c("an ordinary index is created", "CREATE INDEX ix_email" in all_sql)
    c("a constraint's own index is not duplicated", "CREATE UNIQUE INDEX pk_cust" not in all_sql)
    c("a function-based index is declined", "ix_func" not in all_sql)
    c("a domain index is declined", "ix_text" not in all_sql)
    c("both declines are reported", "index_expression" in notes and "index_type" in notes)

    print("tables that must not be created")
    c("a queue table is held back", "q_tab" not in all_sql.lower())
    c("an Oracle Text internal table is held back", "dr$ix" not in all_sql.lower())

    print("nothing is applied")
    c("the plan says so", plan["nothing_applied"] is True)
    c("counts are reported", plan["counts"]["tables"] == 2, str(plan["counts"]))

    if offline:
        print(f"\n{c.ok}/{c.n} checks passed (offline; the proof was skipped)")
        return 0 if c.ok == c.n else 1

    print("proof: every statement runs on a real PostgreSQL, then rolls back")
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
        print(f"\n{c.ok}/{c.n} checks passed (the proof was skipped)")
        return 0 if c.ok == c.n else 1

    cur = conn.cursor()
    ran = failed = 0
    first_error = ""
    try:
        cur.execute("BEGIN")
        cur.execute("DROP SCHEMA IF EXISTS dbmig_app CASCADE")
        cur.execute(plan["schema"])
        cur.execute("CREATE SEQUENCE dbmig_app.seq_c")
        for stmt in ddl.statements_in_order(plan)[1:]:
            try:
                cur.execute(stmt)
                ran += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                first_error = first_error or f"{str(exc).splitlines()[0][:120]} :: {stmt[:80]}"
                cur.execute("ROLLBACK")
                cur.execute("BEGIN")
        c("every generated statement runs", failed == 0, f"{failed} failed: {first_error}")
        c("at least one table was created", ran > 0, str(ran))
    finally:
        conn.rollback()
        conn.close()

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
