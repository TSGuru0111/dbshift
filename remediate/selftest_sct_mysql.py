"""Offline checks for Phases 4 and 5 over AWS SCT on a MySQL source.

    python -m remediate.selftest_sct_mysql

No database, no AWS, no model. What is being checked:

  1. **Routing is per source.** SCT's codes are per source vendor, and the
     shared 999x codes mean different things per source: 9994 is Oracle AQ on
     DBMIG_APP and a MySQL EVENT on DBMIG_MYSQL_APP. Every code the real MySQL
     run raised must be mapped, and Oracle's routing must be unchanged.
  2. **A MySQL source is never judged by Oracle's machinery.** No source-side
     draft, no Oracle template, no Oracle wording in the model prompt.
  3. **CDC readiness speaks MySQL.** A MySQL source is never told to run ALTER
     DATABASE ARCHIVELOG, and binlog_format / binlog_row_image / REPLICATION
     CLIENT each produce their own unmet line.
  4. **A homogeneous pair's empty SCT result is CLEAR with a reason**, not a
     silent pass and not missing evidence.
  5. The target allow-list fix of 2026-09-29: comment text and FK referential
     actions no longer read as "rewrites data", and nothing else loosened.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "remediate"

from blocker import sct_gate
from collector import mode as migration_mode
from sct import route as sct_route

from . import pg_policy, sct_generate, sct_plan

PASS = FAIL = 0

# Every code the real SCT 1.0.677 run raised for DBMIG_MYSQL_APP -> RDS for
# PostgreSQL on 2026-09-29.
MYSQL_CODES = ["8706", "8795", "8811", "8825", "8829", "8844", "8850", "8859",
               "9994", "9997"]


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}\n       got  {got!r}\n       want {want!r}")


def truthy(label, got):
    check(label, bool(got), True)


def _issue(code, **kw):
    base = {"issue_code": code, "title": f"item {code}", "complexity": "simple",
            "occurrences": 1, "owner": "dbmig_mysql_app", "object_name": "obj",
            "object_type": "Procedures", "objects": ["obj"],
            "recommendation": "do the thing"}
    base.update(kw)
    return base


def test_routing():
    print("routing is per source")
    for code in MYSQL_CODES:
        r = sct_route.route(code, "MYSQL")
        check(f"MySQL {code} is mapped, not left to UNMAPPED", r["mapped"], True)
    for code in MYSQL_CODES:
        check(f"MySQL {code} never routes to the source (no MySQL source gates exist)",
              sct_route.route(code, "MYSQL")["where"] != sct_route.SOURCE, True)

    oracle_9994 = sct_route.route("9994")
    mysql_9994 = sct_route.route("9994", "MYSQL")
    truthy("9994 on Oracle still speaks of Advanced Queuing", "Queuing" in oracle_9994["why"])
    truthy("9994 on MySQL speaks of the EVENT, not AQ",
           "EVENT" in mysql_9994["why"] and "Queuing" not in mysql_9994["why"])
    check("a MySQL-only code is unmapped when no engine is given (Oracle table only)",
          sct_route.route("8706")["mapped"], False)
    check("9997 falls back to the shared row on MySQL",
          sct_route.route("9997", "MYSQL")["why"], sct_route.route("9997")["why"])
    check("an unknown MySQL code still routes to a person",
          sct_route.route("8999", "MYSQL")["who"], sct_route.PERSON)
    check("engine names are case-insensitive",
          sct_route.route("8706", "mysql")["mapped"], True)
    for code, row in sct_route.MYSQL_ROUTES.items():
        truthy(f"MySQL {code} says how and what to verify", row["how"] and row["verify"])
        truthy(f"MySQL {code} names no Oracle construct in its reason",
               "Oracle" not in row["why"])

    ann = sct_route.annotate([_issue("8795")], "MYSQL")[0]
    check("annotate() passes the engine through", ann["where"], sct_route.DECISION)
    seg = sct_route.segregate([_issue(c) for c in MYSQL_CODES], "MYSQL")
    check("segregate() on the MySQL run leaves nothing unmapped", seg["unmapped"], [])
    truthy("the source meaning is engine-neutral",
           "Oracle" not in sct_route.WHERE_MEANING[sct_route.SOURCE])


def test_plan():
    print()
    print("a MySQL source is never judged by Oracle's machinery")
    check("no Oracle template is offered for a MySQL item",
          sct_generate.template_fix(_issue("5639"), "MYSQL"), None)
    truthy("Oracle's template still applies to Oracle",
           sct_generate.template_fix(_issue("5639"), "ORACLE") is not None)

    prompt = sct_generate.TARGET_PROMPT.format(item="{}", route_why="w", clears_when="c",
                                               source_label="MySQL")
    truthy("the target prompt names MySQL as the source", "receive a MySQL migration" in prompt)
    truthy("...and says write PostgreSQL, not MySQL", "not MySQL" in prompt)
    check("...and no longer says Oracle anywhere", "Oracle" in prompt, False)

    # A hypothetical MySQL source route must stop before any gate or draft.
    saved = dict(sct_route.MYSQL_ROUTES)
    try:
        sct_route.MYSQL_ROUTES["8000"] = sct_route._r(
            sct_route.SOURCE, sct_route.MODEL, "why", "clears")
        e = sct_plan.plan_item(_issue("8000"), model_mode="live", source_engine="MYSQL")
        check("a MySQL source fix is a person's, never drafted",
              e["status"], sct_plan.HUMAN_AUTHORED)
        check("...with no gates run", e["gates"], [])
        check("...and no model asked", e["generated_by"], None)
        try:
            sct_generate.bedrock_fix(_issue("8000"), sct_route.route("8000", "MYSQL"),
                                     client=object(), source_engine="MYSQL")
            check("bedrock_fix refuses a MySQL source item", "drafted", "refused")
        except sct_generate.GenerationUnavailable:
            check("bedrock_fix refuses a MySQL source item", "refused", "refused")
    finally:
        sct_route.MYSQL_ROUTES.clear()
        sct_route.MYSQL_ROUTES.update(saved)

    e = sct_plan.plan_item(_issue("8795"), source_engine="MYSQL")
    check("8795 (collation) is a decision, with no statement",
          (e["status"], e["sql"]), (sct_plan.DECISION_REQUIRED, None))
    e = sct_plan.plan_item(_issue("8706"), model_mode="off", source_engine="MYSQL")
    check("8706 with the model off is a person's, with a reason",
          e["status"], sct_plan.HUMAN_AUTHORED)
    check("...and the route carried is MySQL's", e["where"], sct_route.TARGET)

    plan = sct_plan.build({"issues": [_issue(c) for c in MYSQL_CODES],
                           "target": {"id": "rds-postgresql"},
                           "source_engine": "MYSQL"})
    check("the plan records its source engine", plan["source_engine"], "MYSQL")
    check("every MySQL item is planned", plan["totals"]["items"], len(MYSQL_CODES))
    check("nothing on the MySQL plan is unmapped",
          [x["issue_code"] for x in plan["entries"] if not x["route_mapped"]], [])
    check("nothing is applied", plan["applied"], False)


def test_cdc():
    print()
    print("CDC readiness speaks MySQL")
    ready = {"log_mode": "ARCHIVELOG", "supplemental_logging": "YES",
             "binlog_format": "ROW", "binlog_row_image": "FULL",
             "replication_client": True}
    r = migration_mode.readiness("ARCHIVELOG", "YES", source_engine="MYSQL", native=ready)
    check("ROW + FULL + log_bin + REPLICATION CLIENT is ready", r["ready"], True)

    bad = {**ready, "binlog_format": "MIXED", "binlog_row_image": "MINIMAL",
           "replication_client": False}
    r = migration_mode.readiness("ARCHIVELOG", "NO", source_engine="MYSQL", native=bad)
    check("MIXED / MINIMAL / no grant is not ready", r["ready"], False)
    truthy("binlog_format is named", any("binlog_format is MIXED" in u for u in r["unmet"]))
    truthy("binlog_row_image is named",
           any("binlog_row_image is MINIMAL" in u for u in r["unmet"]))
    truthy("the missing grant is named", any("REPLICATION CLIENT" in u for u in r["unmet"]))
    check("no Oracle vocabulary in the unmet list",
          any("ARCHIVELOG" in u or "supplemental" in u for u in r["unmet"]), False)

    r = migration_mode.readiness("NOARCHIVELOG", "NO", source_engine="MYSQL", native={})
    truthy("log_bin off is said in MySQL's words",
           any("log_bin is off" in u for u in r["unmet"]))
    r = migration_mode.readiness(None, None, source_engine="MYSQL", native={})
    check("absent evidence is unknown, never ready", r["ready"], False)

    r = migration_mode.readiness("NOARCHIVELOG", "NO")
    truthy("Oracle's wording is unchanged without an engine",
           any("not ARCHIVELOG" in u for u in r["unmet"]))

    rec = migration_mode.decide("full-load-and-cdc", log_mode="NOARCHIVELOG",
                                supplemental_min="NO", source_engine="MYSQL", native={})
    check("the MySQL mode warning never mentions ARCHIVELOG",
          "ARCHIVELOG" in rec["warning"], False)
    truthy("...and names the binary log", "binary log" in rec["warning"])

    with_cdc = migration_mode.decide("full-load-and-cdc", declared=True)
    d = sct_gate.evaluate({"issues": [], "target": {"id": "rds-mysql"}},
                          facts={"log_mode": "ARCHIVELOG", "supplemental_logging": "NO",
                                 "binlog_format": "STATEMENT", "binlog_row_image": "FULL"},
                          migration_mode_record=with_cdc, source_engine="MYSQL")
    check("an unready MySQL binlog halts a CDC migration", d["verdict"], sct_gate.HALT)
    cdc = d["cdc_readiness"]
    check("the remedy is not ALTER DATABASE", "ALTER DATABASE" in cdc["clears_when"], False)
    truthy("the remedy names binlog_format", "binlog_format" in cdc["clears_when"])
    truthy("why-not-from-SCT names the binary log", "binary log" in cdc["why_not_from_sct"])
    check("the decision records its source engine", d["source_engine"], "MYSQL")

    d = sct_gate.evaluate({"issues": [], "target": {"id": "rds-mysql"}},
                          facts={"log_mode": "ARCHIVELOG", "supplemental_logging": "YES",
                                 "binlog_format": "ROW", "binlog_row_image": "FULL"},
                          migration_mode_record=with_cdc, source_engine="MYSQL")
    check("a ready MySQL binlog clears CDC", d["cdc_readiness"]["status"], "clear")
    truthy("...in MySQL's words", "binary log" in d["cdc_readiness"]["detail"])


def test_homogeneous():
    print()
    print("a homogeneous pair's empty SCT result is CLEAR with a reason")
    full = migration_mode.decide("full-load", declared=True)
    d = sct_gate.evaluate(
        {"issues": [], "target": {"id": "rds-mysql", "label": "Amazon RDS for MySQL",
                                  "sct_conversion": False}},
        facts={}, migration_mode_record=full, source_engine="MYSQL")
    check("MySQL -> RDS for MySQL with no items proceeds", d["verdict"], sct_gate.PROCEED)
    truthy("...and the summary says why zero is the expected answer",
           "same engine" in d["summary"] and "not missing evidence" in d["summary"])

    d = sct_gate.evaluate(
        {"issues": [], "target": {"id": "rds-postgresql", "sct_conversion": True}},
        facts={}, migration_mode_record=full, source_engine="MYSQL")
    check("a conversion pair with no items is NOT given the homogeneous reason",
          "same engine" in d["summary"], False)

    d = sct_gate.evaluate(
        {"issues": [_issue(c) for c in MYSQL_CODES],
         "target": {"id": "rds-postgresql", "sct_conversion": True}},
        facts={}, migration_mode_record=full, source_engine="MYSQL")
    check("the real MySQL -> PostgreSQL items block nothing on a full load",
          d["verdict"], sct_gate.PROCEED)
    check("...and every one is reported as work",
          sum(g["item_count"] for g in d["groups"]), len(MYSQL_CODES))
    check("...none unrouted", d["unrouted"], [])


def test_pg_policy():
    print()
    print("the target allow-list: comment text and FK actions, nothing else")
    ok = pg_policy.check_statement
    check("COMMENT text mentioning ON UPDATE is allowed",
          ok("COMMENT ON COLUMN a.b IS 'ON UPDATE CURRENT_TIMESTAMP needs a trigger'"), [])
    check("a foreign key ON UPDATE CASCADE is allowed",
          ok("ALTER TABLE a ADD CONSTRAINT f FOREIGN KEY (x) REFERENCES b (y) "
             "ON UPDATE CASCADE ON DELETE SET NULL"), [])
    for label, sql in (
        ("an UPDATE inside a single-quoted function body",
         "CREATE FUNCTION f() RETURNS void AS 'UPDATE t SET x = 1' LANGUAGE sql"),
        ("an UPDATE of a quoted table name",
         "CREATE FUNCTION f() RETURNS void AS 'UPDATE \"t\" SET x = 1' LANGUAGE sql"),
        ("an UPDATE in a CTE",
         "CREATE TABLE z AS WITH u AS (UPDATE t SET x = 1 RETURNING *) SELECT * FROM u"),
        ("an UPDATE after a comment", "COMMENT ON TABLE a IS 'x'; UPDATE t SET x = 1"),
        ("ON UPDATE followed by anything but a referential action",
         "ALTER TABLE a ADD CONSTRAINT f FOREIGN KEY (x) REFERENCES b ON UPDATE t SET x = 1"),
    ):
        truthy(f"still refused: {label}",
               any("rewrites data" in p for p in ok(sql)))

    class _Exc(Exception):
        pass

    check("pg8000's SQLSTATE is read from its dict",
          sct_plan._sqlstate(_Exc({"C": "3F000", "M": "schema does not exist"})), "3F000")
    e = _Exc("x")
    e.sqlstate = "42P01"
    check("psycopg's SQLSTATE is read from its attribute", sct_plan._sqlstate(e), "42P01")
    check("no SQLSTATE is None, not a guess", sct_plan._sqlstate(_Exc("plain")), None)

    class _Target:
        """A PostgreSQL whose every statement fails with one SQLSTATE."""
        def __init__(self, code):
            self.code, self.rolled_back = code, False

        def describe(self):
            return "fake"

        def connect(self):
            target = self

            class _Cur:
                def execute(self, sql):
                    raise _Exc({"C": target.code, "M": "fails"})

            class _Conn:
                def cursor(self):
                    return _Cur()

                def rollback(self):
                    target.rolled_back = True

                def close(self):
                    pass

            return _Conn()

    fix = {"sql": "ALTER TABLE dbmig_mysql_app.product ALTER COLUMN a TYPE jsonb"}
    t = _Target("3F000")
    g = sct_plan.pg_dry_run(fix, t)
    check("a schema the target does not have yet is BLOCKED (unproven), not rejected",
          g["status"], sct_plan.BLOCKED_GATE)
    truthy("...and the remedy is Phase 4c's DDL", "4c" in g["remedy"])
    truthy("...and the dry run was still rolled back", t.rolled_back)
    g = sct_plan.pg_dry_run(fix, _Target("42P01"))
    check("a wrong table in an existing schema still FAILS -- a hallucinated name "
          "is not excused", g["status"], sct_plan.FAIL)


def main() -> int:
    print("remediate + blocker over AWS SCT, MySQL source -- no model, no database, no AWS\n")
    test_routing()
    test_plan()
    test_cdc()
    test_homogeneous()
    test_pg_policy()
    print(f"\n{PASS}/{PASS + FAIL} checks passed" + (f", {FAIL} FAILED" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
