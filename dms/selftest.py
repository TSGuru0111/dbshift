"""Offline checks for the DMS phase. No AWS, no database, no collector run.

Covers each way a DMS migration could quietly do the wrong thing: a CDC task
started against a source that cannot support it, a table silently missing from
the migration, identifiers that arrive in the wrong case, a target that already
holds rows, LOBs truncated without a word, or a task configured to destroy data
it did not create.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "dms"

from . import mappings, policy, preflight, residue

SCHEMA = "DBMIG_APP"


def _tables():
    return [
        {"owner": SCHEMA, "table_name": "CUSTOMER"},
        {"owner": SCHEMA, "table_name": "LOAN"},
        {"owner": SCHEMA, "table_name": "DR$IX_NOTES$I"},
        {"owner": SCHEMA, "table_name": "AQ$_Q_H"},
        {"owner": SCHEMA, "table_name": "MLOG$_LOAN"},
        {"owner": SCHEMA, "table_name": "MV_SUMMARY"},
        {"owner": SCHEMA, "table_name": "EXT_FEED"},
        {"owner": SCHEMA, "table_name": "LOOKUP", "iot_type": "IOT"},
        {"owner": "OTHER", "table_name": "NOT_MINE"},
    ]


def _objects():
    return [
        # A materialized view appears twice, which is the bug this guards.
        {"owner": SCHEMA, "object_name": "MV_SUMMARY", "object_type": "MATERIALIZED VIEW"},
        {"owner": SCHEMA, "object_name": "MV_SUMMARY", "object_type": "TABLE"},
        {"owner": SCHEMA, "object_name": "CUSTOMER", "object_type": "TABLE"},
        {"owner": SCHEMA, "object_name": "PKG_OPS", "object_type": "PACKAGE"},
        {"owner": SCHEMA, "object_name": "SEQ_ID", "object_type": "SEQUENCE"},
        {"owner": SCHEMA, "object_name": "V_ACTIVE", "object_type": "VIEW"},
    ]


def _facts(log_mode="ARCHIVELOG", suppl="YES"):
    return {"log_mode": log_mode, "supplemental_logging": suppl}


def _gate(**by_phase):
    return {"by_phase": {k: v for k, v in by_phase.items()}}


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = _Check()

    print("table selection")
    sel = mappings.select_tables(_tables(), _objects(), SCHEMA,
                                 external_tables=[{"table_name": "EXT_FEED"}])
    inc, exc = sel["include"], {e["table"]: e for e in sel["exclude"]}
    c("a real table is migrated", "CUSTOMER" in inc)
    c("another schema's table is not", "NOT_MINE" not in inc)
    c("an Oracle Text internal table is held back", "DR$IX_NOTES$I" in exc and "DR$IX_NOTES$I" not in inc)
    c("an AQ internal table is held back", "AQ$_Q_H" in exc and "AQ$_Q_H" not in inc)
    c("a materialized view log is held back", "MLOG$_LOAN" in exc and "MLOG$_LOAN" not in inc)
    c("a materialized view is held back even though it is also a TABLE",
      "MV_SUMMARY" in exc and "MV_SUMMARY" not in inc)
    c("an external table is held back", "EXT_FEED" in exc and "EXT_FEED" not in inc)
    c("the external exclusion names RDS-004", "RDS-004" in exc["EXT_FEED"]["why"])
    c("an index-organized table still migrates", "LOOKUP" in inc)
    c("the index-organized table carries a note", exc["LOOKUP"].get("included") is True)
    c("every exclusion explains itself", all(len(e["why"]) > 30 for e in sel["exclude"]))

    print("name folding")
    het = mappings.table_mappings(schema=SCHEMA, tables=["CUSTOMER"], lowercase=True)
    hom = mappings.table_mappings(schema=SCHEMA, tables=["CUSTOMER"], lowercase=False)
    transforms = [r for r in het["rules"] if r["rule-type"] == "transformation"]
    c("heterogeneous lowercases three levels", len(transforms) == 3,
      str([t["rule-target"] for t in transforms]))
    c("schema, table and column are all folded",
      {t["rule-target"] for t in transforms} == {"schema", "table", "column"})
    c("every transform is convert-lowercase",
      all(t["rule-action"] == "convert-lowercase" for t in transforms))
    c("homogeneous transforms nothing",
      not [r for r in hom["rules"] if r["rule-type"] == "transformation"])
    c("rule ids are unique", len({r["rule-id"] for r in het["rules"]}) == len(het["rules"]))
    c("named tables are selected individually",
      any(r.get("object-locator", {}).get("table-name") == "CUSTOMER"
          for r in het["rules"] if r["rule-type"] == "selection"))
    wild = mappings.table_mappings(schema=SCHEMA, tables=None, lowercase=False)
    c("no table list falls back to a wildcard",
      any(r.get("object-locator", {}).get("table-name") == "%" for r in wild["rules"]))

    print("task settings")
    full = mappings.task_settings(migration_type=policy.FULL_LOAD)
    cdc = mappings.task_settings(migration_type=policy.CDC_ONLY)
    c("the target is never truncated",
      full["FullLoadSettings"]["TargetTablePrepMode"] == "DO_NOTHING")
    c("a CDC-only task has no full-load section", "FullLoadSettings" not in cdc)
    c("LOBs have a declared ceiling", full["TargetMetadata"]["LobMaxSize"] == policy.LOB_MAX_KB)
    c("the LOB ceiling is generous", policy.LOB_MAX_KB >= 32768, str(policy.LOB_MAX_KB))
    c("a failing table is suspended, not skipped",
      full["ErrorBehavior"]["TableErrorPolicy"] == "SUSPEND_TABLE")
    c("DMS validation is on", full["ValidationSettings"]["EnableValidation"])
    c("control tables live in their own schema",
      full["ControlTablesSettings"]["ControlSchema"] == "awsdms_control")
    c("settings serialise as JSON", isinstance(json.dumps(full), str))

    print("objects DMS never moves")
    not_moved = mappings.excluded_objects(_objects(), SCHEMA)
    kinds = {o["object_type"] for o in not_moved}
    c("stored code is named as not moved", "PACKAGE" in kinds)
    c("sequences are named as not moved", "SEQUENCE" in kinds)
    c("views are named as not moved", "VIEW" in kinds)
    c("a plain table is not in the list", "TABLE" not in kinds)
    c("every entry says why", all(o["why"] for o in not_moved))

    print("CDC requirements")
    ok = preflight.cdc_possible(_facts(), [], policy.FULL_LOAD_AND_CDC)
    c("a clean source allows CDC", all(x["status"] == preflight.PASS for x in ok))

    noarch = preflight.cdc_possible(_facts(log_mode="NOARCHIVELOG"), [], policy.FULL_LOAD_AND_CDC)
    arch = [x for x in noarch if x["name"] == "cdc_archivelog"][0]
    c("NOARCHIVELOG fails CDC", arch["status"] == preflight.FAIL)
    c("the archivelog failure cites OPS-001", arch.get("rule_id") == "OPS-001")
    c("the archivelog failure says what to do", "ARCHIVELOG" in (arch.get("remedy") or ""))

    nosup = preflight.cdc_possible(_facts(suppl="NO"), [], policy.FULL_LOAD_AND_CDC)
    sup = [x for x in nosup if x["name"] == "cdc_supplemental_logging"][0]
    c("no supplemental logging fails CDC", sup["status"] == preflight.FAIL)
    c("it cites OPS-002", sup.get("rule_id") == "OPS-002")

    nokey = preflight.cdc_possible(
        _facts(), [{"rule_id": "DQ-001", "object_name": "AUDIT_SCRATCH"},
                   {"rule_id": "DQ-001", "object_name": "SCRATCH2"}], policy.FULL_LOAD_AND_CDC)
    keys = [x for x in nokey if x["name"] == "cdc_primary_keys"][0]
    c("keyless tables fail CDC", keys["status"] == preflight.FAIL)
    c("the keyless tables are named", set(keys["tables"]) == {"AUDIT_SCRATCH", "SCRATCH2"})
    c("the loss is described as silent", "without raising an error" in keys["remedy"])

    full_only = preflight.cdc_possible(_facts(log_mode="NOARCHIVELOG", suppl="NO"),
                                       [{"rule_id": "DQ-001", "object_name": "X"}],
                                       policy.FULL_LOAD)
    c("a full load needs none of it", all(x["status"] == preflight.PASS for x in full_only))
    c("and it says so", "not required" in full_only[0]["detail"])

    print("the gate")
    blocked = preflight.gate_allows(
        _gate(migrate_cdc={"status": "blocked", "blocked_by": ["OPS-001"]}),
        policy.FULL_LOAD_AND_CDC)
    c("a blocked CDC gate fails", blocked["status"] == preflight.FAIL)
    c("it names the blocking rule", "OPS-001" in blocked["detail"])
    allowed = preflight.gate_allows(
        _gate(migrate_full_load={"status": "clear"}), policy.FULL_LOAD)
    c("a clear full-load gate passes", allowed["status"] == preflight.PASS)
    c("full load and CDC read different gate phases",
      preflight.gate_allows(_gate(migrate_full_load={"status": "clear"},
                                  migrate_cdc={"status": "blocked", "blocked_by": ["X"]}),
                            policy.FULL_LOAD)["status"] == preflight.PASS)

    print("target safety")
    c("an empty target passes",
      preflight.target_is_empty({"customer": 0, "loan": 0})["status"] == preflight.PASS)
    non_empty = preflight.target_is_empty({"customer": 1200, "loan": 0})
    c("a non-empty target fails", non_empty["status"] == preflight.FAIL)
    c("the offending table is named", "customer" in non_empty["detail"])
    c("it explains that rows would be added, not replaced",
      "alongside" in non_empty["remedy"])
    c("unknown counts block rather than pass",
      preflight.target_is_empty(None)["status"] == preflight.BLOCKED)

    print("billing")
    c("an existing instance warns",
      preflight.no_instance_running(
          [{"ReplicationInstanceIdentifier": "dbshift-dms-x",
            "ReplicationInstanceStatus": "available"}], "dbshift-dms-x")["status"] == preflight.WARN)
    c("no instance passes",
      preflight.no_instance_running([], "dbshift-dms-x")["status"] == preflight.PASS)
    c("the instance is the smallest class", policy.INSTANCE_CLASS == "dms.t3.small")
    c("no Multi-AZ", policy.MULTI_AZ is False)
    c("names carry the kill switch prefix",
      policy.instance_name("DBMIG_APP").startswith("dbshift-")
      and policy.task_name("DBMIG_APP", policy.FULL_LOAD).startswith("dbshift-"))
    c("task names distinguish migration types",
      policy.task_name("E", policy.FULL_LOAD) != policy.task_name("E", policy.FULL_LOAD_AND_CDC))

    print("what DMS leaves behind")
    datasets = {
        "sequences": [
            {"owner": SCHEMA, "sequence_name": "SEQ_ID", "last_number": 60001, "increment_by": 1},
            {"owner": SCHEMA, "sequence_name": "SEQ_NO_NUM", "increment_by": 1},
            {"owner": "OTHER", "sequence_name": "NOT_MINE", "last_number": 5},
        ],
        "views": [
            {"owner": SCHEMA, "view_name": "VW_ACTIVE"},
            {"owner": SCHEMA, "view_name": "AQ$Q_TAB"},
        ],
        "materialized_views": [{"owner": SCHEMA, "mview_name": "MV_SUMMARY"}],
        "external_tables": [{"owner": SCHEMA, "table_name": "EXT_FEED"}],
    }
    r = residue.build(schema=SCHEMA, datasets=datasets, lowercase=True, model_available=False)
    by_name = {i["object_name"]: i for i in r["items"]}

    c("a sequence is a rule, not a judgement", by_name["SEQ_ID"]["tier"] == residue.RULE)
    c("the sequence restart value comes from the source",
      "RESTART WITH 60001" in by_name["SEQ_ID"]["sql"], by_name["SEQ_ID"]["sql"] or "")
    c("the sequence name is lowercased for PostgreSQL",
      "dbmig_app.seq_id" in by_name["SEQ_ID"]["sql"])
    c("a sequence without a recorded value is not guessed",
      by_name["SEQ_NO_NUM"]["sql"] is None
      and by_name["SEQ_NO_NUM"]["status"] == residue.MANUAL)
    c("another schema's sequence is ignored", "NOT_MINE" not in by_name)
    c("the sequence reason names the real danger",
      "reuses a key" in by_name["SEQ_ID"]["why"])

    c("a view needs the model tier", by_name["VW_ACTIVE"]["tier"] == residue.MODEL)
    c("an AQ internal view is not listed as work", "AQ$Q_TAB" not in by_name)
    c("a materialized view needs the model tier", by_name["MV_SUMMARY"]["tier"] == residue.MODEL)
    c("an external table needs a person", by_name["EXT_FEED"]["tier"] == residue.PERSON)
    c("the external table cites RDS-004", "RDS-004" in by_name["EXT_FEED"]["why"])

    c("without a fixture a model item stays MODEL_REQUIRED",
      by_name["VW_ACTIVE"]["status"] == residue.MODEL_REQUIRED)
    c("nothing is ever applied", r["nothing_applied"] is True)
    c("the note says no model ran", "blocked" in r["model_note"])
    c("every item explains why it exists", all(len(i["why"]) > 40 for i in r["items"]))
    c("every item says what is needed", all(i["what_is_needed"] for i in r["items"]))

    print("the model tier, when it is live")

    class _Stub:
        """Returns one fixed answer, so the wiring is tested, not the model."""

        def __init__(self, text):
            self.text = text
            self.calls = 0

        def complete(self, tier, prompt, **kw):
            self.calls += 1
            return {"text": self.text, "model_id": "stub-model",
                    "input_tokens": 1, "output_tokens": 1}

    good = _Stub('{"sql": "CREATE VIEW dbmig_app.v AS SELECT 1;", '
                 '"explain": "translated NVL to COALESCE", "caveat": "check grants"}')
    r = residue.build(schema=SCHEMA, datasets={
        **datasets, "views": [{"owner": SCHEMA, "view_name": "VW_ACTIVE", "text": "SELECT 1"}]},
        lowercase=True, model_available=True, client=good)
    by_name = {i["object_name"]: i for i in r["items"]}
    c("a live model answers a view item", by_name["VW_ACTIVE"]["status"] == residue.MODEL_CONVERTED,
      by_name["VW_ACTIVE"]["status"])
    c("the answer is labelled as model output", by_name["VW_ACTIVE"]["source"] == "bedrock")
    c("the model id is recorded", by_name["VW_ACTIVE"].get("model_id") == "stub-model")
    c("the SQL is carried", "CREATE VIEW" in (by_name["VW_ACTIVE"].get("sql") or ""))
    c("the model was actually called", good.calls >= 1)
    c("the note says the model ran", "live" in r["model_note"])
    c("still nothing is applied", r["nothing_applied"] is True)

    unusable = _Stub("I cannot translate this view.")
    r = residue.build(schema=SCHEMA, datasets={
        **datasets, "views": [{"owner": SCHEMA, "view_name": "VW_ACTIVE", "text": "SELECT 1"}]},
        lowercase=True, model_available=True, client=unusable)
    by_name = {i["object_name"]: i for i in r["items"]}
    c("an unusable reply leaves the item needing the model",
      by_name["VW_ACTIVE"]["status"] == residue.MODEL_REQUIRED)
    c("and nothing is invented in its place", not by_name["VW_ACTIVE"].get("sql"))

    empty = _Stub('{"sql": "", "explain": "no safe translation exists"}')
    r = residue.build(schema=SCHEMA, datasets={
        **datasets, "views": [{"owner": SCHEMA, "view_name": "VW_ACTIVE", "text": "SELECT 1"}]},
        lowercase=True, model_available=True, client=empty)
    by_name = {i["object_name"]: i for i in r["items"]}
    c("a model declining is not treated as an answer",
      by_name["VW_ACTIVE"]["status"] == residue.MODEL_REQUIRED)

    print("static stand-ins are labelled, never passed off as model output")
    answered = residue.apply_static([
        {"kind": "view", "object_name": "VW_ACTIVE", "tier": residue.MODEL,
         "status": residue.MODEL_REQUIRED, "what_is_needed": "x", "why": "y",
         "sql": None, "source": "rule", "detail": None}], "NO_SUCH_ESTATE")
    c("a missing fixture file changes nothing",
      answered[0]["status"] == residue.MODEL_REQUIRED)

    fixture_estate = "DBMIG_APP"
    real = residue.apply_static([
        {"kind": "view", "object_name": "VW_ACTIVE_LOANS", "tier": residue.MODEL,
         "status": residue.MODEL_REQUIRED, "what_is_needed": "x", "why": "y",
         "sql": None, "source": "rule", "detail": None}], fixture_estate)
    got = real[0]
    c("a fixture answers the item", got["status"] == residue.STATIC)
    c("the answer is labelled a static fixture", got["source"] == "static_fixture")
    c("no model id is claimed", got.get("model_id") is None)
    c("the fixture supplies SQL", bool(got.get("sql")))
    c("the fixture carries a caveat", bool(got.get("caveat")))

    suspended = residue._suspended([
        {"table": "LOAN", "state": "Table error", "full_load_rows": 10, "full_load_errors": 3},
        {"table": "CUSTOMER", "state": "Table completed", "full_load_rows": 100}])
    c("a suspended table becomes an item", len(suspended) == 1)
    c("a completed table does not", suspended[0]["object_name"] == "LOAN")
    c("the suspension explains it would otherwise look fine",
      "no later phase would call that a failure" in suspended[0]["why"])

    print("summary")
    s = preflight.summarise([
        {"name": "a", "status": preflight.PASS, "detail": ""},
        {"name": "b", "status": preflight.FAIL, "detail": ""},
        {"name": "c", "status": preflight.WARN, "detail": ""}])
    c("a failure means not ready", not s["ready"])
    c("the refusal names the check", s["refused_because"] == ["b"])
    c("a warning alone still passes",
      preflight.summarise([{"name": "w", "status": preflight.WARN, "detail": ""}])["ready"])

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
