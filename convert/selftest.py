"""Offline checks for Phase 4b. No database, no AWS, no collector run needed.

Covers each way the phase could lie: a construct silently dropped, Oracle
syntax surviving in the output, a conversion that creates the wrong object or
escalates privileges, a stale fixture served against changed source, a model
answer that is not JSON or omits a construct, and the compile gate passing with
no target. The sources here are copies of the two estates' real objects plus
two synthetic ones that fall outside the rule tier on purpose."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "convert"

from . import classify, gates, model, plan as plan_mod, policy, rules, target as target_mod

OWNER = "DBMIG_APP"

SOURCES = {
    "FUNCTION FN_CUSTOMER_FULL_NAME": """FUNCTION fn_customer_full_name (p_customer_id IN NUMBER)
RETURN VARCHAR2
IS
  v_name customer.full_name%TYPE;
BEGIN
  SELECT full_name INTO v_name FROM customer WHERE customer_id = p_customer_id;
  RETURN v_name;
EXCEPTION
  WHEN NO_DATA_FOUND THEN RETURN NULL;
END fn_customer_full_name;
""",
    "PACKAGE PKG_LOAN_OPS": """PACKAGE pkg_loan_ops AS
  PROCEDURE record_payment (p_loan_id IN NUMBER, p_amount IN NUMBER);
  FUNCTION  outstanding_balance (p_loan_id IN NUMBER) RETURN NUMBER;
END pkg_loan_ops;
""",
    "PACKAGE BODY PKG_LOAN_OPS": """PACKAGE BODY pkg_loan_ops AS

  PROCEDURE record_payment (p_loan_id IN NUMBER, p_amount IN NUMBER) IS
  BEGIN
    INSERT INTO payment_hist (loan_id, payment_date, amount_paid)
    VALUES (p_loan_id, SYSDATE, p_amount);
    COMMIT;
  END record_payment;

  FUNCTION outstanding_balance (p_loan_id IN NUMBER) RETURN NUMBER IS
    v_principal NUMBER;
    v_paid      NUMBER;
  BEGIN
    SELECT principal_amt INTO v_principal FROM loan WHERE loan_id = p_loan_id;
    SELECT NVL(SUM(amount_paid),0) INTO v_paid FROM payment_hist WHERE loan_id = p_loan_id;
    RETURN v_principal - v_paid;
  END outstanding_balance;

END pkg_loan_ops;
""",
    "PROCEDURE SP_BROKEN_DEMO": """PROCEDURE sp_broken_demo IS
BEGIN
  SELECT COUNT(*) INTO :dummy_missing_bind FROM nonexistent_table;
END;
""",
    "PROCEDURE SP_CLOSE_LOAN": """PROCEDURE sp_close_loan (p_loan_id IN NUMBER)
IS
BEGIN
  UPDATE loan SET loan_status = 'CLOSED' WHERE loan_id = p_loan_id;
  COMMIT;
END sp_close_loan;
""",
    "TRIGGER TRG_LOAN_STATUS_CHECK": """TRIGGER trg_loan_status_check
BEFORE UPDATE OF loan_status ON loan
FOR EACH ROW
BEGIN
  IF :OLD.loan_status = 'WRITTEN_OFF' AND :NEW.loan_status <> 'WRITTEN_OFF' THEN
    RAISE_APPLICATION_ERROR(-20001, 'Cannot reopen a written-off loan.');
  END IF;
END trg_loan_status_check;
""",
    "TYPE LoanNotice41_T": """TYPE             "LoanNotice41_T"               AS OBJECT ("SYS_XDBPD$" "XDB"."XDB$RAW_LIST_T","LoanId" NUMBER(38),"Message" VARCHAR2(4000 CHAR))FINAL INSTANTIABLE """,
    "TYPE TY_ADDRESS": """TYPE ty_address AS OBJECT (
  line1      VARCHAR2(100),
  line2      VARCHAR2(100),
  city       VARCHAR2(60),
  state      VARCHAR2(60),
  postal_code VARCHAR2(20),
  country    VARCHAR2(60)
);
""",
    "TYPE TY_PHONE_LIST": "TYPE ty_phone_list AS VARRAY(3) OF VARCHAR2(20);",
    # telco estate
    "TRIGGER TRG_INVOICE_AUDIT": """TRIGGER trg_invoice_audit
