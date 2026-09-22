"""Which records Phase 6 reads, and what it does when they disagree.

The fault this covers, found on 2026-09-21 against a live run: a pipeline
assessed, remediated and gated entirely through AWS SCT left the three 50-rule
files untouched, so Phase 6 compared today's sizing against whatever the rules
path had written last and reported `records come from different collector
runs`. The consistency check was right -- it was reading the wrong three files.

The checks below fix the *selection* rule, not the consistency rule: SCT's
records where they exist, the 50-rule ones otherwise, and a mismatch still a
failure in both cases. Nothing here loosens the estate check.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from provision import preflight, records

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK ' if ok else 'XX '} {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


RUN = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sct_gate(run_id: str = RUN, verdict: str = "PROCEED") -> dict:
    return {
        "collector_run_id": run_id,
        "source_of_findings": "aws-sct",
        "verdict": verdict,
        "by_phase": {"provision": {"status": "clear", "blocked_by": []}},
        "blockers": [],
        "waived": [],
        "work": [
            {"issue_code": "5984", "owner": "DBMIG_TELCO", "title": "precision and scale",
             "objects": ["ORDERS"], "blocks_in_scope": False},
            {"issue_code": "5326", "owner": "DBMIG_TELCO", "title": "trigger status",
             "objects": ["T_AUDIT"], "blocks_in_scope": False},
        ],
    }


def _rules_gate(run_id: str = OTHER) -> dict:
    return {"collector_run_id": run_id, "verdict": "PROCEED", "critical_findings": 0,
            "by_phase": {"provision": {"status": "clear", "blocked_by": []}},
            "blockers": [], "waived": []}


def _paths(tmp: Path, *, sct: bool) -> dict:
    """A record set on disk. With `sct`, the SCT files exist beside the others."""
    _write(tmp / "assess" / "output" / "assessment.json",
           {"collector_run_id": OTHER,
            "findings": [{"rule_id": "RDS-004", "owner": "DBMIG_APP"}]})
    _write(tmp / "sizing" / "output" / "sizing.json", {"collector_run_id": RUN})
    _write(tmp / "remediate" / "output" / "remediation_plan.json",
           {"collector_run_id": OTHER, "entries": []})
    _write(tmp / "blocker" / "output" / "gate_decision.json", _rules_gate())
    if sct:
        _write(tmp / "remediate" / "output" / "sct_remediation_plan.json",
               {"collector_run_id": RUN, "source_of_findings": "aws-sct", "entries": []})
        _write(tmp / "blocker" / "output" / "sct_gate_decision.json", _sct_gate())
    return {
        "assessment": tmp / "assess" / "output" / "assessment.json",
        "sizing": tmp / "sizing" / "output" / "sizing.json",
        "remediation": tmp / "remediate" / "output" / "remediation_plan.json",
        "gate": tmp / "blocker" / "output" / "gate_decision.json",
    }


def main() -> int:
    print("provision.records -- which files, and whether they agree\n")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        original = dict(records.SCT_PATHS)
        try:
            # ---- SCT records present: they are preferred ---------------------
            paths = _paths(tmp, sct=True)
            records.SCT_PATHS.update({
                "remediation": tmp / "remediate" / "output" / "sct_remediation_plan.json",
                "gate": tmp / "blocker" / "output" / "sct_gate_decision.json"})
            recs = records.load(paths)

            check("SCT gate is preferred over the 50-rule gate",
                  recs["gate"].get("source_of_findings") == "aws-sct")
            check("SCT remediation is preferred",
                  recs["remediation"]["collector_run_id"] == RUN)
            check("the source is recorded", recs["_source"]["sct"] == ["gate", "remediation"])
            check("the assessment is derived from the SCT gate, not the stale file",
                  recs["assessment"]["source"] == "aws-sct"
                  and recs["assessment"]["collector_run_id"] == RUN,
                  str(recs["assessment"].get("collector_run_id")))
            check("a derived finding carries SCT's issue_code as its rule_id",
                  {f["rule_id"] for f in recs["assessment"]["findings"]} == {"5984", "5326"})

            c = records.consistency(recs)
            check("four SCT-sourced records read as consistent", c["status"] == "pass", c["detail"])
            check("the pass says the records came via SCT", "via AWS SCT" in c["detail"])
            check("estate_of reads the owner off a derived finding",
                  records.estate_of(recs["assessment"]) == "DBMIG_TELCO")
            check("_source is not compared as a record",
                  "_source" not in c["detail"])

            # ---- a real mismatch is still a failure ---------------------------
            _write(tmp / "blocker" / "output" / "sct_gate_decision.json", _sct_gate(run_id=OTHER))
            bad = records.load(paths)
            cb = records.consistency(bad)
            check("an SCT gate from another run still fails the check",
                  cb["status"] == "fail", cb["detail"])

            # ---- no SCT records: the 50-rule path is unchanged -----------------
            plain = _paths(tmp / "plain", sct=False)
            records.SCT_PATHS.update({
                "remediation": tmp / "plain" / "remediate" / "output" / "sct_remediation_plan.json",
                "gate": tmp / "plain" / "blocker" / "output" / "sct_gate_decision.json"})
            only_rules = records.load(plain)
            check("with no SCT files, the 50-rule records are read",
                  only_rules["_source"]["sct"] == []
                  and only_rules["gate"].get("source_of_findings") is None)
            check("the 50-rule assessment is used as-is, not derived",
                  only_rules["assessment"]["findings"][0]["rule_id"] == "RDS-004")
            check("prefer_sct=False forces the 50-rule records",
                  records.load(paths, prefer_sct=False)["_source"]["sct"] == [])
        finally:
            records.SCT_PATHS.clear()
            records.SCT_PATHS.update(original)

    # ---- gate_allows survives a gate with no critical_findings ---------------
    halted = _sct_gate(verdict="HALT")
    halted["blockers"] = [{"issue_code": "5984"}]
    got = preflight.gate_allows(halted)
    check("gate_allows does not raise on an SCT gate under HALT",
          got["status"] == preflight.WARN, str(got))
    check("it counts the SCT gate's blockers instead of critical_findings",
          "1 open" in got["detail"], got["detail"])
    rules_halt = {**_rules_gate(), "verdict": "HALT", "critical_findings": 4}
    check("the 50-rule gate still reports its own count",
          "4 open" in preflight.gate_allows(rules_halt)["detail"])

    print(f"\n{len(failures)} FAILED" if failures else "\nALL CHECKS PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
