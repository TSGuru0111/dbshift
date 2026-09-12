"""Offline checks for Phase 9. No AWS, no Oracle, no real output folders.

Every case here is a way a cutover certificate could lie: say ready when the
records describe another database, when the validation is stale, when the gate
blocks, or when a status string says "validated" over a report that contradicts
it. Phase 8 shipped that last bug for real, so it is tested rather than trusted.

    python -m cutover.selftest
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "cutover"

from . import requirements as R
from . import run as cutover_run

RUN = "6e48d16a-a3a1-4d17-87b9-c1bb0d3ae631"
OTHER = "83eadb57-30ae-43f5-95c0-058b523e216b"
PASS, FAIL = "pass", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, got, want) -> None:
    ok = got == want
    results.append((PASS if ok else FAIL, name, "" if ok else f"got {got!r}, wanted {want!r}"))


def iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def records(run_id: str = RUN) -> dict:
    return {
        "assessment": {"collector_run_id": run_id,
                       "findings": [{"rule_id": "RDS-005", "owner": "DBMIG_APP"},
                                    {"rule_id": "SEC-006", "owner": "DBMIG_APP"}]},
        "sizing": {"collector_run_id": run_id},
        "remediation": {"collector_run_id": run_id, "entries": [
            {"rule_id": "RDS-006", "object_name": "JOB_REFRESH_LOAN_SUMMARY", "source": "static_fixture",
             "artefact": {"type": "target_runbook", "steps": [
                 {"applies_to_phase": "migrate", "when": "after metadata import",
                  "sql_on_target": "BEGIN DBMS_SCHEDULER.DISABLE('DBMIG_APP.JOB_REFRESH_LOAN_SUMMARY'); END;"},
                 {"applies_to_phase": "cutover", "when": "after validation passes",
                  "sql_on_target": "BEGIN DBMS_SCHEDULER.ENABLE('DBMIG_APP.JOB_REFRESH_LOAN_SUMMARY'); END;"}]}},
            {"rule_id": "DQ-008", "object_name": "LOAN.LEGACY_SCORE", "source": "static_fixture",
             "artefact": {"type": "post_cutover_note", "note": "LOAN.LEGACY_SCORE held no values."}},
            {"rule_id": "RDS-015", "object_name": "NLS_CHARACTERSET", "source": "static_fixture",
             "artefact": {"type": "provision_parameter", "applies_to_phase": "provision"}}]},
        "gate": {"collector_run_id": run_id, "verdict": "HALT", "by_phase": {
            "cutover": {"status": "blocked", "blocked_by": ["OPS-001", "OPS-002"]},
            "migrate_cdc": {"status": "blocked", "blocked_by": ["DQ-001", "OPS-001", "OPS-002"]}}},
    }


def report(**over) -> dict:
    base = {"run_id": RUN, "estate": "DBMIG_APP", "status": "validated", "mismatches": 0,
            "not_comparable": 0, "finished_at_utc": iso(0.5),
            "levels": [{"level": n, "title": t} for n, t in
                       ((1, "Objects"), (2, "Structure"), (3, "Row counts"),
                        (4, "Data content"), (5, "Behaviour"))]}
    base.update(over)
    return base


def with_report(tmp: Path, data: dict | None):
    path = tmp / "validation_report.json"
    if data is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(data), encoding="utf-8")
    R.VALIDATION_REPORT = path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dbshift-cutover-selftest-"))
    # Never let a self-test write into the real output folder. provision.selftest
    # put eight fake deploys into deployments.jsonl before this rule existed.
    cutover_run.OUTPUT = tmp / "out"
    cutover_run.APPROVALS = cutover_run.OUTPUT / "approvals.jsonl"
    cutover_run.CERTIFICATE = cutover_run.OUTPUT / "certificate.json"
    ack = {"approved_by": "guru.ts@ganitinc.com", "reason": "full-outage window agreed",
           "collector_run_id": RUN}

    # --- the records must describe the database that is actually there --------
    check("records agree", R.records_match_target(records(), RUN)["status"], R.MET)
    wrong = R.records_match_target(records(OTHER), RUN)
    check("records from another run refuse", wrong["status"], R.UNMET)
    check("refusal names both runs", OTHER[:8] in wrong["detail"] and RUN[:8] in wrong["detail"], True)
    check("no run id at all refuses", R.records_match_target(records(), None)["status"], R.UNMET)

    # --- validation ------------------------------------------------------------
    with_report(tmp, None)
    check("no validation refuses", R.validation_passed(RUN)["status"], R.UNMET)
    with_report(tmp, report())
    check("clean validation passes", R.validation_passed(RUN)["status"], R.MET)
    with_report(tmp, report(run_id=OTHER))
    check("validation of another run refuses", R.validation_passed(RUN)["status"], R.UNMET)
    with_report(tmp, report(status="incomplete", not_comparable=3))
    check("incomplete validation refuses", R.validation_passed(RUN)["status"], R.UNMET)
    with_report(tmp, report(status="unreachable", reason="target unreachable"))
    check("unreachable validation refuses", R.validation_passed(RUN)["status"], R.UNMET)
    with_report(tmp, report(finished_at_utc=iso(30)))
    check("stale validation refuses", R.validation_passed(RUN)["status"], R.UNMET)
    # Phase 8 shipped exactly this: status "validated" over findings that were
    # never compared. The certificate must not take the word for the evidence.
    with_report(tmp, report(mismatches=2))
    check("validated-but-mismatched refuses", R.validation_passed(RUN)["status"], R.UNMET)
    with_report(tmp, report(not_comparable=11))
    check("validated-but-uncomparable refuses", R.validation_passed(RUN)["status"], R.UNMET)

    # --- the gate ---------------------------------------------------------------
    recs = records()
    check("blocked cutover refuses", R.gate_allows_cutover(recs["gate"], None)["status"], R.UNMET)
    waived = R.gate_allows_cutover(recs["gate"], ack)
    check("blocked cutover waivable by name", waived["status"], R.WAIVED)
    check("waiver names the approver", "guru.ts@ganitinc.com" in waived["detail"], True)
    clear = {"verdict": "PROCEED", "by_phase": {"cutover": {"status": "clear", "blocked_by": []}}}
    check("clear gate passes", R.gate_allows_cutover(clear, None)["status"], R.MET)

    # --- CDC lag: not applicable, and it must say why ---------------------------
    cdc = R.cdc_lag(recs["gate"])
    check("cdc lag not applicable", cdc["status"], R.NOT_APPLICABLE)
    check("cdc says why", "OPS-001" in cdc["detail"], True)
    check("cdc names the consequence", "outage window" in cdc["remedy"], True)
    check("cdc without blockers is unmet, not a free pass",
          R.cdc_lag({"by_phase": {}})["status"], R.UNMET)

    # --- the target -------------------------------------------------------------
    check("stopped target refuses", R.target_ready({"status": "stopped"})["status"], R.UNMET)
    check("unknown target refuses", R.target_ready({"status": None})["status"], R.UNMET)
    check("available target passes", R.target_ready({"status": "available"})["status"], R.MET)

    # --- Phase 4's steps ---------------------------------------------------------
    steps = R.outstanding_steps(recs["remediation"])["evidence"]["steps"]
    check("one cutover step found", len(steps), 1)
    check("it is the enable, not the disable", "ENABLE" in steps[0]["sql_on_target"], True)
    check("provision artefacts are not mistaken for steps",
          all(s["rule_id"] == "RDS-006" for s in steps), True)

    # --- declared gaps ------------------------------------------------------------
    gaps = R.declared_gaps(recs, RUN)["evidence"]["gaps"]
    check("post-cutover note is carried onto the certificate",
          any("LEGACY_SCORE" in g for g in gaps), True)
    check("the dead db link is declared", any("RDS-005" in g for g in gaps), True)
    check("applications are said to be the owner's step",
          any("repointed" in g for g in gaps), True)

    # --- readiness overall ----------------------------------------------------------
    plan = {"stack_name": "dbshift-target-dbmig-app"}
    with_report(tmp, report())
    live = {"run_id": RUN, "estate": "DBMIG_APP", "status": "available", "source": "tag"}
    check("not ready while the gate blocks and nobody signed",
          R.is_ready(R.build(recs, plan, live, None)), False)
    check("ready once a named person accepts the blockers",
          R.is_ready(R.build(recs, plan, live, ack)), True)
    check("not ready when the records describe another run",
          R.is_ready(R.build(records(OTHER), plan, live, ack)), False)
    check("not ready while the target is stopped",
          R.is_ready(R.build(recs, plan, {**live, "status": "stopped"}, ack)), False)

    # --- approvals bind to one estate --------------------------------------------
    cutover_run.OUTPUT.mkdir(parents=True, exist_ok=True)
    cutover_run.APPROVALS.write_text(json.dumps(ack) + "\n", encoding="utf-8")
    check("approval found for its own run", (cutover_run.approval_for(RUN) or {}).get("approved_by"),
          "guru.ts@ganitinc.com")
    check("approval does not carry to another estate", cutover_run.approval_for(OTHER), None)

    # --- only scheduler and refresh calls may run from a plan file -----------------
    allowed = "BEGIN DBMS_SCHEDULER.ENABLE('X'); END;"
    check("scheduler call allowed", allowed.upper().startswith(cutover_run.ALLOWED_SQL), True)
    for evil in ("DROP TABLE DBMIG_APP.LOAN", "GRANT DBA TO PUBLIC",
                 "BEGIN EXECUTE IMMEDIATE 'DROP USER X CASCADE'; END;"):
        check(f"refused: {evil[:24]}", evil.upper().startswith(cutover_run.ALLOWED_SQL), False)

    failed = sum(1 for s, _, _ in results if s == FAIL)
    for status, name, why in results:
        print(f"  [{status:<4}] {name}{('  -- ' + why) if why else ''}")
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
