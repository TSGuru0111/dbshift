"""Checks for Phase 4d on a MySQL source.

    python -m appsql.selftest_mysql            # offline, then the parse proof if PostgreSQL is reachable
    python -m appsql.selftest_mysql --offline

Measured against scripts/demo-app-mysql/answer_key.json -- a key written and
verified independently of this package -- not against the phase's own report.

What is protected:
  - every seeded statement lands in the tier the key says, and on the MySQL
    catalogue (never scanned for ROWNUM);
  - `LIMIT a, b` becomes `LIMIT b OFFSET a` -- the swap is the whole point;
  - rewrites never touch string literals, MyBatis tags or `${}`;
  - a manual-tier construct is never sent to the model;
  - parity rejects MySQL residue; the prompt names MySQL, not Oracle;
  - Oracle's 4d is unchanged.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "appsql"

from . import classify, gates, plan, rules_mysql, transform

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "scripts" / "demo-app-mysql"


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _stmt(sql, **kw):
    return {"statement_id": kw.pop("sid", "s"), "sql": sql, "source_engine": "MYSQL",
            "dynamic": False, "dynamic_tags": [], **kw}


def against_key(check):
    print("\nthe seeded corpus, against its answer key")
    key = json.loads((CORPUS / "answer_key.json").read_text(encoding="utf-8"))
    r = plan.build(CORPUS / "mappers", model_mode="off", source_engine="MYSQL")
    check("the plan records MySQL", r["source_engine"] == "MYSQL")
    check(f"{key['expected_totals']['statements']} statements extracted",
          r["statement_count"] == key["expected_totals"]["statements"], str(r["statement_count"]))
    check("statement tiers match the key", r["statement_tiers"] == key["expected_totals"]["statement_tiers"],
          str(r["statement_tiers"]))
    by_sid = {e["statement_id"]: {c["id"] for c in e["constructs"]} for e in r["entries"]}
    for c in key["constructs"]:
        missing = [s for s in c["statements"] if c["construct"] not in by_sid.get(s, set())]
        check(f"{c['id']} {c['construct']} found where the key says", not missing, str(missing))
    oracle_ids = set(classify.by_id("ORACLE")) - set(classify.by_id("MYSQL"))
    check("no Oracle construct is ever found in MySQL SQL",
          not any(ids & oracle_ids for ids in by_sid.values()))
    manual = [e for e in r["entries"] if e["status"] == "MANUAL"]
    check("every manual statement says why, from the catalogue",
          all((e.get("manual") or {}).get("why_no_draft") for e in manual))
    rule = [e for e in r["entries"] if e["source"] == "rule"]
    check("the six rule-tier statements convert with rules alone", len(rule) == 6, str(len(rule)))
    check("...and each passes parity",
          all(any(g["gate"] == "parity" and g["status"] == gates.PASS for g in e["gates"]) for e in rule))
    return r


def rewrites(check):
    print("\nthe deterministic rewrites")
    conv = lambda sql: rules_mysql.convert(sql, classify.scan(sql, "MYSQL"))
    out = conv("SELECT a FROM t ORDER BY a LIMIT #{offset}, #{size}")["sql"]
    check("LIMIT offset, count becomes LIMIT count OFFSET offset",
          out.endswith("LIMIT #{size} OFFSET #{offset}"), out)
    out = conv("SELECT a FROM t LIMIT 20, 10")["sql"]
    check("...with literals too", out.endswith("LIMIT 10 OFFSET 20"), out)
    out = conv("SELECT `Customer_Id` FROM `customer` WHERE note = 'uses `x` and IFNULL(y)'")["sql"]
    check("backticks go and names lower-case", "SELECT customer_id FROM customer" in out, out)
    check("a string literal is never edited", "'uses `x` and IFNULL(y)'" in out, out)
    out = conv('SELECT IFNULL(a, 0) FROM t <if test="x != null">WHERE IFNULL(b,0) = #{x}</if>')["sql"]
    check("IFNULL -> COALESCE outside the tag", out.startswith("SELECT COALESCE(a, 0)"), out)
    check("the MyBatis tag's test attribute is untouched", '<if test="x != null">' in out, out)
    out = conv("SELECT a FROM t WHERE d >= CURDATE() AND e <= NOW() LOCK IN SHARE MODE")["sql"]
    check("CURDATE, NOW and LOCK IN SHARE MODE", out == ("SELECT a FROM t WHERE d >= CURRENT_DATE AND "
                                                          "e <= LOCALTIMESTAMP FOR SHARE"), out)
    try:
        conv("SELECT GROUP_CONCAT(a) FROM t LIMIT 0, 5")
        check("a model-tier construct declines the whole statement", False)
    except Exception as exc:  # noqa: BLE001
        check("a model-tier construct declines the whole statement -- no half conversion",
              "GROUP_CONCAT" in str(exc))
    check("every non-rule catalogue row is declined by the rules",
          set(rules_mysql.NOT_COVERED) == {c["id"] for c in classify.catalogue("MYSQL") if c["tier"] != "rule"})


def model_seam(check):
    print("\nthe model seam")
    calls = []

    class _Stub:
        def complete(self, tier, prompt, **kw):
            calls.append(prompt)
            return {"text": json.dumps({
                "sql": "SELECT o.order_id, string_agg(p.sku::text, ', ' ORDER BY l.line_no) AS skus FROM "
                       "customer_order o JOIN order_line l ON l.order_id = o.order_id JOIN product p "
                       "ON p.product_id = l.product_id WHERE o.customer_id = #{customerId} GROUP BY o.order_id",
                "constructs": [{"id": "MY_GROUP_CONCAT", "state": "translated", "postgres": "string_agg"}],
                "caveat": None}), "model_id": "stub"}

    s = _stmt("SELECT o.order_id, GROUP_CONCAT(p.sku ORDER BY l.line_no SEPARATOR ', ') AS skus FROM "
              "customer_order o JOIN order_line l ON l.order_id = o.order_id JOIN product p ON "
              "p.product_id = l.product_id WHERE o.customer_id = #{customerId} GROUP BY o.order_id",
              sid="orderSkus")
    c = transform.transform(s, model_mode="live", client=_Stub())
    check("a model-tier statement is drafted", c.get("source") == "bedrock", str(c.get("reason")))
    check("the prompt names MySQL, not Oracle",
          calls and "**MySQL** SQL statement" in calls[0] and "THE MYSQL STATEMENT" in calls[0]
          and "Oracle" not in calls[0].split("THE CONSTRUCTS")[0])
    g = gates.parity_check(c, s)
    check("its parity passes on the MySQL catalogue", g["status"] == gates.PASS, g["detail"])
    bad = dict(c, sql=c["sql"].replace("string_agg(p.sku::text, ', ' ORDER BY l.line_no)",
                                       "GROUP_CONCAT(p.sku)"))
    g = gates.parity_check(bad, s)
    check("MySQL residue fails parity", g["status"] == gates.FAIL and "no MySQL" not in g["detail"], g["detail"])

    calls.clear()
    m = _stmt("SELECT customer_id, @r := @r + 1 FROM customer, (SELECT @r := 0) i", sid="rank")
    out = transform.transform(m, model_mode="live", client=_Stub())
    check("a manual-tier construct is never sent to the model", not calls and out.get("manual"))
    check("...and says why, in the catalogue's words",
          "window function" in out["manual"]["why_no_draft"])
    # A construct id both engines share must still take MySQL's reason: Oracle's
    # names DBMIG_APP.ORDER -> order_col, a table 4c never creates from MySQL.
    rw = transform.manual_note([{"id": "RESERVED_WORD_OBJECT", "name": "reserved word",
                                 "postgres": "4c's name"}], "MYSQL")
    check("a shared manual construct is explained in MySQL's words, not Oracle's",
          rw and "Oracle" not in rw["why_no_draft"] and "MySQL name" in rw["why_no_draft"],
          (rw or {}).get("why_no_draft", ""))
    g = gates.result_check({"sql": "x"}, _stmt("x"))
    check("the result gate names the MySQL source", "MySQL source" in g["detail"], g["detail"])


def oracle_unchanged(check):
    print("\nOracle's 4d is unchanged")
    found = {c["id"] for c in classify.scan("SELECT NVL(a, 0) FROM dual WHERE ROWNUM <= 5")}
    check("an Oracle statement still finds NVL, DUAL and ROWNUM", {"NVL", "DUAL", "ROWNUM"} <= found, str(found))
    check("and no MySQL construct", not any(i.startswith("MY_") for i in found))


def proof(check):
    dsn, pw = os.environ.get("DBSHIFT_PG_DSN"), os.environ.get("DBSHIFT_PG_PASSWORD")
    ddl_path = ROOT / "convert" / "output" / "schema_ddl.json"
    ddl = json.loads(ddl_path.read_text(encoding="utf-8")) if ddl_path.exists() else None
    if not (dsn and pw) or not ddl or ddl.get("source_engine") != "MYSQL":
        print("\nthe parse proof was skipped: needs DBSHIFT_PG_DSN / _PASSWORD and a MySQL "
              "Phase 4c plan in convert/output/schema_ddl.json")
        return
    from convert.target import PgTarget
    print(f"\nproof: rule rewrites parsed on {dsn} against 4c's shadow, rolled back")
    r = plan.build(CORPUS / "mappers", model_mode="off", target=PgTarget.from_env(), ddl_plan=ddl,
                   source_engine="MYSQL")
    sh = r["shadow"] or {}
    check("the shadow was built from 4c's DDL", sh.get("tables_built", 0) > 0 and not sh.get("build_failures"),
          str(sh))
    rule = [e for e in r["entries"] if e["source"] == "rule"]
    failed = [e["statement_id"] for e in rule
              if not any(g["gate"] == "parse" and g["status"] == gates.PASS for g in e["gates"])]
    check("every rule-tier rewrite parses on PostgreSQL", rule and not failed, str(failed))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check = _Check()
    print("appsql selftest -- MySQL source")
    against_key(check)
    rewrites(check)
    model_seam(check)
    oracle_unchanged(check)
    if "--offline" not in argv:
        proof(check)
    print(f"\n{check.ok}/{check.n} checks passed" + ("" if check.ok == check.n else f", {check.n - check.ok} FAILED"))
    return 0 if check.ok == check.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
