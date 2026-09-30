"""Checks for Phases 4b and 4c on a MySQL source.

    python -m convert.selftest_mysql            # offline, then the PostgreSQL proof if reachable
    python -m convert.selftest_mysql --offline

The offline half needs nothing. The **proof** half runs the generated schema DDL
on the local PostgreSQL inside a transaction that is rolled back -- the same
discipline as `selftest_ddl` -- because a foreign key pointing at the wrong
`PRIMARY`, or an unquoted default, only fails when executed.

What is being protected, each learned on the real DBMIG_MYSQL_APP run of
2026-09-29:

  - every MySQL primary key is called PRIMARY, so a foreign key's parent must
    come from the referenced table, not the constraint name;
  - nullability is YES/NO, defaults are unquoted, DEFAULT_GENERATED is not a
    generated column;
  - SELECT ... INTO must NOT be made STRICT on MySQL (the Oracle rule inverted);
  - parity checks MySQL residue on every construct, because there is no rule tier;
  - a FOR loop over a target LIST is a valid translation of a cursor loop.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "convert"

from . import classify, ddl, gates, model, plan, target, typemap_mysql as tm
from .typemap import Unmappable

OWNER = "dbmig_mysql_app"


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _col(table, name, dtype, ctype, pos, **kw):
    return {"owner": OWNER, "table_name": table, "column_name": name, "data_type": dtype.upper(),
            "data_type_mod": ctype, "column_id": pos, "nullable": kw.pop("nullable", "YES"),
            "extra": kw.pop("extra", ""), "identity_column": kw.pop("identity", "NO"), **kw}


def _datasets():
    cols = [
        _col("customer", "customer_id", "bigint", "bigint unsigned", 1, nullable="NO",
             extra="auto_increment", identity="YES", data_precision=20, data_scale=0),
        _col("customer", "country_code", "char", "char(2)", 2, nullable="NO", char_length=2,
             data_default="GB"),
        _col("customer", "is_verified", "tinyint", "tinyint(1)", 3, nullable="NO", data_default="0"),
        _col("customer", "created_at", "datetime", "datetime", 4, nullable="NO",
             data_default="CURRENT_TIMESTAMP", extra="DEFAULT_GENERATED",
             virtual_column="YES"),     # the old collector's mislabel, on purpose
        _col("customer", "loyalty_points", "int", "int unsigned", 5, nullable="NO", data_default="0"),
        _col("product", "product_id", "bigint", "bigint unsigned", 1, nullable="NO",
             extra="auto_increment", identity="YES"),
        _col("product", "status", "enum", "enum('draft','active','it''s')", 2, nullable="NO",
             data_default="draft"),
        _col("product", "fulfil_options", "set", "set('ship','collect')", 3, nullable="NO",
             data_default="ship"),
        _col("product", "attributes", "json", "json", 4),
        _col("product", "price_with_tax", "decimal", "decimal(14,4)", 5, extra="STORED GENERATED",
             generation_expression="(`unit_price` * 1.2)", data_precision=14, data_scale=4),
        _col("product", "updated_at", "datetime", "datetime", 6, nullable="NO",
             data_default="CURRENT_TIMESTAMP", extra="DEFAULT_GENERATED on update CURRENT_TIMESTAMP"),
        _col("customer_order", "order_id", "bigint", "bigint unsigned", 1, nullable="NO",
             extra="auto_increment", identity="YES"),
        _col("customer_order", "customer_id", "bigint", "bigint unsigned", 2, nullable="NO"),
        _col("customer_order", "balance_minor", "bigint", "bigint unsigned", 3, nullable="NO"),
        _col("order", "key", "int", "int unsigned", 1, nullable="NO", extra="auto_increment",
             identity="YES"),
        _col("order", "group", "varchar", "varchar(50)", 2, nullable="NO", char_length=50),
        _col("v_orders", "order_id", "bigint", "bigint unsigned", 1),
    ]
    return {
        "tables": [{"owner": OWNER, "table_name": t} for t in
                   ("customer", "product", "customer_order", "order", "v_orders")],
        "objects": [{"owner": OWNER, "object_name": "v_orders", "object_type": "VIEW"}],
        "columns": cols,
        "constraints": [
            {"owner": OWNER, "table_name": "customer", "constraint_name": "PRIMARY", "constraint_type": "P",
             "index_name": "PRIMARY"},
            {"owner": OWNER, "table_name": "product", "constraint_name": "PRIMARY", "constraint_type": "P",
             "index_name": "PRIMARY"},
            {"owner": OWNER, "table_name": "customer_order", "constraint_name": "PRIMARY",
             "constraint_type": "P", "index_name": "PRIMARY"},
            {"owner": OWNER, "table_name": "order", "constraint_name": "PRIMARY", "constraint_type": "P",
             "index_name": "PRIMARY"},
            {"owner": OWNER, "table_name": "customer_order", "constraint_name": "fk_order_customer",
             "constraint_type": "R", "r_owner": OWNER, "r_constraint_name": "PRIMARY",
             "delete_rule": "CASCADE", "update_rule": "NO ACTION"},
            {"owner": OWNER, "table_name": "customer", "constraint_name": "ck_points",
             "constraint_type": "C", "search_condition_vc": "(`loyalty_points` >= 0)"},
            {"owner": OWNER, "table_name": "customer", "constraint_name": "ck_mysql_fn",
             "constraint_type": "C", "search_condition_vc": "(ifnull(`country_code`,'') <> '')"},
        ],
        "constraint_columns": [
            {"owner": OWNER, "table_name": "customer", "constraint_name": "PRIMARY",
             "column_name": "customer_id", "position": 1},
            {"owner": OWNER, "table_name": "product", "constraint_name": "PRIMARY",
             "column_name": "product_id", "position": 1},
            {"owner": OWNER, "table_name": "customer_order", "constraint_name": "PRIMARY",
             "column_name": "order_id", "position": 1},
            {"owner": OWNER, "table_name": "order", "constraint_name": "PRIMARY",
             "column_name": "key", "position": 1},
            {"owner": OWNER, "table_name": "customer_order", "constraint_name": "fk_order_customer",
             "column_name": "customer_id", "position": 1, "referenced_table_schema": OWNER,
             "referenced_table_name": "customer", "referenced_column_name": "customer_id"},
        ],
        "indexes": [
            {"owner": OWNER, "table_name": "customer", "index_name": "ix_country", "index_type": "NORMAL",
             "uniqueness": "NONUNIQUE"},
            {"owner": OWNER, "table_name": "product", "index_name": "ix_country", "index_type": "NORMAL",
             "uniqueness": "NONUNIQUE"},
            {"owner": OWNER, "table_name": "product", "index_name": "ft_desc", "index_type": "FULLTEXT",
             "uniqueness": "NONUNIQUE"},
        ],
        "index_columns": [
            {"index_owner": OWNER, "table_name": "customer", "index_name": "ix_country",
             "column_name": "country_code", "column_position": 1},
            {"index_owner": OWNER, "table_name": "product", "index_name": "ix_country",
             "column_name": "status", "column_position": 1},
        ],
    }


def offline_typemap(check):
    print("\nthe MySQL type map")
    m = lambda **kw: tm.map_column(_col("t", "c", kw.pop("dt"), kw.pop("ct"), 1, **kw))[0]
    check("int unsigned widens to bigint", m(dt="int", ct="int unsigned") == "bigint")
    check("smallint unsigned widens to integer", m(dt="smallint", ct="smallint unsigned") == "integer")
    check("a bigint unsigned value column widens to numeric(20,0)",
          m(dt="bigint", ct="bigint unsigned") == "numeric(20,0)")
    check("a bigint unsigned AUTO_INCREMENT key stays bigint",
          m(dt="bigint", ct="bigint unsigned", extra="auto_increment") == "bigint")
    check("a bigint unsigned foreign key stays bigint, matching its parent",
          tm.map_column(_col("t", "customer_id", "bigint", "bigint unsigned", 1),
                        fk_columns={"customer_id"})[0] == "bigint")
    check("tinyint(1) stays smallint so DMS's 0/1 loads", m(dt="tinyint", ct="tinyint(1)") == "smallint")
    check("json -> jsonb", m(dt="json", ct="json") == "jsonb")
    check("datetime -> timestamp(0)", m(dt="datetime", ct="datetime") == "timestamp(0)")
    check("datetime(3) keeps its precision", m(dt="datetime", ct="datetime(3)") == "timestamp(3)")
    check("timestamp -> timestamptz", m(dt="timestamp", ct="timestamp").startswith("timestamptz"))
    check("MySQL TIME -> interval (838 hours does not fit time)", m(dt="time", ct="time") == "interval")
    check("float -> real (single precision, not truncated)", m(dt="float", ct="float") == "real")
    check("decimal keeps precision and scale",
          m(dt="decimal", ct="decimal(14,2)", data_precision=14, data_scale=2) == "numeric(14,2)")
    try:
        m(dt="geometry", ct="geometry")
        check("spatial is refused, not guessed", False)
    except Unmappable:
        check("spatial is refused, not guessed", True)
    check("enum members are read, a doubled quote unescaped",
          tm.enum_values(_col("t", "c", "enum", "enum('a','it''s')", 1)) == ["a", "it's"])
    check("YES/NO nullability is read", tm.is_not_null({"nullable": "NO"}) and not tm.is_not_null({"nullable": "YES"}))
    check("Oracle's Y/N still reads", tm.is_not_null({"nullable": "N"}))
    check("an unquoted literal default is quoted",
          tm.default_expr({"data_default": "GB"}, "char(2)") == ("'GB'", None))
    check("a quote inside a default is escaped",
          tm.default_expr({"data_default": "it's"}, "text")[0] == "'it''s'")
    check("a numeric default stays bare", tm.default_expr({"data_default": "0.00"}, "numeric(14,2)")[0] == "0.00")
    check("CURRENT_TIMESTAMP into a DATETIME is LOCALTIMESTAMP",
          tm.default_expr({"data_default": "CURRENT_TIMESTAMP", "extra": "DEFAULT_GENERATED"},
                          "timestamp(0)")[0] == "LOCALTIMESTAMP")
    check("an unknown expression default is declined with a reason",
          tm.default_expr({"data_default": "(rand())", "extra": "DEFAULT_GENERATED"}, "real")[0] is None)
    check("DEFAULT_GENERATED is not a generated column",
          not tm.is_generated({"extra": "DEFAULT_GENERATED on update CURRENT_TIMESTAMP"}))
    check("STORED GENERATED is", tm.is_generated({"extra": "STORED GENERATED"}))


def offline_ddl(check) -> dict:
    print("\nPhase 4c DDL from MySQL's catalogue")
    p = ddl.build(owner=OWNER, datasets=_datasets(), source_engine="MYSQL")
    sql = "\n".join(ddl.statements_in_order(p))
    notes = {(n["kind"], n["subject"]) for n in p["notes"]}
    check("the plan says it is MySQL", p.get("source_engine") == "MYSQL")
    check("a view is not built as a table", "v_orders" not in sql)
    check("NOT NULL is emitted from MySQL's NO", "country_code char(2) DEFAULT 'GB' NOT NULL" in sql, sql[:400])
    check("a DEFAULT_GENERATED column is kept, not dropped as computed",
          "created_at timestamp(0) DEFAULT LOCALTIMESTAMP NOT NULL" in sql)
    check("a STORED GENERATED column is omitted, with its expression in the note",
          "price_with_tax" not in sql
          and any("unit_price" in n["detail"] for n in p["notes"] if n["kind"] == "generated_column"))
    check("ON UPDATE CURRENT_TIMESTAMP is flagged for a trigger",
          ("on_update_timestamp", "product.updated_at") in notes)
    check("AUTO_INCREMENT becomes an identity", "customer_id bigint GENERATED BY DEFAULT AS IDENTITY" in sql)
    check("the identity restart is noted", any(k == "identity_restart" for k, _ in notes))
    check("the FK child column is bigint, matching its parent", "  customer_id bigint NOT NULL" in sql)
    check("a value column past bigint is numeric(20,0)", "balance_minor numeric(20,0)" in sql)
    check("the foreign key points at customer -- not the first PRIMARY in the schema",
          "FOREIGN KEY (customer_id) REFERENCES dbmig_mysql_app.customer (customer_id) ON DELETE CASCADE" in sql)
    check("primary keys get schema-unique <table>_pkey names",
          "customer_pkey PRIMARY KEY" in sql and "product_pkey PRIMARY KEY" in sql
          and "primary_col" not in sql.split("CREATE TABLE")[0])
    check("an ENUM becomes a CHECK on its members (quote escaped)",
          "status IN ('draft', 'active', 'it''s')" in sql)
    check("a SET becomes a string_to_array CHECK",
          "string_to_array(fulfil_options, ',') <@ ARRAY['ship', 'collect']::text[]" in sql)
    check("a MySQL check loses its backticks", "CHECK ((loyalty_points >= 0))" in sql)
    check("a check using a MySQL-only function is declined, not copied",
          "ifnull" not in sql.lower() and ("check_not_translated", "customer.ck_mysql_fn") in notes)
    check("a table named `order` becomes order_tbl, everywhere, with a note",
          "CREATE TABLE dbmig_mysql_app.order_tbl" in sql and "ALTER TABLE dbmig_mysql_app.order_tbl" in sql
          and ("reserved_word", "table order") in notes)
    # Created under its source name and renamed AFTER the load: a DMS column-rename
    # rule delivered the renamed columns as NULL on the real run (2026-09-30).
    check("a reserved column is created under its source name, quoted",
          '"group" varchar(50)' in sql and "group_col varchar" not in sql)
    order = ddl.statements_in_order(p)
    ren = [i for i, st in enumerate(order) if 'RENAME COLUMN "group" TO group_col' in st]
    first_key = min(i for i, st in enumerate(order) if "PRIMARY KEY" in st)
    check("...and renamed after the load, before any key names it",
          ren and ren[0] > max(i for i, st in enumerate(order) if st.startswith("CREATE TABLE"))
          and ren[0] < first_key)
    check("the reserved-word note no longer says 'not in Oracle'",
          not any("not in Oracle" in n["detail"] for n in p["notes"]))
    check("two tables' same-named indexes get distinct PostgreSQL names",
          "CREATE INDEX ix_country ON" in sql and "CREATE INDEX product_ix_country ON" in sql)
    check("FULLTEXT is noted, not emitted", "ft_desc" not in sql)
    return p


def offline_4b(check):
    print("\nPhase 4b classification, prompt, parity")
    inv = {"source_engine": "MYSQL", "errors_by_object": {}}
    src = ("CREATE DEFINER=`root`@`%` FUNCTION `f`(p BIGINT UNSIGNED) RETURNS text CHARSET utf8mb4\n"
           "READS SQL DATA DETERMINISTIC\nBEGIN\n  DECLARE v TEXT; -- IFNULL( in a comment\n"
           "  # GROUP_CONCAT( in a hash comment\n"
           "  SELECT GROUP_CONCAT(sku SEPARATOR ', ') INTO v FROM product WHERE note = \"IFNULL(\";\n"
           "  RETURN IFNULL(v, '');\nEND")
    obj = {"owner": OWNER, "object_type": "FUNCTION", "object_name": "f", "source_text": src,
           "source_sha256": "x"}
    r = classify.route(obj, inv)
    ids = {c["id"] for c in r["constructs"]}
    check("a MySQL routine routes to the model tier -- there is no rule tier", r["route"] == classify.MODEL)
    check("IFNULL and GROUP_CONCAT are found", {"MY_IFNULL", "MY_GROUP_CONCAT"} <= ids)
    check("IFNULL counted once: comments and double-quoted strings are not code",
          next(c for c in r["constructs"] if c["id"] == "MY_IFNULL")["count"] == 1)
    check("SELECT ... INTO is recognised", "MY_SELECT_INTO" in ids)
    check("no Oracle construct is ever found in MySQL code",
          not any(i in ids for i in classify.by_id("ORACLE")))
    manual = dict(obj, source_text="BEGIN LOAD DATA INFILE 'x' INTO TABLE t; END")
    check("LOAD DATA is a person's", classify.route(manual, inv)["route"] == classify.MANUAL)
    check("an Oracle inventory still uses the Oracle catalogue",
          "NVL" in {c["id"] for c in classify.scan("x := NVL(a, b);")})

    prompt = model.build_prompt(obj, r["constructs"], {}, "MYSQL")
    check("the prompt says MySQL source", "MySQL source:" in prompt and "Oracle source:" not in prompt)
    check("the MySQL system prompt forbids STRICT where NULL is tested",
          "do NOT add STRICT" in model.SYSTEM_PROMPT_MYSQL)
    check("the Oracle system prompt still demands STRICT", "INTO STRICT" in model.SYSTEM_PROMPT)
    sel = classify.by_id("MYSQL")["MY_SELECT_INTO"]
    check("the catalogue's SELECT INTO note is not the Oracle rule", "NOT the Oracle rule" in sel["note"])

    good = ("CREATE OR REPLACE FUNCTION dbmig_mysql_app.f(p bigint) RETURNS text LANGUAGE plpgsql STABLE AS $$\n"
            "DECLARE v text;\nBEGIN\n  SELECT string_agg(sku::text, ', ') INTO v FROM product;\n"
            "  RETURN COALESCE(v, '');\nEND;\n$$")
    accounting = [{"oracle": c["id"], "handling": "translated", "postgres": "x", "note": "n"}
                  for c in r["constructs"]]
    conv = {"statements": [good], "ddl": good, "constructs": accounting}
    check("a faithful conversion passes parity",
          gates.parity_check(conv, obj, r["constructs"], "MYSQL")["status"] == gates.PASS,
          gates.parity_check(conv, obj, r["constructs"], "MYSQL")["detail"])
    bad = good.replace("COALESCE(v, '')", "IFNULL(v, '')")
    g = gates.parity_check({**conv, "ddl": bad, "statements": [bad]}, obj, r["constructs"], "MYSQL")
    check("MySQL residue fails parity even though every construct is model-tier",
          g["status"] == gates.FAIL and "MySQL form of IFNULL" in g["detail"], g["detail"])
    bt = good.replace("FROM product", "FROM `product`")
    check("a surviving backtick fails parity",
          gates.parity_check({**conv, "ddl": bt, "statements": [bt]}, obj, r["constructs"],
                             "MYSQL")["status"] == gates.FAIL)

    cur_src = ("BEGIN DECLARE v_done INT DEFAULT 0; DECLARE a INT; DECLARE b INT;\n"
               "DECLARE cur CURSOR FOR SELECT x, y FROM t;\n"
               "DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;\n"
               "OPEN cur; l: LOOP FETCH cur INTO a, b; IF v_done = 1 THEN LEAVE l; END IF; END LOOP l; END")
    cobj = dict(obj, object_type="PROCEDURE", object_name="p", source_text=cur_src)
    cr = classify.route(cobj, inv)
    out = ("CREATE OR REPLACE PROCEDURE dbmig_mysql_app.p() LANGUAGE plpgsql AS $$\n"
           "DECLARE a int; b int;\nBEGIN\n  FOR a, b IN SELECT x, y FROM t LOOP\n    NULL;\n  END LOOP;\nEND;\n$$")
    acc = [{"oracle": c["id"], "handling": "translated", "postgres": "x", "note": "n"} for c in cr["constructs"]]
    g = gates.parity_check({"statements": [out], "ddl": out, "constructs": acc}, cobj, cr["constructs"], "MYSQL")
    check("FOR a, b IN query LOOP is accepted for a cursor loop (the real sp_reprice_category shape)",
          g["status"] == gates.PASS, g["detail"])

    reply = json.dumps({"statements": [good], "constructs": accounting, "explain": "e", "caveat": None,
                        "confidence": 0.9, "assumptions": []})
    check("the validator accepts MySQL construct ids", bool(model.validate_output(reply, obj, r["constructs"], "MYSQL")))
    try:
        model.validate_output(reply, obj, r["constructs"])
        check("...and the Oracle validator refuses them", False)
    except model.ModelOutputInvalid:
        check("...and the Oracle validator refuses them", True)

    class _Stub:
        def complete(self, tier, prompt, **kw):
            self.system = kw.get("system")
            return {"text": reply, "model_id": "stub"}

    stub = _Stub()
    full_inv = {"source_engine": "MYSQL", "collector_run_id": "t", "estate": OWNER, "objects": [obj],
                "errors_by_object": {}, "invalid_objects": {}, "columns": [], "tables": [],
                "triggers": {}, "types": {}}
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        res = plan.build(full_inv, model_mode="live", pg_target=None, compile=False, client=stub,
                         output_dir=Path(tmp) / "out")
    e = res["entries"][0]
    check("a stubbed model runs the whole MySQL plan", e["source"] == "bedrock")
    check("...with the MySQL system prompt", stub.system == model.SYSTEM_PROMPT_MYSQL)
    check("...and parity passed on the MySQL catalogue",
          any(g["gate"] == "parity" and g["status"] == gates.PASS for g in e["gates"]))
    check("the plan records its source engine", res["source_engine"] == "MYSQL")

    print("\nthe compile shadow reads MySQL columns")
    shadow_inv = {"source_engine": "MYSQL", "columns": _datasets()["columns"],
                  "constraints": _datasets()["constraints"],
                  "constraint_columns": _datasets()["constraint_columns"]}
    stmts, _ = target.shadow_statements(shadow_inv, OWNER, {"CUSTOMER_ORDER", "CUSTOMER"}, [])
    body = "\n".join(stmts)
    check("a shadow FK column is bigint, as on the real target", "customer_id bigint NOT NULL" in body, body)
    check("a nullable MySQL column is not made NOT NULL in the shadow",
          "is_verified smallint NOT NULL" in body and "attributes" not in body)


def proof(check, p: dict) -> None:
    dsn = os.environ.get("DBSHIFT_PG_DSN")
    pw = os.environ.get("DBSHIFT_PG_PASSWORD")
    if not (dsn and pw):
        print("\nthe proof was skipped: DBSHIFT_PG_DSN / DBSHIFT_PG_PASSWORD not set")
        return
    from . import ddl_run
    print(f"\nproof: the generated DDL on {dsn}, rolled back")
    p = dict(p, estate=OWNER, sequences_needed=[])
    try:
        r = ddl_run.compile_check(p, dsn, os.environ.get("DBSHIFT_PG_USER", "dbshift"), pw)
    except Exception as exc:  # noqa: BLE001
        print(f"  the proof was skipped: {exc}")
        return
    check("every generated statement runs on PostgreSQL", not r.get("failures"), str(r.get("failures"))[:300])
    check("...and statements actually ran -- zero run would also be zero failures",
          r.get("ran", 0) >= len(ddl.statements_in_order(p)) - 1, f"ran {r.get('ran')}")
    check("...and it was rolled back", r.get("rolled_back") is True)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check = _Check()
    print("convert selftest -- MySQL source")
    offline_typemap(check)
    p = offline_ddl(check)
    offline_4b(check)
    if "--offline" not in argv:
        proof(check, p)
    print(f"\n{check.ok}/{check.n} checks passed" + ("" if check.ok == check.n else f", {check.n - check.ok} FAILED"))
    return 0 if check.ok == check.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