BEFORE UPDATE OF invoice_status ON invoice
FOR EACH ROW
BEGIN
  IF :NEW.invoice_status = 'PAID' AND :OLD.invoice_status <> 'PAID' THEN
    :NEW.due_on := NVL(:OLD.due_on, SYSDATE);
  END IF;
END;
""",
    "PROCEDURE SP_BAR_SUBSCRIBER": """PROCEDURE sp_bar_subscriber (p_subscriber_id IN NUMBER) AS
BEGIN
  UPDATE subscriber SET sub_status = 'BARRED' WHERE subscriber_id = p_subscriber_id;
  UPDATE device SET device_status = 'RETIRED'
   WHERE subscriber_id = p_subscriber_id AND device_status = 'IN_USE';
  COMMIT;
END;
""",
    # synthetic: outside the rule tier on purpose
    "FUNCTION FN_HIERARCHY": """FUNCTION fn_hierarchy (p_root IN NUMBER) RETURN VARCHAR2 IS
  v_path VARCHAR2(4000);
BEGIN
  SELECT LISTAGG(node_name, '/') WITHIN GROUP (ORDER BY LEVEL) INTO v_path
    FROM org_node START WITH node_id = p_root CONNECT BY PRIOR node_id = parent_id;
  RETURN v_path;
END fn_hierarchy;
""",
    "PROCEDURE SP_AUDIT_AUTONOMOUS": """PROCEDURE sp_audit_autonomous (p_msg IN VARCHAR2) IS
  PRAGMA AUTONOMOUS_TRANSACTION;
BEGIN
  INSERT INTO audit_log (msg) VALUES (p_msg);
  COMMIT;
