"""Offline checks for the Phase 1 migration mode. No database, no AWS.

The mode decides whether three CRITICAL findings are blockers or irrelevancies,
so the ways it can be wrong are expensive in both directions: a full-load run
that still halts on ARCHIVELOG sends a client to schedule a database restart
they do not need, and a CDC run that quietly stops halting on it produces a
replication that silently loses updates.

Covers the parsing, the decision record, the assessment pass, and the gate --
end to end, because the value's whole purpose is to survive the journey between
those phases.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "collector"

from . import mode as M

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}\n       got  {got!r}\n       want {want!r}")


def _findings():
    """The four CRITICALs a real DBMIG_APP run produces."""
    def f(rule_id, name, category, level):
        return {"rule_id": rule_id, "severity": "CRITICAL", "title": rule_id,
                "owner": "DBMIG_APP", "object_name": name, "category": category,
                "remediation_level": level, "rationale": "", "detail": "",
                "finding_id": rule_id + name}
    return [
        f("DQ-001", "T1", "data_quality", "L2"),
        f("DQ-001", "T2", "data_quality", "L2"),
        f("OPS-001", "XE", "operational_risk", "L3"),
        f("OPS-002", "XE", "operational_risk", "L3"),
        f("RDS-004", "EXT_FEED", "rds_compatibility", "L3"),
    ]


def main() -> int:
    print("parsing")
    check("unset means full load", M.normalize(None), M.FULL_LOAD)
    check("blank means full load", M.normalize("   "), M.FULL_LOAD)
    check("'cdc' shorthand", M.normalize("cdc"), M.FULL_LOAD_AND_CDC)
    check("underscores and case", M.normalize("FULL_LOAD"), M.FULL_LOAD)
    check("the DMS spelling round-trips",
          M.normalize(M.FULL_LOAD_AND_CDC), M.FULL_LOAD_AND_CDC)
    try:
        M.normalize("maybe")
        check("an unknown mode is refused", "accepted", "refused")
    except M.ModeError:
        check("an unknown mode is refused", "refused", "refused")

    # Defaulting to CDC would assert a source configuration nothing verified.
    check("the default is the weaker claim", M.DEFAULT, M.FULL_LOAD)

    print("\nthe decision record")
    d = M.decide(None, declared=False)
    check("an undeclared run says so", d["declared"], False)
    check("and still resolves a mode", d["mode"], M.FULL_LOAD)

    d = M.decide("cdc", chosen_by="guru.ts@ganitinc.com",
                 log_mode="NOARCHIVELOG", supplemental_min="NO")
    check("CDC declared is in scope", d["cdc_in_scope"], True)
    check("the chooser is recorded", d["chosen_by"], "guru.ts@ganitinc.com")
    check("an unready source warns", bool(d.get("warning")), True)
    check("and the warning names the restart", "restart" in d["warning"], True)
    check("readiness is evidence, not a verdict", d["cdc_readiness"]["ready"], False)

    # Declaring CDC against an unready source is allowed: it is a remediation
    # task with a window attached, not a reason to refuse the declaration.
    check("declaring CDC on an unready source is not refused", d["mode"],
          M.FULL_LOAD_AND_CDC)

    d = M.decide("full-load", log_mode="ARCHIVELOG", supplemental_min="YES")
    check("a ready source is noted even when unused", bool(d.get("note")), True)
    check("no warning when CDC is out of scope", d.get("warning"), None)

    print("\napplicability")
    for rule_id in ("OPS-001", "OPS-002", "DQ-001"):
        applies, why = M.applies(rule_id, M.FULL_LOAD)
        check(f"{rule_id} does not apply to a full load", applies, False)
        check(f"{rule_id} says why", bool(why and len(why) > 30), True)
        check(f"{rule_id} applies to CDC", M.applies(rule_id, M.FULL_LOAD_AND_CDC)[0], True)
    check("RDS-004 applies to both", M.applies("RDS-004", M.FULL_LOAD)[0], True)

    print("\nthe assessment pass")
    from assess import engine

    fs = engine.apply_migration_mode(json.loads(json.dumps(_findings())), M.FULL_LOAD)
    crit = {f["rule_id"] for f in fs if f["severity"] == "CRITICAL"}
    check("only the real blocker stays CRITICAL", crit, {"RDS-004"})
    check("nothing was removed", len(fs), 5)
    na = [f for f in fs if not f["applies"]]
    # Three rules, four findings: DQ-001 fires once per table without a key.
    # The pass marks findings, not rules, so a rule firing twice must produce
    # two marked findings and not one.
    check("three rules marked not applicable",
          sorted({f["rule_id"] for f in na}), ["DQ-001", "OPS-001", "OPS-002"])
    check("every occurrence is marked, not just the first", len(na), 4)
    check("each keeps the severity it would have had",
          {f["severity_if_applicable"] for f in na}, {"CRITICAL"})
    check("each carries its reason", all(f.get("not_applicable_because") for f in na), True)

    fs_cdc = engine.apply_migration_mode(json.loads(json.dumps(_findings())),
                                         M.FULL_LOAD_AND_CDC)
    check("CDC leaves every severity alone",
          {f["severity"] for f in fs_cdc}, {"CRITICAL"})
    check("and marks nothing not-applicable",
          [f for f in fs_cdc if not f["applies"]], [])

    print("\nthe gate")
    from blocker import gate

    full = gate.evaluate({"findings": fs, "collector_run_id": "x",
                          "migration_mode": M.decide("full-load")})
    check("full load blocks on the real one only",
          [b["rule_id"] for b in full["blockers"]], ["RDS-004"])
    check("migrate_cdc is not in scope",
          full["by_phase"]["migrate_cdc"]["status"], "not_in_scope")
    check("cutover is no longer blocked",
          full["by_phase"]["cutover"]["status"], "clear")
    check("the summary names the migration judged",
          "full-load" in full["summary"], True)
    check("phases stay in execution order",
          list(full["by_phase"]), ["provision", "migrate_full_load", "migrate_cdc",
                                   "validate", "cutover"])

    cdc = gate.evaluate({"findings": fs_cdc, "collector_run_id": "x",
                         "migration_mode": M.decide("cdc")})
    check("CDC blocks on all four",
          sorted(b["rule_id"] for b in cdc["blockers"]),
          ["DQ-001", "OPS-001", "OPS-002", "RDS-004"])
    check("and migrate_cdc is blocked, not absent",
          cdc["by_phase"]["migrate_cdc"]["status"], "blocked")

    # A source in NOARCHIVELOG is the exact case this whole change exists for.
    only_cdc = engine.apply_migration_mode(
        [f for f in json.loads(json.dumps(_findings())) if f["rule_id"] != "RDS-004"],
        M.FULL_LOAD)
    clean = gate.evaluate({"findings": only_cdc, "collector_run_id": "x",
                           "migration_mode": M.decide("full-load")})
    check("NOARCHIVELOG alone no longer halts a full load",
          clean["verdict"], gate.PROCEED)
    check("the same estate still halts a CDC migration",
          gate.evaluate({
              "findings": engine.apply_migration_mode(
                  [f for f in json.loads(json.dumps(_findings())) if f["rule_id"] != "RDS-004"],
                  M.FULL_LOAD_AND_CDC),
              "collector_run_id": "x",
              "migration_mode": M.decide("cdc")})["verdict"],
          gate.HALT)

    print("\nthe answer key")
    # The key grades the rule catalogue -- "DQ-001 should call a missing primary
    # key CRITICAL" -- not whether today's migration cares. Grading the
    # downgraded severity made declaring a full load look like a severity
    # regression in the engine, which is the opposite of what happened: the
    # engine found the defect, and the mode said it does not block this run.
    from assess import scoring

    seeded = [f for f in fs if f["rule_id"] == "DQ-001"]
    check("a downgraded finding still grades as the rule wrote it",
          sorted({f.get("severity_if_applicable") or f["severity"] for f in seeded}),
          ["CRITICAL"])
    check("the downgrade is what the score and gate see",
          sorted({f["severity"] for f in seeded}), ["INFO"])
    check("scoring grades the rule severity, not the downgrade",
          "severity_if_applicable" in Path("assess/scoring.py").read_text(encoding="utf-8"),
          True)
    check("the key still returns a result on downgraded findings",
          isinstance(scoring.score_against_answer_key(fs, owners={"DBMIG_APP"}), dict),
          True)

    print("\nolder runs")
    # A run collected before the mode existed carries none. Falling back to CDC
    # would reintroduce the phantom blockers; falling back to full load would
    # hide real ones. The default is full load, so the fallback must be the
    # explicitly undeclared record -- visible as an assumption in the report.
    legacy = gate.evaluate({"findings": _findings(), "collector_run_id": "x"})
    check("a run with no mode still gates", legacy["verdict"], gate.HALT)
    check("and is recorded as undeclared",
          legacy["migration_mode"]["declared"], False)

    print("\nthe Phase 7 handover")
    from dms import run as dms_run
    from dms import policy as dms_policy

    check("Phase 7 follows a declared CDC mode",
          dms_run.declared_migration_type({"gate": {"migration_mode": M.decide("cdc")}}),
          dms_policy.FULL_LOAD_AND_CDC)
    check("and a declared full load",
          dms_run.declared_migration_type({"gate": {"migration_mode": M.decide("full-load")}}),
          dms_policy.FULL_LOAD)
    check("a gate with no mode falls back to full load",
          dms_run.declared_migration_type({"gate": {}}), dms_policy.FULL_LOAD)
    check("an unrecognised mode falls back to full load, never to CDC",
          dms_run.declared_migration_type({"gate": {"migration_mode": {"mode": "wat"}}}),
          dms_policy.FULL_LOAD)
    # collector.mode and dms.policy must keep spelling these the same, or the
    # value silently stops passing through.
    check("the two vocabularies agree",
          (M.FULL_LOAD, M.FULL_LOAD_AND_CDC),
          (dms_policy.FULL_LOAD, dms_policy.FULL_LOAD_AND_CDC))

    print(f"\n{PASS}/{PASS + FAIL} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
