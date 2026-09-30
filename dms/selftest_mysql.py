"""Phase 7 for a MySQL source: the schema copy, the mappings, the residue, the preflight.

    python -m dms.selftest_mysql

No AWS and no database. What is protected, each found on the local rehearsal of
DBMIG_MYSQL_APP on 2026-09-30:

  - the homogeneous schema copy strips DEFINER (ERROR 1227 on RDS otherwise),
    creates events DISABLED, orders views after the views they read;
  - DMS renames what Phase 4c renamed, and does not load generated columns;
  - MySQL's "sequences" are AUTO_INCREMENT counters -- restarted as identities
    on PostgreSQL, set with AUTO_INCREMENT = on MySQL, never ALTER SEQUENCE;
  - a zero date in a NOT NULL column stops a PostgreSQL load before it starts;
  - a MySQL endpoint names no database and uses an SSL mode MySQL accepts;
  - DMS refuses records judged for another target.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "dms"

from . import actions, mappings, policy, preflight, residue, schema_mysql

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}" + (f" -- {detail}" if detail else ""))


class _Cur:
    """Answers the handful of SHOW CREATE / information_schema reads extract() makes."""

    def __init__(self):
        self._last = None
        self.description = None

    def execute(self, sql, args=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT default_character_set_name"):
            self._rows([("cs", "co")], [("utf8mb4", "utf8mb4_0900_ai_ci")])
        elif "FROM information_schema.tables" in s:
            self._rows([("name", "engine")], [("t1", "InnoDB"), ("t2", "MyISAM")])
        elif "FROM information_schema.views" in s:
            self._rows([("name",)], [("v_outer",), ("v_inner",)])
        elif "FROM information_schema.routines" in s:
            self._rows([("name", "type")], [("f1", "FUNCTION")])
        elif "FROM information_schema.triggers" in s:
            self._rows([("name",)], [("tr1",)])
        elif "FROM information_schema.events" in s:
            self._rows([("name", "status")], [("ev1", "ENABLED")])
        elif s.startswith("SHOW CREATE TABLE"):
            name = s.split("`.`")[-1].rstrip("`")
            eng = "MyISAM" if name == "t2" else "InnoDB"
            self._one((name, f"CREATE TABLE `{name}` (`id` int) ENGINE={eng} DEFAULT CHARSET=utf8mb4"))
        elif s.startswith("SHOW CREATE VIEW"):
            name = s.split("`.`")[-1].rstrip("`")
            body = ("SELECT * FROM `app`.`v_inner`" if name == "v_outer" else "SELECT 1 AS x")
            self._one((name, f"CREATE ALGORITHM=UNDEFINED DEFINER=`root`@`localhost` SQL SECURITY "
                             f"DEFINER VIEW `app`.`{name}` AS {body}", "utf8mb4", "x"))
        elif s.startswith("SHOW CREATE FUNCTION"):
            self._one(("f1", "", "CREATE DEFINER=`root`@`localhost` FUNCTION `f1`() RETURNS int "
                                 "DETERMINISTIC RETURN 1"))
        elif s.startswith("SHOW CREATE TRIGGER"):
            self._one(("tr1", "", "CREATE DEFINER=`root`@`localhost` TRIGGER `tr1` AFTER UPDATE ON "
                                  "`t1` FOR EACH ROW SET @x = 1"))
        elif s.startswith("SHOW CREATE EVENT"):
            self._one(("ev1", "SYSTEM", "", "CREATE DEFINER=`root`@`localhost` EVENT `ev1` ON SCHEDULE "
                                            "EVERY 1 DAY ON COMPLETION NOT PRESERVE ENABLE DO DELETE FROM t1"))
        else:
            raise AssertionError("unexpected SQL: " + s[:80])

    def _rows(self, cols, rows):
        names = cols[0] if len(cols) == 1 and isinstance(cols[0], tuple) else cols
        self.description = [(c,) for c in names]
        self._last = rows

    def _one(self, row):
        self._last = [row]

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0]


class _Conn:
    def cursor(self):
        return _Cur()


def test_schema_copy():
    print("the homogeneous schema copy")
    plan = schema_mysql.extract(_Conn(), "app")
    everything = plan["pre_load"] + plan["post_load"]
    check("DEFINER is removed from every view, routine, trigger and event",
          not any("DEFINER=" in x["sql"] for x in everything))
    check("SQL SECURITY is kept -- only the definer changes",
          any("SQL SECURITY DEFINER" in x["sql"] for x in plan["pre_load"] if x["kind"] == "VIEW"))
    check("a MyISAM table becomes InnoDB, with a note",
          "ENGINE=InnoDB" in next(x["sql"] for x in plan["pre_load"] if x["name"] == "t2")
          and any("MyISAM -> InnoDB" in n for n in plan["notes"]))
    views = [x["name"] for x in plan["pre_load"] if x["kind"] == "VIEW"]
    check("a view is created after the view it reads", views == ["v_inner", "v_outer"], str(views))
    kinds = [x["kind"] for x in plan["pre_load"]]
    check("tables, then routines, then views before the load",
          kinds.index("FUNCTION") > kinds.index("TABLE") and kinds.index("VIEW") > kinds.index("FUNCTION"))
    check("triggers and events come AFTER the load",
          {x["kind"] for x in plan["post_load"]} == {"TRIGGER", "EVENT"})
    ev = next(x["sql"] for x in plan["post_load"] if x["kind"] == "EVENT")
    check("the event is created DISABLED", "PRESERVE DISABLE" in ev and "ENABLE DO" not in ev, ev)
    check("and enabled only at cutover, because it was enabled on the source",
          plan["at_cutover"] and "ENABLE" in plan["at_cutover"][0]["sql"])
    check("strip_definer handles unquoted forms", schema_mysql.strip_definer(
        "CREATE DEFINER=root@localhost PROCEDURE p() BEGIN END")[0].startswith("CREATE PROCEDURE"))
    try:
        schema_mysql._q("x`; DROP")
        check("an unsafe identifier is never interpolated", False)
    except ValueError:
        check("an unsafe identifier is never interpolated", True)
    try:
        schema_mysql.apply(_Conn(), plan, "pre", approved_by="")
        check("applying needs a named approver", False)
    except PermissionError:
        check("applying needs a named approver", True)


def test_mappings():
    print("\ntable mappings")
    tm = mappings.table_mappings(
        schema="app", tables=["order", "customer"], lowercase=True,
        renames={"tables": {"order": "order_tbl"}, "columns": {("order", "group"): "group_col"}},
        remove_columns=[("product", "price_with_tax")])
    rules = tm["rules"]
    check("a table 4c renamed is renamed for DMS",
          any(r.get("rule-action") == "rename" and r.get("rule-target") == "table"
              and r["value"] == "order_tbl" for r in rules))
    check("and its reserved column too",
          any(r.get("rule-action") == "rename" and r.get("rule-target") == "column"
              and r["value"] == "group_col" for r in rules))
    lower = max(i for i, r in enumerate(rules) if r.get("rule-action") == "convert-lowercase")
    first_rename = min(i for i, r in enumerate(rules) if r.get("rule-action") == "rename")
    check("renames come after the lower-casing, so they have the last word", first_rename > lower)
    check("a generated column is removed from the load",
          any(r.get("rule-action") == "remove-column"
              and r["object-locator"]["column-name"] == "price_with_tax" for r in rules))
    ids = [int(r["rule-id"]) for r in rules]
    check("rule ids are unique and sequential", ids == list(range(1, len(ids) + 1)))
    homog = mappings.table_mappings(schema="app", tables=["order"], lowercase=False)
    check("MySQL -> MySQL transforms nothing",
          not any(r["rule-type"] == "transformation" for r in homog["rules"]))

    from dms import run as dms_run
    rn = dms_run._pg_renames("MYSQL", "app", ["order", "customer"],
                             [{"owner": "app", "table_name": "order", "column_name": "group"},
                              {"owner": "app", "table_name": "order", "column_name": "key"},
                              {"owner": "app", "table_name": "customer", "column_name": "desc"}])
    check("renames are 4c's own: order -> order_tbl", rn["tables"] == {"order": "order_tbl"})
    check("group and desc renamed, key (not reserved in PostgreSQL) left alone",
          rn["columns"] == {("order", "group"): "group_col", ("customer", "desc"): "desc_col"},
          str(rn["columns"]))


def test_residue():
    print("\nresidue")
    rows = [{"sequence_owner": "app", "sequence_name": "customer.customer_id",
             "table_name": "customer", "column_name": "customer_id", "last_number": 8192.0},
            {"sequence_owner": "app", "sequence_name": "order.key",
             "table_name": "order", "column_name": "key", "last_number": 12.0}]
    pg = residue.build(schema="app", datasets={"sequences": rows}, lowercase=True,
                       source_engine="MYSQL", target_engine="POSTGRESQL")
    sqls = [i["sql"] for i in pg["items"]]
    check("PostgreSQL: the identity is restarted at the source's next value",
          "ALTER TABLE app.customer ALTER COLUMN customer_id RESTART WITH 8192;" in sqls, str(sqls))
    check("...on the table and column as 4c named them",
          "ALTER TABLE app.order_tbl ALTER COLUMN key RESTART WITH 12;" in sqls, str(sqls))
    check("never ALTER SEQUENCE schema.table.column", not any("ALTER SEQUENCE" in s for s in sqls))
    my = residue.build(schema="app", datasets={"sequences": rows}, lowercase=False,
                       source_engine="MYSQL", target_engine="MYSQL")
    check("MySQL: AUTO_INCREMENT is set to the source's counter",
          "ALTER TABLE `app`.`customer` AUTO_INCREMENT = 8192;" in [i["sql"] for i in my["items"]])
    check("MySQL -> MySQL: views are not residue (the schema copy made them)",
          not any(i["kind"] == "view" for i in residue.build(
              schema="app", datasets={"sequences": [], "views": [{"owner": "app", "view_name": "v"}]},
              lowercase=False, source_engine="MYSQL", target_engine="MYSQL")["items"]))
    v = residue.build(schema="app", lowercase=True, source_engine="MYSQL", target_engine="POSTGRESQL",
                      datasets={"sequences": [], "views": [{"owner": "app", "view_name": "v_rev",
                                                            "text": "select sum(line_total) from x"}],
                                "columns": [{"owner": "app", "table_name": "x", "column_name": "line_total",
                                             "extra": "STORED GENERATED"}]})["items"]
    check("a view over a generated column says to create it after the column is added back",
          v and "line_total" in (v[0].get("create_after") or ""))
    check("the view's reason names MySQL, not Oracle", "IFNULL" in v[0]["why"] and "NVL" not in v[0]["why"])


def test_preflight():
    print("\npreflight")
    z = [{"table_name": "contract_term", "column_name": "expires_on", "nullable": "NO",
          "zero_date_count": 1, "zero_in_date_count": 0}]
    check("a zero date in a NOT NULL column FAILs a PostgreSQL load",
          preflight.zero_dates(z, "POSTGRESQL")["status"] == preflight.FAIL)
    check("in a nullable column it WARNs: it arrives as NULL, which Phase 8 reports",
          preflight.zero_dates([{**z[0], "nullable": "YES"}], "POSTGRESQL")["status"] == preflight.WARN)
    check("on MySQL -> MySQL it WARNs about the carried strict sql_mode",
          preflight.zero_dates(z, "MYSQL")["status"] == preflight.WARN
          and "sql_mode" in preflight.zero_dates(z, "MYSQL")["detail"])
    check("no zero dates PASSes", preflight.zero_dates([], "POSTGRESQL")["status"] == preflight.PASS)
    recs = {"sizing": {"collector_run_id": "r", "decision": {"engine": "POSTGRESQL"}},
            "gate": {"collector_run_id": "r", "target": "rds-mysql"}}
    check("DMS refuses a gate judged for another target", preflight.records_consistent(recs)["status"] == preflight.FAIL)


def test_endpoints():
    print("\nendpoints")
    made = []

    class _Dms:
        def describe_endpoints(self):
            return {"Endpoints": []}

        def create_endpoint(self, **kw):
            made.append(kw)
            return {"Endpoint": {"EndpointArn": kw["EndpointIdentifier"]}}

    actions.create_endpoints(
        _Dms(), estate="app", run_id="r",
        source={"engine": "mysql", "host": "10.0.0.5", "port": 3306, "user": "u", "password": "p",
                "database": None, "ssl_mode": policy.MYSQL_SSL_MODE},
        target={"engine": "mysql", "host": "rds", "port": 3306, "user": "a", "password": "p",
                "database": None, "ssl_mode": policy.MYSQL_SSL_MODE,
                "extra_settings": policy.MYSQL_TARGET_ATTRIBUTES},
        emit=lambda e: None)
    check("a MySQL endpoint names no database", all("DatabaseName" not in m for m in made))
    check("MySQL endpoints use an SSL mode MySQL accepts (no 'require')",
          all(m["SslMode"] in ("none", "verify-ca", "verify-full") for m in made))
    check("the MySQL target disables foreign key checks for the load",
          "FOREIGN_KEY_CHECKS=0" in (made[1].get("ExtraConnectionAttributes") or ""))
    check("passwords never reach a tag", not any("p" == t.get("Value") for m in made for t in m["Tags"]))


def main() -> int:
    print("dms selftest -- MySQL source\n")
    test_schema_copy()
    test_mappings()
    test_residue()
    test_preflight()
    test_endpoints()
    print(f"\n{PASS}/{PASS + FAIL} checks passed" + (f", {FAIL} FAILED" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