END sp_audit_autonomous;
""",
}


def _obj(key: str, sha: str = "abc123") -> dict:
    otype, name = key.rsplit(" ", 1)
    return {"owner": OWNER, "object_type": otype, "object_name": name, "source_sha256": sha,
            "source_text": SOURCES[key], "line_count": SOURCES[key].count("\n") + 1}


def _inv(errors=None) -> dict:
    return {"errors_by_object": errors or {}, "invalid_objects": {}, "types": {(OWNER, "TY_ADDRESS"): {}, (OWNER, "TY_PHONE_LIST"): {}},
            "triggers": {}, "columns": [], "tables": [], "objects": [], "estate": OWNER, "collector_run_id": "selftest"}


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name: str, cond: bool, detail: str = ""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = _Check()
    known = {"TY_ADDRESS", "TY_PHONE_LIST"}
    print("classify")
    inv = _inv(errors={(OWNER, "PROCEDURE", "SP_BROKEN_DEMO"): ["line 3: PLS-00049"]})
    c("broken procedure is excluded", classify.route(_obj("PROCEDURE SP_BROKEN_DEMO"), inv)["route"] == classify.EXCLUDED)
    c("XDB-generated type is manual", classify.route(_obj("TYPE LoanNotice41_T"), inv)["route"] == classify.MANUAL)
    c("CONNECT BY routes to the model", classify.route(_obj("FUNCTION FN_HIERARCHY"), inv)["route"] == classify.MODEL)
    c("autonomous transaction is manual", classify.route(_obj("PROCEDURE SP_AUDIT_AUTONOMOUS"), inv)["route"] == classify.MANUAL)
    c("package spec is absorbed", classify.route(_obj("PACKAGE PKG_LOAN_OPS"), inv)["route"] == classify.ABSORBED)
    for k in ("FUNCTION FN_CUSTOMER_FULL_NAME", "PACKAGE BODY PKG_LOAN_OPS", "PROCEDURE SP_CLOSE_LOAN",
              "TRIGGER TRG_LOAN_STATUS_CHECK", "TYPE TY_ADDRESS", "TYPE TY_PHONE_LIST",
              "TRIGGER TRG_INVOICE_AUDIT", "PROCEDURE SP_BAR_SUBSCRIBER"):
        c(f"{k} routes to the rules", classify.route(_obj(k), inv)["route"] == classify.RULE)
    c("a construct in a comment does not count", not classify.scan("-- uses CONNECT BY\nBEGIN NULL; END;"))
    c("a construct in a string does not count", not classify.scan("BEGIN x := 'SYSDATE'; END;"))

    print("shadow")
    # A referenced table whose column takes a user-defined type. The shadow may
    # name that type only when it creates it in the same transaction.
    sh_inv = {**_inv(), "columns": [
        {"owner": OWNER, "table_name": "CUSTOMER", "column_id": 1, "column_name": "CUSTOMER_ID", "data_type": "NUMBER",
         "data_precision": 12, "data_scale": 0, "nullable": "N"},
        {"owner": OWNER, "table_name": "CUSTOMER", "column_id": 2, "column_name": "HOME_ADDRESS", "data_type": "TY_ADDRESS",
         "data_type_owner": OWNER, "nullable": "Y"}]}
    stmts, notes = target_mod.shadow_statements(sh_inv, OWNER, {"CUSTOMER"}, [])
    c("shadow: unconverted type becomes a TEXT placeholder", any("home_address TEXT" in st for st in stmts)
      and not any("dbmig_app.ty_address" in st for st in stmts))
    c("shadow: the placeholder is noted", any("HOME_ADDRESS" in n and "not converted" in n for n in notes))
    stmts, notes = target_mod.shadow_statements(sh_inv, OWNER, {"CUSTOMER"},
                                                ["CREATE TYPE dbmig_app.ty_address AS (street VARCHAR(80));"])
    c("shadow: converted type is used by the table", any("home_address dbmig_app.ty_address" in st for st in stmts) and not notes)
    c("shadow: a failed scaffold blocks rather than rejects",
      gates.compile_check({"ok": False, "shadow_failed": True, "message": "type x does not exist"}, "pg")["status"] == gates.BLOCKED)

    print("rules")
    fn = rules.convert(_obj("FUNCTION FN_CUSTOMER_FULL_NAME"), known_types=known)
    c("function: SELECT INTO became STRICT", "INTO STRICT v_name" in fn["ddl"])
    c("function: NUMBER param mapped", "p_customer_id NUMERIC" in fn["ddl"])
    c("function: RETURNS VARCHAR", "RETURNS VARCHAR" in fn["ddl"])
    c("function: %TYPE kept", "customer.full_name%TYPE" in fn["ddl"])
    c("function: definer rights recorded as not translated",
      any(x["oracle"] == "AUTHID_DEFINER" and x["handling"] == "not_translated" for x in fn["constructs"]))
    pkg = rules.convert(_obj("PACKAGE BODY PKG_LOAN_OPS"), known_types=known, spec_text=SOURCES["PACKAGE PKG_LOAN_OPS"])
    c("package: two members flattened", sorted(pkg["creates"]) == ["dbmig_app.pkg_loan_ops$outstanding_balance", "dbmig_app.pkg_loan_ops$record_payment"])
    c("package: procedure keeps COMMIT", "CREATE OR REPLACE PROCEDURE dbmig_app.pkg_loan_ops$record_payment" in pkg["ddl"] and "COMMIT;" in pkg["ddl"])
    c("package: NVL -> COALESCE and SYSDATE truncated", "COALESCE(SUM(amount_paid),0)" in pkg["ddl"] and "date_trunc('second', LOCALTIMESTAMP)" in pkg["ddl"])
    c("package: declared NUMBER variables mapped", "v_principal NUMERIC;" in pkg["ddl"])
    trg = rules.convert(_obj("TRIGGER TRG_LOAN_STATUS_CHECK"), known_types=known)
    c("trigger: two statements", len(trg["statements"]) == 2)
    c("trigger: RAISE EXCEPTION with the ORA number kept", "RAISE EXCEPTION 'Cannot reopen a written-off loan.' USING ERRCODE = 'P0001', DETAIL = 'ORA-20001'" in trg["ddl"])
    c("trigger: RETURN NEW added", "RETURN NEW;" in trg["statements"][0])
    c("trigger: bound to the table with the column list", "BEFORE UPDATE OF loan_status ON dbmig_app.loan" in trg["statements"][1])
    trg2 = rules.convert(_obj("TRIGGER TRG_INVOICE_AUDIT"), known_types=known)
    c("trigger: assignment to NEW kept", "NEW.due_on := COALESCE(OLD.due_on, date_trunc('second', LOCALTIMESTAMP));" in trg2["ddl"])
    ty = rules.convert(_obj("TYPE TY_ADDRESS"), known_types=known)
    c("object type -> composite", ty["ddl"].startswith("CREATE TYPE dbmig_app.ty_address AS (") and "postal_code VARCHAR(20)" in ty["ddl"])
    va = rules.convert(_obj("TYPE TY_PHONE_LIST"), known_types=known)
    c("varray -> domain with cardinality check", "CREATE DOMAIN dbmig_app.ty_phone_list AS VARCHAR(20)[]" in va["ddl"] and "cardinality(VALUE) <= 3" in va["ddl"])
    bar = rules.convert(_obj("PROCEDURE SP_BAR_SUBSCRIBER"), known_types=known)
    c("procedure with unnamed END converts", "CREATE OR REPLACE PROCEDURE dbmig_app.sp_bar_subscriber" in bar["ddl"])
    try:
        rules.convert(_obj("TYPE LoanNotice41_T"), known_types=known)
        c("quoted XDB type is declined", False)
    except rules.Declined:
        c("quoted XDB type is declined", True)
    commit_fn = {**_obj("FUNCTION FN_CUSTOMER_FULL_NAME"), "source_text": SOURCES["FUNCTION FN_CUSTOMER_FULL_NAME"].replace("RETURN v_name;", "COMMIT; RETURN v_name;")}
    try:
        rules.convert(commit_fn, known_types=known)
        c("function that commits is declined", False)
    except rules.Declined:
        c("function that commits is declined", True)
    stale_spec = SOURCES["PACKAGE PKG_LOAN_OPS"].replace("END pkg_loan_ops;", "  FUNCTION missing_one RETURN NUMBER;\nEND pkg_loan_ops;")
    try:
        rules.convert(_obj("PACKAGE BODY PKG_LOAN_OPS"), known_types=known, spec_text=stale_spec)
        c("spec member missing from the body is declined", False)
    except rules.Declined:
        c("spec member missing from the body is declined", True)
    c("referenced tables found", rules.referenced_tables(SOURCES["PACKAGE BODY PKG_LOAN_OPS"], {"LOAN", "PAYMENT_HIST", "CUSTOMER"}) == {"LOAN", "PAYMENT_HIST"})

    print("gates")
    obj = _obj("TRIGGER TRG_LOAN_STATUS_CHECK")
    found = classify.scan(obj["source_text"])
    c("trigger passes static/policy/parity", all(g["status"] == "pass" for g in (
        gates.static_check(trg, obj, rules.expected_names(obj)), gates.policy_check(trg, obj), gates.parity_check(trg, obj, found))))
    hostile = {**trg, "ddl": trg["ddl"] + "\n\nDROP TABLE dbmig_app.loan;", "statements": trg["statements"] + ["DROP TABLE dbmig_app.loan;"]}
    c("policy refuses DROP", gates.policy_check(hostile, obj)["status"] == "fail")
    definer = {**trg, "ddl": trg["ddl"].replace("LANGUAGE plpgsql", "LANGUAGE plpgsql SECURITY DEFINER"),
               "statements": [s.replace("LANGUAGE plpgsql", "LANGUAGE plpgsql SECURITY DEFINER") for s in trg["statements"]]}
    c("policy refuses SECURITY DEFINER", gates.policy_check(definer, obj)["status"] == "fail")
    wrong = {**trg, "statements": [s.replace("trg_loan_status_check", "trg_other") for s in trg["statements"]]}
    wrong["ddl"] = "\n\n".join(wrong["statements"])
    c("static refuses a different object name", gates.static_check(wrong, obj, rules.expected_names(obj))["status"] == "fail")
    dropped = {**trg, "constructs": [x for x in trg["constructs"] if x["oracle"] != "RAISE_APPLICATION_ERROR"]}
    c("parity refuses an unaccounted construct", gates.parity_check(dropped, obj, found)["status"] == "fail")
    residue = {**trg, "ddl": trg["ddl"].replace("RAISE EXCEPTION 'Cannot reopen a written-off loan.' USING ERRCODE = 'P0001', DETAIL = 'ORA-20001'",
                                               "RAISE_APPLICATION_ERROR(-20001, 'Cannot reopen a written-off loan.')")}
    c("parity refuses Oracle residue", gates.parity_check(residue, obj, found)["status"] == "fail")
    noreturn = {**trg, "ddl": trg["ddl"].replace("  RETURN NEW;\n", "")}
    c("parity refuses a trigger function without RETURN", gates.parity_check(noreturn, obj, found)["status"] == "fail")
    c("compile is blocked without a target", gates.compile_check(None, None)["status"] == "blocked")
    c("compile reports the PostgreSQL error", "42601" in gates.compile_check({"ok": False, "sqlstate": "42601", "message": "syntax error", "statement": 1}, "x")["detail"])
    c("approval is blocked without a person", gates.approval(None)["status"] == "blocked")
    c("verdict: only approval blocked -> READY_FOR_APPROVAL",
      gates.verdict([{"gate": "static", "status": "pass"}, {"gate": "compile", "status": "pass"}, {"gate": "approval", "status": "blocked"}]) == "READY_FOR_APPROVAL")
    c("verdict: compile blocked -> BLOCKED",
      gates.verdict([{"gate": "compile", "status": "blocked"}, {"gate": "approval", "status": "blocked"}]) == "BLOCKED")
    c("policy: statements split across dollar quotes", len(policy.split_statements(trg["ddl"])) == 2)

    print("model seam")
    with tempfile.TemporaryDirectory() as tmp:
        fx = Path(tmp) / "DBMIG_APP.json"
        fx.write_text(json.dumps({"estate": OWNER, "authored_on": "2026-09-12", "outputs": [{
            "owner": OWNER, "object_type": "FUNCTION", "object_name": "FN_HIERARCHY", "written_against": "sha-fixture",
            "statements": ["CREATE OR REPLACE FUNCTION dbmig_app.fn_hierarchy(IN p_root NUMERIC) RETURNS VARCHAR LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END; $$"],
            "constructs": [], "explain": "x"}]}), encoding="utf-8")
        fresh = _obj("FUNCTION FN_HIERARCHY", sha="sha-fixture")
        stale = _obj("FUNCTION FN_HIERARCHY", sha="sha-changed")
        c("fixture served when the source hash matches", model.static_convert(fresh, Path(tmp))[0] is not None)
        c("fixture refused as stale when the source changed", model.static_convert(stale, Path(tmp))[0] is None and "stale" in model.static_convert(stale, Path(tmp))[1])
        c("no fixture for an unknown object", model.static_convert(_obj("PROCEDURE SP_CLOSE_LOAN"), Path(tmp))[0] is None)

        hier = _obj("FUNCTION FN_HIERARCHY")
        found_h = classify.scan(hier["source_text"])
        good = {"statements": ["CREATE OR REPLACE FUNCTION dbmig_app.fn_hierarchy(IN p_root NUMERIC) RETURNS VARCHAR LANGUAGE plpgsql AS $$\nDECLARE v_path VARCHAR(4000);\nBEGIN\n  WITH RECURSIVE t AS (SELECT node_id, node_name, 1 AS lvl FROM org_node WHERE node_id = p_root UNION ALL SELECT n.node_id, n.node_name, t.lvl + 1 FROM org_node n JOIN t ON n.parent_id = t.node_id)\n  SELECT string_agg(node_name, '/' ORDER BY lvl) INTO STRICT v_path FROM t;\n  RETURN v_path;\nEND;\n$$"],
                "constructs": [{"oracle": "CONNECT_BY", "handling": "translated", "postgres": "WITH RECURSIVE", "note": "x"},
                               {"oracle": "LISTAGG", "handling": "translated", "postgres": "string_agg", "note": "x"},
                               {"oracle": "SELECT_INTO", "handling": "translated", "postgres": "INTO STRICT", "note": "x"},
                               {"oracle": "ORACLE_DECLARED_TYPES", "handling": "translated", "postgres": "mapped", "note": "x"},
                               {"oracle": "AUTHID_DEFINER", "handling": "not_translated", "postgres": None, "note": "definer rights not added"}],
                "explain": "recursive CTE", "caveat": None, "confidence": 0.8, "assumptions": []}

        class Stub:
            def __init__(self, text): self.text = text
            def complete(self, tier, prompt, **kw):
                return {"text": self.text, "model_id": "stub-model", "input_tokens": 1, "output_tokens": 1}

        audit = Path(tmp) / "audit.jsonl"
        conv, src = model.live_convert(hier, found_h, {}, client=Stub(json.dumps(good)), audit_path=audit)
        c("valid model JSON accepted and labelled bedrock", src == "bedrock" and conv["model_id"] == "stub-model")
        c("model conversion passes parity", gates.parity_check(conv, hier, found_h)["status"] == "pass")
        try:
            model.live_convert(hier, found_h, {}, client=Stub("not json at all"), audit_path=audit)
            c("non-JSON model output rejected", False)
        except model.ModelOutputInvalid:
            c("non-JSON model output rejected", True)
        missing = dict(good, constructs=[x for x in good["constructs"] if x["oracle"] != "CONNECT_BY"])
        try:
            model.live_convert(hier, found_h, {}, client=Stub(json.dumps(missing)), audit_path=audit)
            c("model output omitting a construct rejected", False)
        except model.ModelOutputInvalid:
            c("model output omitting a construct rejected", True)
        rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
        c("every model call wrote an audit row", len(rows) == 3 and {r["validator_verdict"] for r in rows} == {"accepted", "rejected"})

    print("plan")
    inv2 = _inv(errors={(OWNER, "PROCEDURE", "SP_BROKEN_DEMO"): ["line 3: PLS-00049"]})
    inv2["objects"] = [_obj(k) for k in SOURCES]
    with tempfile.TemporaryDirectory() as tmp:
        result = plan_mod.build(inv2, model_mode="off", pg_target=None, output_dir=Path(tmp))
        by = {f"{e['object_type']} {e['object_name']}": e["status"] for e in result["entries"]}
        c("plan: broken procedure excluded", by["PROCEDURE SP_BROKEN_DEMO"] == "EXCLUDED_BROKEN_ON_SOURCE")
        c("plan: spec absorbed", by["PACKAGE PKG_LOAN_OPS"] == "ABSORBED_INTO_BODY")
        c("plan: model-tier object needs the model", by["FUNCTION FN_HIERARCHY"] == "MODEL_REQUIRED")
        c("plan: manual objects are manual", by["TYPE LoanNotice41_T"] == "MANUAL" and by["PROCEDURE SP_AUDIT_AUTONOMOUS"] == "MANUAL")
        c("plan: rule conversions blocked only on compile without a target",
          all(by[k] == "BLOCKED" for k in ("FUNCTION FN_CUSTOMER_FULL_NAME", "PACKAGE BODY PKG_LOAN_OPS", "TRIGGER TRG_LOAN_STATUS_CHECK", "TYPE TY_ADDRESS")))
        c("plan: DDL file written per conversion", len(list((Path(tmp) / "plpgsql").glob("*.sql"))) == 8)
        c("plan: nothing claims to be model output", result["model_generation_enabled"] is False and result["static_outputs_used"] == 0)

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
