"""Offline checks for the target decision. No database, no AWS, no collector run.

Covers each way the two-path decision could mislead: a blocker treated as a
warning, an unmeasured estate recommended anyway, effort counted twice, the
PostgreSQL decision carrying Oracle licence arithmetic, or a blocked path
accepted because someone chose it.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sizing"

from . import policy, target as t, validate as v


def _facts(features=(), structural=None, segment_bytes=8 * 1024**3, character_set="AL32UTF8"):
    return {
        "segment_bytes": segment_bytes,
        "segment_gb": round(segment_bytes / 1024**3, 3),
        "object_count": 90,
        "features_detected": [{"name": n, "currently_used": "TRUE"} for n in features],
        "structural": structural or {"partitioned_tables": 0, "bitmap_indexes": 0},
        "character_set": character_set,
        "utilization": {"available": False, "reason": "no metric signal", "measured": None},
    }


def _conversion(**totals):
    return {"totals": totals}


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = _Check()

    print("blockers")
    a = t.assess(_facts(["Real Application Clusters (RAC)"]))
    c("RAC blocks PostgreSQL", not a["paths"][t.POSTGRESQL]["possible"])
    c("RAC does not block Oracle", a["paths"][t.ORACLE]["possible"])
    c("a blocked path recommends Oracle with high confidence",
      a["recommended"]["target"] == t.ORACLE and a["recommended"]["confidence"] == "high")
    try:
        t.choose(t.POSTGRESQL, a)
        c("choosing a blocked path is refused", False)
    except ValueError as exc:
        c("choosing a blocked path is refused", "blocked by" in str(exc))
    c("choosing the open path is allowed", t.choose(t.ORACLE, a)["target"] == t.ORACLE)

    for name in ("Oracle Label Security", "Oracle Database Vault", "Oracle Advanced Queuing"):
        c(f"{name} blocks PostgreSQL",
          not t.assess(_facts([name]))["paths"][t.POSTGRESQL]["possible"])

    print("effort, not blockers")
    a = t.assess(_facts(["Partitioning (user)", "Advanced Compression"]))
    c("partitioning is effort, not a blocker", a["paths"][t.POSTGRESQL]["possible"])
    c("both features are counted", a["paths"][t.POSTGRESQL]["effort_points"] >= 4)
    c("an unused feature contributes nothing",
      t.assess(_facts())["paths"][t.POSTGRESQL]["effort_points"] == 0)
    c("a feature detected but not currently used is ignored",
      t.assess({**_facts(), "features_detected": [
          {"name": "Partitioning (user)", "currently_used": "FALSE"}]}
      )["paths"][t.POSTGRESQL]["effort_points"] == 0)

    print("structural facts")
    a = t.assess(_facts(structural={"partitioned_tables": 3, "bitmap_indexes": 2}))
    subjects = " ".join(i["subject"] for i in a["paths"][t.POSTGRESQL]["effort"])
    c("partitioned tables are counted", "3 partitioned table(s)" in subjects)
    c("bitmap indexes are counted", "2 bitmap index(es)" in subjects)

    print("stored code")
    a = t.assess(_facts())
    c("no conversion plan means unmeasured", not a["stored_code"]["measured"])
    c("unmeasured refuses to recommend", a["recommended"]["target"] is None
      and a["recommended"]["confidence"] == "insufficient evidence")

    a = t.assess(_facts(), _conversion(READY_FOR_APPROVAL=6, BLOCKED=6))
    c("uncompiled objects refuse a recommendation", a["recommended"]["target"] is None,
      a["recommended"]["confidence"])

    a = t.assess(_facts(), _conversion(READY_FOR_APPROVAL=6, MANUAL=1,
                                       EXCLUDED_BROKEN_ON_SOURCE=1))
    c("handwork is a judgement, not a verdict", a["recommended"]["target"] is None
      and "judgement" in a["recommended"]["confidence"])
    c("percentage automatic is computed over convertible only",
      a["stored_code"]["pct_automatic"] == 86, str(a["stored_code"]["pct_automatic"]))
    c("broken-on-source objects cost nothing",
      all(i["weight"] == 0 for i in a["paths"][t.POSTGRESQL]["effort"]
          if "broken on the source" in i["subject"]))

    a = t.assess(_facts(), _conversion(READY_FOR_APPROVAL=8))
    c("fully converted code recommends PostgreSQL",
      a["recommended"]["target"] == t.POSTGRESQL and a["recommended"]["confidence"] == "high")
    c("a clean conversion adds no effort points",
      a["paths"][t.POSTGRESQL]["effort_points"] == 0)

    print("evidence is traceable")
    a = t.assess(_facts(["Partitioning (user)"]), _conversion(READY_FOR_APPROVAL=8))
    c("every item names its evidence",
      all(i.get("evidence") for i in a["paths"][t.POSTGRESQL]["effort"]))
    c("every item explains itself",
      all(len(i.get("detail", "")) > 40 for i in a["paths"][t.POSTGRESQL]["effort"]))

    print("sizing by engine")
    # Partitioning forces EE only when partitioned tables actually exist -- the
    # feature row alone is not evidence, which is the point of policy.edition_verdict.
    facts = _facts(["Partitioning (user)"], structural={"partitioned_tables": 3, "bitmap_indexes": 0},
                   segment_bytes=8 * 1024**3)
    proposal = {"edition": "EE", "instance_class": "db.t3.medium", "storage_gb": 20,
                "apparent_forcing_features": [], "source": "test", "model_id": None,
                "rationale": "test"}

    ora = v.validate(proposal, facts, engine=t.ORACLE)
    pg = v.validate(proposal, facts, engine=t.POSTGRESQL)

    c("Oracle decides an edition", ora["edition"] == "EE")
    c("Oracle counts processor licences", ora["processor_licences"] > 0)
    c("PostgreSQL has no edition", pg["edition"] is None)
    c("PostgreSQL has no licence model", pg["licence_model"] is None)
    c("PostgreSQL counts no licences", pg["processor_licences"] == 0)
    c("PostgreSQL names its engine", pg["engine"] == t.POSTGRESQL)
    c("Oracle names its engine", ora["engine"] == t.ORACLE)

    c("PostgreSQL storage exceeds Oracle for the same estate",
      pg["storage_gb"] > ora["storage_gb"],
      f"pg {pg['storage_gb']} vs oracle {ora['storage_gb']}")
    c("the storage difference is explained",
      any("fill factor" in ck["detail"] for ck in pg["checks"] if ck["check"] == "storage"))
    c("both engines land on the floor for a tiny estate",
      v.validate(proposal, _facts(segment_bytes=1024**3), engine=t.POSTGRESQL)["storage_gb"]
      == policy.PG_MIN_STORAGE_GB)

    c("PostgreSQL targets UTF8", pg["character_set"] == "UTF8")
    c("a clean character set passes",
      any(ck["verdict"] == "PASS" for ck in pg["checks"] if ck["check"] == "encoding"))
    odd = v.validate(proposal, _facts(character_set="WE8ISO8859P1"), engine=t.POSTGRESQL)
    c("a non-UTF8 source warns",
      any(ck["verdict"] == "WARN" for ck in odd["checks"] if ck["check"] == "encoding"))

    print("choice recording")
    a = t.assess(_facts(), _conversion(READY_FOR_APPROVAL=8))
    chose = t.choose(t.POSTGRESQL, a, chosen_by="someone@example.com")
    c("a choice records who made it", chose["chosen_by"] == "someone@example.com")
    c("agreeing with the recommendation is recorded", chose["agreed_with_recommendation"])
    c("disagreeing is recorded too",
      not t.choose(t.ORACLE, a)["agreed_with_recommendation"])
    try:
        t.choose("AURORA", a)
        c("an unknown target is refused", False)
    except ValueError:
        c("an unknown target is refused", True)

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
