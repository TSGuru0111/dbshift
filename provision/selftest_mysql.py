"""Phase 6 for a MySQL source: RDS for MySQL rendered from the source's own settings.

    python -m provision.selftest_mysql

No AWS: the lifecycle and parameter checks run against stub clients shaped like
the real responses recorded on 2026-09-30. What is protected:

  - the MySQL major is chosen from RDS standard support, never defaulted into
    Extended Support (8.0 left standard support on 2026-07-31);
  - the parameter group carries the source's sql_mode, collation and event
    scheduler, and forces what RDS needs (log_bin_trust_function_creators,
    local_infile);
  - SHOW VARIABLES' ON/OFF becomes the 0/1 RDS accepts for boolean parameters;
  - records judged for another target are refused, even from the same run;
  - a zero-item homogeneous SCT result still names its estate;
  - 4b/4c/4d artefacts are not claimed for an RDS for MySQL target.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "provision"

from . import policy, preflight, prepared, records, render

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}" + (f" -- {detail}" if detail else ""))


def _records(engine="MYSQL", gate_target="rds-mysql"):
    decision = {"engine": engine, "edition": None, "licence_model": None,
                "instance_class": "db.t3.medium", "vcpu": 2, "memory_gib": 4,
                "storage_gb": 20, "storage_type": "gp3", "character_set": "utf8mb4",
                "source_character_set": "utf8mb4"}
    return {
        "sizing": {"collector_run_id": "r1", "decision": decision,
                   "facts": {"segment_gb": 0.03, "utilization": {"basis": "capacity"}}},
        "gate": {"collector_run_id": "r1", "target": gate_target, "verdict": "PROCEED",
                 "by_phase": {"provision": {"status": "clear", "blocked_by": []}},
                 "source_of_findings": "aws-sct"},
        "assessment": {"collector_run_id": "r1", "findings": []},
        "remediation": {"collector_run_id": "r1", "target": gate_target, "entries": []},
    }


FACTS = {"source_engine": "MYSQL", "version": "8.0.46",
         "mysql_parameters": {
             "sql_mode": "STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
                         "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION",
             "character_set_server": "utf8mb4", "collation_server": "utf8mb4_0900_ai_ci",
             "event_scheduler": "ON", "explicit_defaults_for_timestamp": "ON",
             "lower_case_table_names": "0", "time_zone": "SYSTEM", "system_time_zone": "UTC"}}


def test_render():
    print("rendering RDS for MySQL")
    out = render.render(_records(), FACTS, engine_version="8.4.11",
                        stack_name="dbshift-target-x-mysql", estate="x",
                        now=datetime(2026, 9, 30, tzinfo=timezone.utc))
    res = out["template"]["Resources"]
    inst = res["DbInstance"]["Properties"]
    params = res["DbParameterGroup"]["Properties"]["Parameters"]
    check("the engine is mysql, general-public-license",
          (inst["Engine"], inst["LicenseModel"]) == ("mysql", "general-public-license"))
    check("the parameter group family follows the resolved major",
          res["DbParameterGroup"]["Properties"]["Family"] == "mysql8.4")
    check("the instance uses the parameter group",
          inst.get("DBParameterGroupName") == {"Ref": "DbParameterGroup"})
    check("no option group and no CharacterSetName on MySQL",
          "OptionGroup" not in res and "CharacterSetName" not in inst and "OptionGroupName" not in inst)
    check("port 3306, on the instance and the security group",
          inst["Port"] == "3306"
          and res["DbSecurityGroup"]["Properties"]["SecurityGroupIngress"][0]["FromPort"] == 3306)
    check("the source's sql_mode is carried -- RDS 8.4's default is only NO_ENGINE_SUBSTITUTION",
          params.get("sql_mode", "").startswith("STRICT_TRANS_TABLES"))
    check("the source's collation and event scheduler are carried",
          params.get("collation_server") == "utf8mb4_0900_ai_ci" and params.get("event_scheduler") == "ON")
    check("log_bin_trust_function_creators is forced to 1 (ERROR 1419 otherwise)",
          params.get("log_bin_trust_function_creators") == "1")
    check("local_infile is forced to 1 (DMS loads with LOAD DATA LOCAL)", params.get("local_infile") == "1")
    check("SHOW VARIABLES' ON becomes the 1 RDS accepts for a boolean parameter",
          params.get("explicit_defaults_for_timestamp") == "1")
    check("event_scheduler keeps ON/OFF, which is what RDS takes for it",
          params.get("event_scheduler") == "ON")
    check("time_zone is not set when the source is UTC",
          "time_zone" not in params and any(p["property"] == "TimeZone" and p["value"] == "UTC"
                                            for p in out["provenance"]))
    check("the render says MYSQL", out["target"] == "MYSQL" and out["port"] == 3306)
    try:
        render.render(_records(), FACTS, engine_version="5.7.44", stack_name="s", estate="x",
                      now=datetime(2026, 9, 30, tzinfo=timezone.utc))
        check("a major outside MYSQL_MAJORS is refused", False)
    except render.RenderError:
        check("a major outside MYSQL_MAJORS is refused", True)
    check("the stack name carries -mysql", render.stack_name_for("DBMIG_MYSQL_APP", "mysql")
          == "dbshift-target-dbmig-mysql-app-mysql")
    check("Oracle's stack name is unchanged", render.stack_name_for("DBMIG_APP", "oracle-ee")
          == "dbshift-target-dbmig-app")


class _Rds:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows, fail

    def describe_db_major_engine_versions(self, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied: not authorised")
        return {"DBMajorEngineVersions": self.rows}


def _life(major, std_end):
    return {"MajorEngineVersion": major, "SupportedEngineLifecycles": [
        {"LifecycleSupportName": "open-source-rds-standard-support",
         "LifecycleSupportEndDate": std_end}]}


def test_major():
    print("\nthe MySQL major comes from RDS standard support")
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    rows = [_life("8.0", datetime(2026, 7, 31, tzinfo=timezone.utc)),
            _life("8.4", datetime(2029, 7, 31, tzinfo=timezone.utc))]
    major, c = preflight.mysql_major(_Rds(rows), "8.0.46", now)
    check("8.0 past standard support -> 8.4, and it WARNs as an upgrade",
          major == "8.4" and c["status"] == preflight.WARN and "upgrade" in c["detail"])
    major, c = preflight.mysql_major(_Rds(rows), "8.0.46", datetime(2026, 1, 1, tzinfo=timezone.utc))
    check("while 8.0 is supported, the source's major is kept", major == "8.0" and c["status"] == preflight.PASS)
    major, c = preflight.mysql_major(_Rds([_life("8.4", datetime(2025, 1, 1, tzinfo=timezone.utc))]),
                                     "8.4.2", now)
    check("nothing in standard support FAILs -- Extended Support is never taken silently",
          c["status"] == preflight.FAIL)
    major, c = preflight.mysql_major(_Rds(fail=True), "8.0.46", now)
    check("an unreadable lifecycle WARNs about Extended Support, never PASSes",
          c["status"] == preflight.WARN and "Extended Support" in (c["remedy"] or ""))
    vd = preflight.version_direction({"version": "8.0.46"}, "mysql", "8.4.11")
    check("8.0 -> 8.4 is reported as an upgrade", vd["status"] == preflight.WARN and "UPGRADE" in vd["detail"])
    vd = preflight.version_direction({"version": "8.4.3"}, "mysql", "8.0.43")
    check("a downgrade FAILs", vd["status"] == preflight.FAIL)


class _Sess:
    def __init__(self, params):
        self.params = params

    def client(self, *a, **k):
        outer = self

        class _P:
            def paginate(self, **kw):
                yield {"EngineDefaults": {"Parameters": outer.params}}

        class _C:
            def get_paginator(self, name):
                return _P()
        return _C()


def test_parameters():
    print("\nparameters are checked against RDS's own list")
    known = [{"ParameterName": "explicit_defaults_for_timestamp", "IsModifiable": True, "AllowedValues": "0,1"},
             {"ParameterName": "event_scheduler", "IsModifiable": True, "AllowedValues": "ON,OFF"},
             {"ParameterName": "lower_case_table_names", "IsModifiable": True, "AllowedValues": "0-1"}]
    c = preflight.mysql_parameters_valid(_Sess(known), "mysql8.4", {"explicit_defaults_for_timestamp": "ON"})
    check("ON for a 0/1 parameter FAILs -- the rollback this check exists to prevent",
          c["status"] == preflight.FAIL and "not allowed" in c["detail"])
    c = preflight.mysql_parameters_valid(_Sess(known), "mysql8.4",
                                         {"explicit_defaults_for_timestamp": "1", "event_scheduler": "ON",
                                          "lower_case_table_names": "0"})
    check("correct values pass; a range is not mistaken for a list", c["status"] == preflight.PASS, c["detail"])
    c = preflight.mysql_parameters_valid(_Sess(known), "mysql8.4", {"no_such_param": "1"})
    check("an unknown parameter FAILs by name", c["status"] == preflight.FAIL and "no_such_param" in c["detail"])


def test_records():
    print("\nrecords must describe the target Phase 3 chose")
    recs = _records(engine="POSTGRESQL", gate_target="rds-mysql")
    c = records.consistency(recs)
    check("a MySQL-target gate beside a PostgreSQL sizing is refused, though the run matches",
          c["status"] == "fail" and "rds-mysql" in c["detail"])
    check("the matching pair passes", records.consistency(_records())["status"] == "pass")
    check("the 50-rule records (no target) are not affected",
          not records.target_mismatches({**_records(), "gate": {"collector_run_id": "r1"},
                                         "remediation": {"collector_run_id": "r1"}}))

    import json
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp) / "r9"
        run.mkdir()
        (run / "manifest.json").write_text(json.dumps(
            {"schemas": {"configured": ["dbmig_mysql_app"], "present": ["dbmig_mysql_app"]}}))
        est = records.estate_of({"collector_run_id": "r9", "findings": []}, Path(tmp))
        check("zero findings (MySQL -> RDS for MySQL) still name the estate from discovery",
              est == "dbmig_mysql_app")
        (run / "manifest.json").write_text(json.dumps({"schemas": {"configured": ["a", "b"]}}))
        try:
            records.estate_of({"collector_run_id": "r9", "findings": []}, Path(tmp))
            check("two configured schemas are refused rather than guessed", False)
        except records.RecordError as exc:
            check("two configured schemas are refused rather than guessed", "a, b" in str(exc))


def test_prepared():
    print("\n4b/4c/4d artefacts are not claimed for a MySQL target")
    c = prepared.check({"schema_ddl": {"estate": "x"}}, "x", "MYSQL")
    check("not applicable on MySQL -> RDS for MySQL, with where the schema comes from",
          c["status"] == "pass" and "not applicable" in c["detail"] and "mysqldump" in c["detail"])


def main() -> int:
    print("provision selftest -- MySQL source\n")
    test_render()
    test_major()
    test_parameters()
    test_records()
    test_prepared()
    print(f"\n{PASS}/{PASS + FAIL} checks passed" + (f", {FAIL} FAILED" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
