"""Self-test for the MySQL source on the real-SCT path.

Offline. Nothing here starts SCT or opens a database: what needs proving is that
the **scenario** is right for a MySQL source, and a generated `.scts` file can be
checked as text. Running SCT to find out that `vendor: 'ORACLE'` was hardcoded
would cost 25 minutes to learn something a string comparison establishes.

Three things this guards, in order of how badly they would fail:

  1. **The pair matrix.** `rds-oracle` is a real target id that does not exist
     from a MySQL source. A caller that forgets to pass the engine would silently
     get Oracle's list, and the first sign of it would be SCT connecting to MySQL
     with `vendor: 'ORACLE'`.
  2. **`sct_conversion` on MySQL -> MySQL.** AWS publishes no such conversion
     path, so zero action items is a *complete* result there. Phase 5's default
     -- absent evidence reports blocked -- is right everywhere else and wrong
     exactly here, so the flag has to be present and false.
  3. **The Oracle path is untouched.** Every Oracle assertion here is a
     regression test for the refactor that added the engine dimension, including
     that the cache directory names did not change: five real 25-minute
     assessments sit in `sct/output/` under the old names.

    python -m sct.selftest_mysql
"""

from __future__ import annotations

import sys
from pathlib import Path

import engines
from sct import runner as runner_mod
from sct import scenario as scenario_mod
from sct import targets as targets_mod
from sct import toolchain as toolchain_mod


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _script(engine, target_id, dsn, schemas=("s1",)):
    return scenario_mod.build_script(
        project_name="t", project_dir=Path("C:/t/p"), report_dir=Path("C:/t/r"),
        log_dir=Path("C:/t/l"), target_id=target_id, dsn=dsn, user="u",
        password="pw", schemas=list(schemas), jdbc_jar=Path("C:/t/d.jar"),
        source_engine=engine,
    )


