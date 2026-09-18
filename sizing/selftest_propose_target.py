"""Offline checks for the engine recommendation. No database, no AWS, no collector run.

The model is exercised with a stub client, so every path through
`propose_target.py` runs here -- including the Bedrock path -- without spending
a token or needing credentials.

What these checks are actually protecting, in order of how much damage each
failure would do to a client conversation:

  1. **A blocker is never put to a model.** RAC, Label Security, Database Vault
     and Advanced Queuing are facts about the estate. A model given room to
     weigh one would eventually argue past it, so the code decides before the
     client is ever constructed -- and that is asserted by handing the stub a
     recommendation for the blocked path and proving no call was made.

  2. **A projection can never read as a measurement.** `classified` evidence
     may not carry `high` confidence, whoever proposes it. This is the property
     that lets Phase 3 run before Phase 4b without overclaiming.

  3. **A fabricated number is caught.** The failure mode of a model weighing a
     commercial trade-off is not malformed JSON -- it is a plausible sentence
     resting on a figure nobody measured.

  4. **Every model error falls back rather than failing the phase**, and the
     fallback is visible in `source`. A phase that dies because a model
     returned prose is worse than one that quietly uses the rules and says so.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sizing"

from . import propose_target as pt
from . import target as t
from . import validate_target as vt


# --------------------------------------------------------------- fixtures


def _paths(pg_possible=True, pg_blockers=(), pg_points=8, ora_points=0,
           pg_effort=(), ora_effort=()):
    """Two paths in the shape `target.assess()` publishes them."""
    return {
        t.ORACLE: {
            "target": t.ORACLE, "label": t.LABEL[t.ORACLE],
            "possible": True, "blockers": [],
            "effort": [{"subject": s, "detail": d} for s, d in ora_effort],
            "effort_points": ora_points,
        },
        t.POSTGRESQL: {
            "target": t.POSTGRESQL, "label": t.LABEL[t.POSTGRESQL],
            "possible": pg_possible,
            "blockers": [{"subject": b, "detail": "no equivalent"} for b in pg_blockers],
            "effort": [{"subject": s, "detail": d} for s, d in pg_effort],
            "effort_points": pg_points,
        },
    }


def _classified(ready=6, model_tier=6, manual=1, excluded=1):
    """A `convert.project` projection, as DBMIG_APP actually produces one."""
    convertible = ready + model_tier + manual
    return {
        "basis": "classified", "measured": False,
        "convertible": convertible, "ready": ready,
        "handwork": model_tier + manual,
        "model_tier": model_tier, "manual": manual,
        "blocked": 0, "excluded_broken_on_source": excluded,
        "pct_automatic": round(100 * ready / convertible) if convertible else None,
    }


def _proven(ready=6, handwork=0, blocked=0):
    convertible = ready + handwork + blocked
    return {
        "measured": True, "basis": "measured" if blocked else "proven",
        "convertible": convertible, "ready": ready, "handwork": handwork,
        "model_tier": 0, "manual": handwork, "blocked": blocked,
        "excluded_broken_on_source": 0,
        "pct_automatic": round(100 * ready / convertible) if convertible else None,
    }


class _Stub:
    """A Bedrock client that returns canned text and counts its calls."""

    def __init__(self, payload, raise_error=None):
        self.payload = payload
        self.raise_error = raise_error
        self.calls = 0
        self.prompt = None

    def complete(self, tier, prompt, max_tokens=None):
        self.calls += 1
        self.prompt = prompt
        if self.raise_error:
            from bedrock.client import BedrockError
            raise BedrockError(self.raise_error)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return {"text": text, "model_id": "stub-model",
                "input_tokens": 100, "output_tokens": 20}


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = _Check()

    # ------------------------------------------------------------- tiers
    print("evidence tiers")
    c("a 4b plan with nothing blocked is proven",
      pt._basis({"measured": True, "blocked": 0}) == "proven")
    c("a 4b plan with blocked objects was never compiled",
      pt._basis({"measured": True, "blocked": 3}) == "measured")
    c("a projection declares its own basis",
      pt._basis(_classified()) == "classified")
    c("no evidence is not a tier", pt._basis({}) is None)
    c("classified evidence can never claim high confidence",
      pt.HIGH not in pt.ALLOWED_BY_BASIS["classified"])
    c("uncompiled evidence can never claim high confidence",
      pt.HIGH not in pt.ALLOWED_BY_BASIS["measured"])
    c("proven evidence may claim high confidence",
      pt.HIGH in pt.ALLOWED_BY_BASIS["proven"])
    c("absent evidence may claim only insufficient",
      pt.ALLOWED_BY_BASIS[None] == (pt.INSUFFICIENT,))

    # --------------------------------------------------------- heuristic
    print("\nheuristic recommendation")
    p = pt.heuristic_target_proposal(_paths(pg_possible=False, pg_blockers=["Oracle Label Security"]),
                                     _proven())
    c("a blocked path recommends Oracle", p["target"] == t.ORACLE)
    c("a blocked path is certain", p["confidence"] == pt.HIGH)
    c("the blocker is named in the reason", "Label Security" in p["reason"])

    p = pt.heuristic_target_proposal(_paths(), {})
    c("no stored-code evidence refuses to recommend", p["target"] is None)
    c("no evidence is insufficient evidence", p["confidence"] == pt.INSUFFICIENT)

    p = pt.heuristic_target_proposal(_paths(), _proven(blocked=3))
    c("uncompiled objects refuse a recommendation", p["target"] is None)

    p = pt.heuristic_target_proposal(_paths(), _proven())
    c("fully proven code recommends PostgreSQL", p["target"] == t.POSTGRESQL)
    c("fully proven code is certain", p["confidence"] == pt.HIGH)

    # The change that decouples Phase 3 from Phase 4b: a projection is enough
    # to answer, where it previously returned "insufficient evidence".
    p = pt.heuristic_target_proposal(_paths(), _classified(ready=6, model_tier=0, manual=0))
    c("a clean projection recommends PostgreSQL", p["target"] == t.POSTGRESQL)
    c("a clean projection is labelled a projection", p["confidence"] == pt.PROJECTION)
    c("a projection says nothing was compiled",
      "compiled" in p["reason"] and "projection" in p["reason"])

    p = pt.heuristic_target_proposal(_paths(), _classified())
    c("a projection with handwork makes no recommendation", p["target"] is None)
    c("projected handwork is still a projection", p["confidence"] == pt.PROJECTION)
    c("the drafted and hand-written halves are described separately",
      "drafted" in p["reason"] and "from nothing" in p["reason"])
    c("the projected percentage is the one measured", "46%" in p["reason"])

    p = pt.heuristic_target_proposal(_paths(), _proven(ready=6, handwork=2))
    c("measured handwork is a judgement, not a projection",
      p["confidence"] == pt.JUDGEMENT)

    # ----------------------------------------------------------- bedrock
    print("\nbedrock proposal")
    good = {"target": None, "confidence": pt.PROJECTION,
            "reason": "46% of 13 convertible objects project as automatic; 6 need a "
                      "drafted rewrite and 1 has no equivalent."}

    s = _Stub(good)
    p = pt.bedrock_target_proposal(_paths(), _classified(), client=s)
    c("the model is called once", s.calls == 1)
    c("a model proposal is labelled bedrock", p["source"] == "bedrock")
    c("the model id is recorded", p["model_id"] == "stub-model")
    c("token counts are recorded", p["tokens"]["in"] == 100)
    c("the basis travels with the proposal", p["evidence_basis"] == "classified")

    # Property 1: a blocker is never put to a model.
    s = _Stub(good)
    p = pt.bedrock_target_proposal(
        _paths(pg_possible=False, pg_blockers=["Real Application Clusters (RAC)"]),
        _classified(), client=s)
    c("a blocked path never reaches the model", s.calls == 0)
    c("a blocked path is still answered", p["target"] == t.ORACLE)

    s = _Stub(good)
    pt.bedrock_target_proposal(_paths(), {}, client=s)
    c("absent evidence never reaches the model", s.calls == 0)

    # The prompt must state how the numbers were obtained, and must offer only
    # the confidence phrases the tier allows.
    s = _Stub(good)
    pt.bedrock_target_proposal(_paths(), _classified(), client=s)
    c("the prompt says nothing was converted or compiled",
      "NOTHING WAS CONVERTED OR COMPILED" in s.prompt)
    c("the prompt withholds high confidence on a projection",
      f'"{pt.HIGH}"' not in s.prompt.split("Rules you must follow")[0])
    c("the prompt names the construct count from the catalogue",
      "57 Oracle constructs" in s.prompt or "catalogue of" in s.prompt)

    s = _Stub(good)
    pt.bedrock_target_proposal(_paths(), _proven(), client=s)
    c("the prompt offers high confidence on proven evidence",
      f'"{pt.HIGH}"' in s.prompt)
    c("the prompt says the code was compiled for real",
      "rolled back" in s.prompt)

    # Property 4: every model failure falls back, visibly.
    s = _Stub(good, raise_error="throttled")
    p = pt.bedrock_target_proposal(_paths(), _classified(), client=s)
    c("a model error falls back to the heuristic",
      p["source"] == "heuristic_after_model_error")
    c("the model error is recorded", "throttled" in p["model_error"])

    p = pt.bedrock_target_proposal(_paths(), _classified(), client=_Stub("not json at all"))
    c("an unparseable reply falls back",
      p["source"] == "heuristic_after_unparseable_reply")

    p = pt.bedrock_target_proposal(_paths(), _classified(),
                                   client=_Stub({**good, "target": "AURORA"}))
    c("an unknown target falls back", p["source"] == "heuristic_after_unknown_target")
    c("the rejected target is named", "AURORA" in p["model_error"])

    p = pt.bedrock_target_proposal(_paths(), _classified(),
                                   client=_Stub({**good, "confidence": "certain"}))
    c("a confidence outside the vocabulary falls back",
      p["source"] == "heuristic_after_unallowed_confidence")

    p = pt.bedrock_target_proposal(_paths(), _classified(),
                                   client=_Stub({**good, "confidence": pt.HIGH}))
    c("high confidence on a projection falls back",
      p["source"] == "heuristic_after_unallowed_confidence")
    c("the refusal explains why", "classified" in p["model_error"])

    # A fenced reply is the commonest well-formed-but-wrapped case.
    p = pt.bedrock_target_proposal(
        _paths(), _classified(),
        client=_Stub("```json\n" + json.dumps(good) + "\n```"))
    c("a fenced JSON reply is still read", p["source"] == "bedrock")

    # ---------------------------------------------------------- validator
    print("\nvalidation bounds the model")
    a = {"paths": _paths(), "stored_code": _classified()}

    v = vt.validate_target(pt.heuristic_target_proposal(_paths(), _classified()), a)
    c("the heuristic needs no override", v["override_count"] == 0)
    c("agreement is recorded", v["agreed_with_proposal"])

    v = vt.validate_target(
        {"source": "bedrock", "model_id": "m", "target": t.POSTGRESQL,
         "confidence": pt.HIGH, "evidence_basis": "classified",
         "reason": "46% converts and the rest is trivial."}, a)
    c("high confidence on a projection is overridden",
      any(k["check"] == "confidence_allowed" and k["verdict"] == "OVERRIDE"
          for k in v["checks"]))
    c("the corrected confidence is a projection", v["confidence"] == pt.PROJECTION)
    c("recommending PostgreSQL over handwork is overridden",
      any(k["check"] == "handwork_acknowledged" and k["verdict"] == "OVERRIDE"
          for k in v["checks"]))
    c("the decided target is withdrawn", v["target"] is None)
    c("disagreement is recorded", not v["agreed_with_proposal"])
    c("both overrides are counted", v["override_count"] == 2)

    # Property 3: an invented number is caught even when everything else is right.
    v = vt.validate_target(
        {"source": "bedrock", "model_id": "m", "target": None,
         "confidence": pt.PROJECTION, "evidence_basis": "classified",
         "reason": "Roughly 85% of the stored code converts, so the path is cheap."}, a)
    c("a fabricated percentage warns",
      any(k["check"] == "reason_cites_evidence" and k["verdict"] == "WARN"
          for k in v["checks"]))
    c("the invented number is named",
      any("85" in (k["detail"] or "") for k in v["checks"]))
    c("a fabricated number does not override the verdict", v["override_count"] == 0)

    v = vt.validate_target(
        {"source": "bedrock", "model_id": "m", "target": None,
         "confidence": pt.PROJECTION, "evidence_basis": "classified",
         "reason": "6 of 13 objects convert, 46% automatic; 6 drafted, 1 by hand."}, a)
    c("every cited number from the evidence passes",
      all(k["verdict"] == "PASS" for k in v["checks"]))

    # A blocked recommendation reaching the validator directly -- the path a
    # hand-written or future proposal could take past propose_target's check.
    blocked = {"paths": _paths(pg_possible=False, pg_blockers=["Oracle Advanced Queuing"]),
               "stored_code": _proven()}
    v = vt.validate_target(
        {"source": "bedrock", "model_id": "m", "target": t.POSTGRESQL,
         "confidence": pt.HIGH, "evidence_basis": "proven",
         "reason": "Migrate to PostgreSQL."}, blocked)
    c("a blocked recommendation is overridden at validation",
      any(k["check"] == "target_open" and k["verdict"] == "OVERRIDE" for k in v["checks"]))
    c("the override lands on Oracle", v["target"] == t.ORACLE)
    c("the blocker is named", "Advanced Queuing" in str(v["checks"]))

    v = vt.validate_target(pt.heuristic_target_proposal(_paths(), _proven()),
                           {"paths": _paths(), "stored_code": _proven()})
    c("a proven clean estate passes every check",
      v["override_count"] == 0 and v["warning_count"] == 0)
    c("a proven clean estate recommends PostgreSQL with confidence",
      v["target"] == t.POSTGRESQL and v["confidence"] == pt.HIGH)

    # ------------------------------------------------- wired into assess()
    print("\nwired into the assessment")
    facts = {
        "segment_bytes": 8 * 1024**3, "segment_gb": 8.0, "object_count": 90,
        "features_detected": [], "structural": {"partitioned_tables": 0, "bitmap_indexes": 0},
        "character_set": "AL32UTF8",
        "utilization": {"available": False, "reason": "none", "measured": None},
    }
    a1 = t.assess(facts, _classified())
    c("assess accepts a projection", a1["stored_code"]["basis"] == "classified")
    c("a projection produces effort items",
      any("project as automatic" in i["subject"] for i in a1["paths"][t.POSTGRESQL]["effort"]))
    c("drafted rewrites are weighted below hand-written ones",
      next(i["weight"] for i in a1["paths"][t.POSTGRESQL]["effort"]
           if "drafted rewrite" in i["subject"]) // 6
      < next(i["weight"] for i in a1["paths"][t.POSTGRESQL]["effort"]
             if "no PostgreSQL equivalent" in i["subject"]))
    c("the projection reaches the recommendation",
      a1["recommended"]["evidence_basis"] == "classified")
    c("assess still defaults to no model", a1["recommended"]["source"] == "heuristic")

    a2 = t.assess(facts, {"totals": {"READY_FOR_APPROVAL": 8}})
    c("a 4b plan is still read as proven", a2["stored_code"]["basis"] == "proven")
    c("a proven plan recommends PostgreSQL", a2["recommended"]["target"] == t.POSTGRESQL)

    a3 = t.assess(facts, _classified(), use_bedrock=True, client=_Stub(good))
    c("assess can route the recommendation through the model",
      a3["recommended"]["source"] == "bedrock")
    c("the model's checks are published", len(a3["recommended"]["checks"]) == 4)
    c("the model's overrides are counted", "override_count" in a3["recommended"])

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
