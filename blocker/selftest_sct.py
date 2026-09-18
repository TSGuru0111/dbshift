"""Offline checks for the SCT blocker gate. No database, no AWS, no model.

What is being checked, in order of how much it matters:

  1. **A blocker must never be invented or lost.** An item halts a phase only
     when the routing table says it blocks that phase *and* the phase is in
     scope. Anything else is work -- and "work" must never read as "nothing".
  2. **CDC readiness comes from measured evidence, not from SCT and not from a
     default.** SCT never reads redo. An absent reading must report unknown, not
     clear: a gate that says "ready" because it failed to look is the worst
     possible failure here.
  3. **The migration mode decides scope.** On a full-load run a CDC-only blocker
     blocks nothing, and the gate says so rather than reporting a pass it never
     checked.
  4. Nothing here consults a model.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "blocker"

from collector import mode as migration_mode
from sct import route as sct_route

from . import policy, sct_gate

PASS = FAIL = 0


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


def _issue(code, occurrences=1, **kw):
    base = {"issue_code": code, "title": f"item {code}", "complexity": "simple",
            "occurrences": occurrences, "owner": "DBMIG_APP",
            "object_name": "OBJ", "object_type": "Tables", "objects": ["OBJ"],
            "recommendation": "do the thing"}
    base.update(kw)
    return base


def _assessment(codes):
    return {"issues": [_issue(c) for c in codes],
            "target": {"id": "rds-postgresql"},
            "collector_run_id": "test1234"}


FULL_LOAD = migration_mode.decide("full-load", chosen_by="test")
WITH_CDC = migration_mode.decide("full-load-and-cdc", chosen_by="test")
READY = {"log_mode": "ARCHIVELOG", "supplemental_logging": "YES"}
NOT_READY = {"log_mode": "NOARCHIVELOG", "supplemental_logging": "NO"}


def test_blockers():
    print("what halts and what does not")
    # 5200 blocks the full load; 5659 blocks only CDC; 5639 blocks nothing.
    d = sct_gate.evaluate(_assessment(["5200", "5659", "5639"]),
                          facts=READY, migration_mode_record=WITH_CDC)
    codes = {b["issue_code"] for b in d["blockers"]}
    check("5200 halts", "5200" in codes, True)
    check("5659 halts with CDC in scope", "5659" in codes, True)
    check("5639 does not halt", "5639" in codes, False)
    check("the verdict is HALT", d["verdict"], sct_gate.HALT)

    # Non-blocking items must still be reported -- as work, not as absent.
    work_codes = {w["issue_code"] for w in d["work"]}
    check("a non-blocking item is reported as work", "5639" in work_codes, True)
    truthy("the summary says work remains", "remain as work" in d["summary"])

    # On a full-load run the CDC-only blocker is out of scope, not clear.
    d2 = sct_gate.evaluate(_assessment(["5659"]), facts=NOT_READY,
                           migration_mode_record=FULL_LOAD)
    check("5659 does not halt a full-load run", d2["blockers"], [])
    check("...it is listed as out of scope", len(d2["out_of_scope_blockers"]), 1)
    truthy("...with the reason",
           d2["out_of_scope_blockers"][0]["not_in_scope_because"])
    check("a full-load run with only 5659 proceeds", d2["verdict"], sct_gate.PROCEED)
    check("migrate_cdc is not reported clear on a full load",
          d2["by_phase"]["migrate_cdc"]["status"], "not_in_scope")

    # An empty assessment proceeds, but must not read as an audited pass.
    d3 = sct_gate.evaluate(_assessment([]), facts=READY, migration_mode_record=FULL_LOAD)
    check("no items proceeds", d3["verdict"], sct_gate.PROCEED)
    check("...and counts zero action items", d3["action_items"], 0)

    # Every blocker names the phases it actually blocks, and how it clears.
    for b in d["blockers"]:
        truthy(f"{b['issue_code']} names the phases it blocks", b["blocks_in_scope"])
        truthy(f"{b['issue_code']} says what clears it", b["clears_when"])
        truthy(f"{b['issue_code']} explains itself", len(b["why"]) > 40)
        check(f"{b['issue_code']} blocks only real phases",
              all(p in policy.DOWNSTREAM for p in b["blocks"]), True)


def test_cdc_readiness():
    print("CDC readiness")
    # Declared CDC on an unready source halts, and names what is missing.
    c = sct_gate.cdc_requirements(NOT_READY, "full-load-and-cdc")
    check("an unready source blocks CDC", c["status"], "blocked")
    truthy("...and names both unmet requirements", len(c["readiness"]["unmet"]) == 2)
    truthy("...and says it needs a restart window", "restart" in c["clears_when"])
    truthy("...and says why SCT cannot answer it", "never reads redo" in
           c["why_not_from_sct"])

    c2 = sct_gate.cdc_requirements(READY, "full-load-and-cdc")
    check("a ready source clears", c2["status"], "clear")

    # **The one that matters.** No evidence must report unknown, never clear --
    # a gate that says "ready" because it failed to look is worse than no gate.
    c3 = sct_gate.cdc_requirements({}, "full-load-and-cdc")
    check("absent evidence does not clear", c3["status"], "blocked")
    check("...and does not claim readiness", c3["readiness"]["ready"], False)
    c4 = sct_gate.cdc_requirements(None, "full-load-and-cdc")
    check("None evidence does not clear", c4["status"], "blocked")

    # On a full load it is not a requirement, but the facts are still reported.
    c5 = sct_gate.cdc_requirements(NOT_READY, "full-load")
    check("a full load does not require CDC", c5["status"], "not_in_scope")
    check("...and does not apply", c5["applies"], False)
    truthy("...but still reports the configuration", c5["readiness"]["unmet"])

    # It reaches the verdict, not just the record.
    d = sct_gate.evaluate(_assessment(["5639"]), facts=NOT_READY,
                          migration_mode_record=WITH_CDC)
    check("unready CDC halts the run on its own", d["verdict"], sct_gate.HALT)
    truthy("...and blocks cutover",
           "CDC-READINESS" in d["by_phase"]["cutover"]["blocked_by"])
    truthy("...and is named in the summary", "CDC readiness" in d["summary"])

    d2 = sct_gate.evaluate(_assessment(["5639"]), facts=NOT_READY,
                           migration_mode_record=FULL_LOAD)
    check("the same source proceeds on a full load", d2["verdict"], sct_gate.PROCEED)
    truthy("...and the summary warns declaring CDC would block it",
           "declaring CDC later" in d2["summary"])


def test_grouping():
    print("grouping by where the work lands")
    d = sct_gate.evaluate(_assessment(list(sct_route.ROUTES)),
                          facts=READY, migration_mode_record=WITH_CDC)
    check("four groups, always", len(d["groups"]), 4)
    check("in the order the work happens",
          [g["where"] for g in d["groups"]],
          [sct_route.SOURCE, sct_route.TARGET, sct_route.DECISION, sct_route.HUMAN])
    check("every item lands in exactly one group",
          sum(g["item_count"] for g in d["groups"]), len(sct_route.ROUTES))
    truthy("each group explains what it means",
           all(len(g["meaning"]) > 40 for g in d["groups"]))
    truthy("a group counts its blockers",
           any(g["blocking_count"] for g in d["groups"]))
    truthy("a group counts what only a person can do",
           any(g["human_only"] for g in d["groups"]))

    # The source/target split is the point: they must not both be one list.
    src = [g for g in d["groups"] if g["where"] == sct_route.SOURCE][0]
    tgt = [g for g in d["groups"] if g["where"] == sct_route.TARGET][0]
    truthy("source work is separated", src["item_count"] > 0)
    truthy("target work is separated", tgt["item_count"] > 0)
    src_codes = {i["issue_code"] for i in src["items"]}
    tgt_codes = {i["issue_code"] for i in tgt["items"]}
    check("nothing appears in both", src_codes & tgt_codes, set())


def test_unrouted_and_waivers():
    print("unrouted items and waivers")
    # An SCT code nobody has mapped must be surfaced, and must not halt on a
    # guess -- it has no known blast radius, so it is work plus a warning.
    d = sct_gate.evaluate(_assessment(["99999"]), facts=READY,
                          migration_mode_record=FULL_LOAD)
    check("an unmapped code is reported", len(d["unrouted"]), 1)
    check("...and does not halt", d["blockers"], [])
    check("...and is routed to a person",
          d["unrouted"][0]["who"], sct_route.PERSON)

    # A valid waiver moves a blocker out of the way and is recorded.
    # The field is `approved_by`, per policy.REQUIRED_WAIVER_FIELDS. The first
    # version of this test used `accepted_by` and the gate correctly rejected
    # it -- the validation working, not a bug.
    waiver = {"rule_id": "SCT-5200", "reason": "external feed is being retired "
              "before the migration window", "approved_by": "dba@example.com"}
    d2 = sct_gate.evaluate(_assessment(["5200"]), facts=READY,
                           migration_mode_record=FULL_LOAD, waivers=[waiver])
    check("a waived blocker does not halt", d2["blockers"], [])
    check("...it is recorded as waived", len(d2["waived"]), 1)
    check("...and the verdict says so", d2["verdict"], sct_gate.PROCEED_WITH_WAIVERS)
    truthy("...against a named person",
           d2["waived"][0]["waiver"]["approved_by"] == "dba@example.com")

    # A waiver with a token reason is refused: "reason is too short to be a
    # reason" is the policy's own phrasing, and it exists so a waiver cannot be
    # a rubber stamp.
    thin = {"rule_id": "SCT-5200", "reason": "ok", "approved_by": "dba@example.com"}
    d4 = sct_gate.evaluate(_assessment(["5200"]), facts=READY,
                           migration_mode_record=FULL_LOAD, waivers=[thin])
    check("a token reason is refused", len(d4["rejected_waivers"]), 1)
    check("...and the blocker stands", d4["verdict"], sct_gate.HALT)

    # An invalid waiver is rejected, and the blocker stands.
    bad = {"rule_id": "SCT-5200"}
    d3 = sct_gate.evaluate(_assessment(["5200"]), facts=READY,
                           migration_mode_record=FULL_LOAD, waivers=[bad])
    check("an invalid waiver is rejected", len(d3["rejected_waivers"]), 1)
    check("...and the blocker still halts", d3["verdict"], sct_gate.HALT)


def test_determinism():
    print("determinism")
    src = (Path(__file__).resolve().parent / "sct_gate.py").read_text(encoding="utf-8")
    check("the gate imports no model client", "bedrock" in src.lower(), False)
    truthy("...and says so", "consults one" in src or "computed by a model" in src)

    # The same input twice must give the same verdict, with no timestamp inside
    # the decision that changes it.
    a = sct_gate.evaluate(_assessment(["5200", "5659"]), facts=NOT_READY,
                          migration_mode_record=WITH_CDC)
    b = sct_gate.evaluate(_assessment(["5200", "5659"]), facts=NOT_READY,
                          migration_mode_record=WITH_CDC)
    check("the verdict is reproducible", a["verdict"], b["verdict"])
    check("the summary is reproducible", a["summary"], b["summary"])
    check("the blockers are reproducible",
          [x["issue_code"] for x in a["blockers"]],
          [x["issue_code"] for x in b["blockers"]])

    # A record must say which assessment it judged, or it cannot be audited.
    truthy("the record names its source", a["source_of_findings"] == "aws-sct")
    truthy("the record names the migration mode", a["migration_mode"]["mode"])
    truthy("the record names the target", a["target"])


def test_estate_matching():
    print("estate matching in the CLI")
    from . import sct_run
    src = (Path(__file__).resolve().parent / "sct_run.py").read_text(encoding="utf-8")

    # `manifest["schemas"]` is a dict, not a list. Iterating it yielded key
    # names, so no estate matched and every run claimed no discovery existed.
    truthy("the schemas record is handled as a dict",
           'isinstance(schema_record, dict)' in src)
    truthy("...preferring what was actually collected", '"present"' in src)

    # The readiness is read from where collector/mode.py stores it, not from a
    # key that never existed.
    truthy("readiness is read from the stored cdc_readiness",
           '"cdc_readiness"' in src)
    check("the non-existent facts key is gone", 'manifest.get("facts")' in src, False)

    # Comparing two gates that read different estates is not a comparison.
    truthy("the comparison checks both read the same estate",
           "DIFFERENT ESTATES" in src)


def main() -> int:
    print("blocker/sct selftest -- no database, no AWS, no model\n")
    test_blockers()
    test_cdc_readiness()
    test_grouping()
    test_unrouted_and_waivers()
    test_determinism()
    test_estate_matching()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed" + (f", {FAIL} FAILED" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