def main(argv=None) -> int:
    c = _Check()

    print("the target matrix is per source")
    c("MySQL offers RDS MySQL and RDS PostgreSQL in scope",
      set(targets_mod.in_scope_ids("mysql")) == {"rds-mysql", "rds-postgresql"},
      str(targets_mod.in_scope_ids("mysql")))
    c("Oracle still offers RDS Oracle and RDS PostgreSQL",
      set(targets_mod.in_scope_ids("oracle")) == {"rds-oracle", "rds-postgresql"},
      str(targets_mod.in_scope_ids("oracle")))
    c("rds-oracle does not exist from a MySQL source", not any(
        t["id"] == "rds-oracle" for t in targets_mod.for_source("mysql")))
    c("rds-mysql does not exist from an Oracle source", not any(
        t["id"] == "rds-mysql" for t in targets_mod.for_source("oracle")))

    refused = ""
    try:
        targets_mod.get("rds-oracle", "mysql")
    except targets_mod.UnknownTarget as exc:
        refused = str(exc)
    c("asking for rds-oracle from MySQL is refused, and the message names the source",
      "MySQL" in refused and "rds-mysql" in refused, refused[:90])

    c("out-of-scope targets are listed and marked, never hidden -- a dropdown "
      "that omits Aurora looks like the tool cannot assess it",
      any(not t["in_scope"] for t in targets_mod.for_source("mysql")))
    c("an out-of-scope target carries engine=None, so it cannot leak into Phase 3",
      all(t["engine"] is None for t in targets_mod.for_source("mysql")
          if not t["in_scope"]))
    c("every MySQL target row carries sct_conversion",
      all("sct_conversion" in t for t in targets_mod.for_source("mysql")))

    print()
    print("assessment is not the same claim as conversion")
    c("MySQL -> PostgreSQL reports conversion work",
      targets_mod.converts("rds-postgresql", "mysql"))
    c("MySQL -> MySQL reports NO conversion work: AWS publishes no such path, so "
      "zero action items is a complete result and Phase 5 must read it as CLEAR",
      targets_mod.converts("rds-mysql", "mysql") is False)
    c("Oracle -> Oracle likewise reports no conversion work",
      targets_mod.converts("rds-oracle", "oracle") is False)
    c("the homogeneous scope note says why there is nothing, rather than leaving "
      "a client to wonder",
      "nothing to convert" in targets_mod.get("rds-mysql", "mysql")["scope_note"])

    print()
    print("the console payload")
    rows = targets_mod.for_console("mysql")
    c("in-scope targets sort first", not rows[0]["in_scope"] is False)
    c("exactly one row is the default", sum(1 for r in rows if r["default"]) == 1)
    c("the default is the pair with conversion work to show",
      next(r["id"] for r in rows if r["default"]) == "rds-postgresql")

    print()
    print("the scenario SCT actually receives")
    my = _script("mysql", "rds-postgresql", "10.0.1.42:3306/appdb")
    ora = _script("oracle", "rds-postgresql", "localhost:1521/XEPDB1")

    c("MySQL scenario declares vendor MYSQL", "-vendor: 'MYSQL'" in my, )
    c("Oracle scenario still declares vendor ORACLE", "-vendor: 'ORACLE'" in ora)
    # MEASURED, not assumed. SCT 1.0.677's `MySqlConnectionProperties` declares
    # serverName, port, username, password and useSSL -- and, unlike
    # `OracleConnectionProperties`, has no `$ConnectionType` inner class at all.
    #
    # Passing connectionType made SCT fall back to Oracle's property class:
    #   No enum constant OracleConnectionProperties.ConnectionType.BASIC
    # and then `Not found object(s) for path "Servers.MYSQL"`, because AddSource
    # had already failed. The second error is the one the console shows, and it
    # reads like a tree-path bug rather than a parameter that should be absent.
    c("MySQL emits NO connectionType -- its property class has no such concept",
      "-connectionType" not in my)
    c("Oracle keeps BASIC_SERVICE_NAME, because XEPDB1 is a pluggable database "
      "reached by service name and BASIC_SID would silently fail",
      "-connectionType: 'BASIC_SERVICE_NAME'" in ora)
    c("MySQL emits NO database parameter -- the schema filter scopes the "
      "assessment and MySqlConnectionProperties has nowhere to put one",
      "-database:" not in my and "-serviceName" not in my)
    c("Oracle passes -serviceName, not -database",
      "-serviceName: 'XEPDB1'" in ora and "-database:" not in ora)
    c("both still pass host, port, user and password",
      all(f"-{k}" in my for k in ("host", "port", "user", "password")))
    c("MySQL registers the driver under mysql_driver_file",
      '"mysql_driver_file"' in my)
    c("Oracle registers it under oracle_driver_file -- the keys are not "
      "interchangeable, and the wrong one fails inside AddSource as a driver "
      "error rather than a connection error",
      '"oracle_driver_file"' in ora)
    c("the MySQL source tree path follows the vendor name",
      "Servers.MYSQL.Schemas.%" in my and "Servers.MYSQL" in my)
    c("the Oracle tree path is unchanged", "Servers.ORACLE.Schemas.%" in ora)
    c("the schema filter is present on both -- without it SCT reports the whole "
      "instance and buries the client's objects",
      "DBShiftScope" in my and "DBShiftScope" in ora)
    c("both scenarios run the same ten commands in the same order",
      scenario_mod.command_names(my) == scenario_mod.command_names(ora),
      str(scenario_mod.command_names(my)))

    print()
    print("a MySQL DSN with or without a database produces the same AddSource")
    nodb = _script("mysql", "rds-postgresql", "10.0.1.42:3306")
    withdb = _script("mysql", "rds-postgresql", "10.0.1.42:3306/appdb")
    c("the database in the DSN is used by the COLLECTOR but never passed to SCT, "
      "so both DSNs generate an identical script", nodb == withdb)
    c("...and the scenario is complete either way",
      len(scenario_mod.command_names(nodb)) == 10)

    print()
    print("the password is still the only secret, and still redacted")
    c("the password appears in the generated MySQL script", "-password: 'pw'" in my)
    c("redacted() masks it", "-password: '***'" in scenario_mod.redacted(my))
    c("redaction leaves everything else intact",
      "-vendor: 'MYSQL'" in scenario_mod.redacted(my))

    print()
    print("DSN parsing per engine")
    c("MySQL splits host:port/database",
      scenario_mod.split_dsn("h:3306/db", "mysql") == ("h", 3306, "db"))
    c("MySQL allows the database to be absent",
      scenario_mod.split_dsn("h:3306", "mysql") == ("h", 3306, None))
    for bad, why in (("localhost:1521", "no service name"),
                     ("localhost/XEPDB1", "no port")):
        rejected = False
        try:
            scenario_mod.split_dsn(bad, "oracle")
        except ValueError:
            rejected = True
        c(f"Oracle refuses {bad!r} ({why})", rejected)
    rejected = False
    try:
        scenario_mod.split_dsn("h:33o6/db", "mysql")
    except ValueError:
        rejected = True
    c("MySQL refuses a non-numeric port rather than defaulting to 3306", rejected)

    print()
    print("the JDBC driver is per engine")
    c("MySQL looks for Connector/J under its own environment variable",
      toolchain_mod.ENV_MYSQL_JDBC == "DBSHIFT_MYSQL_JDBC_JAR")
    c("Oracle's variable is unchanged",
      toolchain_mod.ENV_JDBC == "DBSHIFT_ORACLE_JDBC_JAR")
    c("the two engines have different check names, so a preflight says which "
      "driver is missing",
      toolchain_mod._JDBC_BY_ENGINE["ORACLE"]["check_name"]
      != toolchain_mod._JDBC_BY_ENGINE["MYSQL"]["check_name"])
    c("MySQL's jar is matched by glob, because the official name carries its "
      "version (mysql-connector-j-8.4.0.jar and mysql-connector-java-8.0.33.jar "
      "are both real)",
      len(toolchain_mod._JDBC_BY_ENGINE["MYSQL"]["globs"]) >= 2)
    _, chk = toolchain_mod._find_jdbc("mysql")
    c("a missing MySQL driver names a remedy rather than just failing",
      chk.status == toolchain_mod.OK or bool(chk.remedy), chk.detail[:70])
    c("discover(mysql) checks the MySQL driver, not Oracle's",
      any(x.name == "mysql_jdbc_driver" for x in toolchain_mod.discover("mysql").checks))
    c("discover() with no engine still checks Oracle's",
      any(x.name == "oracle_jdbc_driver" for x in toolchain_mod.discover().checks))

    print()
    print("the result cache cannot collide across engines")
    key = runner_mod._cache_key
    c("rds-postgresql from MySQL and from Oracle are different directories -- the "
      "target id is the same on both, so without the engine one would overwrite "
      "the other",
      key("rds-postgresql", ["app"], "mysql") != key("rds-postgresql", ["app"], "oracle"))
    c("ORACLE's key is UNCHANGED, so the five real 25-minute assessments already "
      "in sct/output/ are still found",
      key("rds-postgresql", ["DBMIG_APP"], "oracle") == "DBMIG_APP-rds-postgresql",
      key("rds-postgresql", ["DBMIG_APP"], "oracle"))
    c("a caller that names no engine gets Oracle's historical key",
      key("rds-postgresql", ["DBMIG_APP"]) == "DBMIG_APP-rds-postgresql")
    c("MySQL keys are namespaced",
      key("rds-postgresql", ["app"], "mysql").startswith("mysql-"))
    c("MySQL schema case is preserved in the key: Sales and SALES are different "
      "databases on Linux and must not share a cache directory",
      key("rds-postgresql", ["Sales"], "mysql") != key("rds-postgresql", ["SALES"], "mysql"))
    c("Oracle schema case is still folded, because the engine folded it too",
      key("rds-postgresql", ["dbmig_app"], "oracle") == key("rds-postgresql", ["DBMIG_APP"], "oracle"))
    c("a different estate is a different directory",
      key("rds-postgresql", ["a"], "mysql") != key("rds-postgresql", ["b"], "mysql"))
    c("a different target is a different directory",
      key("rds-mysql", ["a"], "mysql") != key("rds-postgresql", ["a"], "mysql"))

    print()
    print("the cache key separates HOSTS -- a real collision, found 2026-09-29")
    # The same estate seeded on a local Docker MySQL and on an EC2 host produced
    # the same (engine, schemas, target) key, so the EC2 run served the
    # container's cached report. It looked correct -- identical action items,
    # because it IS the same estate -- which is what made it dangerous. A report
    # is evidence about a server.
    c("a local DSN and an EC2 DSN are different cache directories",
      key("rds-postgresql", ["app"], "mysql", "127.0.0.1:3399/app")
      != key("rds-postgresql", ["app"], "mysql", "3.108.190.1:3306/app"))
    c("localhost and 127.0.0.1 collapse to one tag: same machine, so re-running "
      "locally should still hit the cache",
      key("rds-postgresql", ["app"], "mysql", "localhost:3306/app")
      == key("rds-postgresql", ["app"], "mysql", "127.0.0.1:3306/app"))
    c("a local run carries no host segment, so existing directories are unchanged",
      key("rds-postgresql", ["app"], "mysql", "localhost:3306/app")
      == "mysql-app-rds-postgresql")
    c("the port is NOT in the key -- a tunnel or a port change is the same estate",
      key("rds-postgresql", ["app"], "mysql", "10.0.0.5:3306/app")
      == key("rds-postgresql", ["app"], "mysql", "10.0.0.5:3399/app"))
    c("the database is NOT in the key -- the schema filter governs scope",
      key("rds-postgresql", ["app"], "mysql", "10.0.0.5:3306/one")
      == key("rds-postgresql", ["app"], "mysql", "10.0.0.5:3306/two"))
    c("ORACLE with a local DSN still resolves to its historical directory, so the "
      "five real 25-minute assessments on disk are still found",
      key("rds-postgresql", ["DBMIG_APP"], "oracle", "localhost:1521/XEPDB1")
      == "DBMIG_APP-rds-postgresql")
    c("a host tag is filesystem-safe (no dots or colons)",
      "." not in key("rds-postgresql", ["app"], "mysql", "3.108.190.1:3306/app")
      and ":" not in key("rds-postgresql", ["app"], "mysql", "3.108.190.1:3306/app"))

    print()
    print("plan() is free, needs no password, and reports the pair honestly")
    pl = runner_mod.plan(target_id="rds-postgresql", dsn="127.0.0.1:3306/appdb",
                         user="u", schemas=["appdb"], source_engine="mysql")
    c("the plan records the source engine", pl["source_engine"] == engines.MYSQL)
    c("the plan carries sct_conversion for the gate to read",
      pl["target"]["sct_conversion"] is True)
    c("the plan lists the commands parsed back out of the real script, so it "
      "cannot drift from what would run", len(pl["commands"]) == 10)
    c("the plan names the output directory", "mysql-" in pl["output_dir"])
    hom = runner_mod.plan(target_id="rds-mysql", dsn="127.0.0.1:3306/appdb",
                          user="u", schemas=["appdb"], source_engine="mysql")
    c("the homogeneous plan reports sct_conversion False",
      hom["target"]["sct_conversion"] is False)

    print()
    print("routing an unknown code is honest rather than a guess")
    from sct import route as route_mod
    r = route_mod.route("99999")
    c("an unmapped SCT code routes to UNMAPPED with mapped=False, so a MySQL "
      "action item nobody has classified shows as unclassified rather than as a "
      "decision somebody made", r["mapped"] is False)
    c("a known Oracle code still maps", route_mod.route("5659")["mapped"] is True)

    print()
    print("facts measured against a real SCT 1.0.677 run, 2026-09-29")
    spec = scenario_mod.source_vendor("mysql")
    c("MySQL declares no connection type", spec["connection_type"] is None)
    c("MySQL declares no database parameter", spec["database_param"] is None)
    c("Oracle still declares both",
      scenario_mod.source_vendor("oracle")["connection_type"] == "BASIC_SERVICE_NAME"
      and scenario_mod.source_vendor("oracle")["database_param"] == "serviceName")
    # SCT demands SELECT and SHOW VIEW at SERVER scope (ON *.*), not per schema:
    #   "The specified account (dbmig_collector) does not have sufficient
    #    privileges for working with the following object(s):
    #    MYSQL Server : [SELECT, SHOW VIEW]"
    # Schema-scoped grants satisfy the collector and do NOT satisfy SCT.
    from engines import spec as _es
    privs = " ".join(_es.spec("mysql")["collector_privileges"]).upper()
    c("the engine spec names SELECT and SHOW VIEW, which SCT requires ON *.*",
      "SELECT" in privs and "SHOW VIEW" in privs)

    print()
    print(f"{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
