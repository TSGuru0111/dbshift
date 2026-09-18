"""Offline checks for the stored-code projection. No database, no AWS, no model.

`project.py` exists so Phase 3 can weigh the heterogeneous path without first
running a conversion against a PostgreSQL target. That makes it an input to a
commercial decision, and these checks protect the two ways it could corrupt one:

  1. **Pooling unrelated schemas.** A collector run may hold a production
     schema, its writable rehearsal copy, and an unrelated estate. Averaging
     them produces a figure that describes none of them -- on the run this was
     built against, 46% / 43% / 100% pool to 58%. A caller sizing an estate
     must be able to scope, and the spread must always be visible.

  2. **Collapsing two different costs.** `MODEL` (a construct needing
     judgement, drafted and then gated) and `MANUAL` (no PostgreSQL equivalent,
     written by a person from nothing) are both "handwork" and cost very
     differently. Reporting only the sum hides which one an estate has.

The classifier itself is covered by `convert/selftest.py`; this covers the
counting and scoping on top of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "convert"

from . import classify, project


def _obj(owner, otype, name, text):
    return {"owner": owner, "object_type": otype, "object_name": name,
            "source_sha256": "0" * 64, "line_count": text.count("\n") + 1,
            "char_length": len(text), "source_text": text}


# Source text chosen so the classifier routes it deterministically. Each uses a
# construct whose tier is fixed in constructs.json, so these are assertions
# about the catalogue as well as the counting.
_RULE_SRC = "BEGIN v := NVL(a, b); END;"
_MODEL_SRC = "BEGIN SELECT x BULK COLLECT INTO arr FROM t; END;"
_MANUAL_SRC = 'BEGIN v := "MixedCase"; END;'


def _inv(objects, errors=None):
    return {"objects": objects, "errors_by_object": errors or {},
            "types": set(), "triggers": {}, "columns": {}, "tables": {}}


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = _Check()

    print("the projection declares what it is")
    p = project.project(_inv([_obj("A", "FUNCTION", "F1", _RULE_SRC)]))
    c("basis is classified", p["basis"] == "classified")
    c("it does not claim to be measured", p["measured"] is False)
    c("nothing can be blocked without a compile", p["blocked"] == 0)

    print("\ncounting")
    inv = _inv([
        _obj("A", "FUNCTION", "F1", _RULE_SRC),
        _obj("A", "FUNCTION", "F2", _RULE_SRC),
        _obj("A", "PROCEDURE", "P1", _MODEL_SRC),
        _obj("A", "TYPE", "T1", _MANUAL_SRC),
    ])
    p = project.project(inv)
    c("rule objects are ready", p["ready"] == 2)
    c("model-tier objects are counted apart", p["model_tier"] == 1)
    c("manual objects are counted apart", p["manual"] == 1)
    c("handwork is the sum of the two", p["handwork"] == 2)
    c("convertible excludes neither half", p["convertible"] == 4)
    c("the percentage is over convertible only", p["pct_automatic"] == 50)

    # A broken object is excluded rather than converted, and must not dilute
    # the percentage -- it is an estate problem on either path.
    inv = _inv(
        [_obj("A", "FUNCTION", "F1", _RULE_SRC),
         _obj("A", "PROCEDURE", "BROKEN", _RULE_SRC)],
        errors={("A", "PROCEDURE", "BROKEN"): ["line 3: PLS-00103"]})
    p = project.project(inv)
    c("a broken object is excluded", p["excluded_broken_on_source"] == 1)
    c("an excluded object is not convertible", p["convertible"] == 1)
    c("an excluded object does not lower the percentage", p["pct_automatic"] == 100)

    # A package specification is absorbed into its body and is not work.
    p = project.project(_inv([
        _obj("A", "PACKAGE", "PKG", _RULE_SRC),
        _obj("A", "PACKAGE BODY", "PKG", _RULE_SRC),
    ]))
    c("a package spec is absorbed", p["absorbed"] == 1)
    c("an absorbed spec is not counted as work", p["handwork"] == 0)

    print("\nper-owner, never pooled silently")
    inv = _inv([
        _obj("PROD", "FUNCTION", "F1", _RULE_SRC),
        _obj("PROD", "FUNCTION", "F2", _RULE_SRC),
        _obj("REHEARSAL", "TYPE", "T1", _MANUAL_SRC),
        _obj("REHEARSAL", "TYPE", "T2", _MANUAL_SRC),
    ])
    p = project.project(inv)
    c("every owner is listed", p["owners"] == ["PROD", "REHEARSAL"])
    c("each owner is tallied separately", set(p["by_owner"]) == {"PROD", "REHEARSAL"})
    c("a clean schema reads 100%", p["by_owner"]["PROD"]["pct_automatic"] == 100)
    c("a hand-written schema reads 0%", p["by_owner"]["REHEARSAL"]["pct_automatic"] == 0)
    c("the pooled figure matches neither", p["pct_automatic"] == 50)
    c("the spread is always visible even when pooled",
      p["by_owner"]["PROD"]["pct_automatic"] != p["by_owner"]["REHEARSAL"]["pct_automatic"])

    print("\nscoping")
    s = project.project(inv, owners=["PROD"])
    c("scoping records what it was scoped to", s["scoped_to"] == ["PROD"])
    c("scoping excludes other owners", s["owners"] == ["PROD"])
    c("a scoped projection counts only its own objects", s["convertible"] == 2)
    c("a scoped percentage is the owner's own", s["pct_automatic"] == 100)
    c("an unscoped projection says so", p["scoped_to"] is None)
    c("scoping to nothing present yields nothing",
      project.project(inv, owners=["ABSENT"])["convertible"] == 0)
    c("an empty projection has no percentage to report",
      project.project(inv, owners=["ABSENT"])["pct_automatic"] is None)

    print("\nevery object carries its reason")
    p = project.project(_inv([_obj("A", "PROCEDURE", "P1", _MODEL_SRC)]))
    o = p["objects"][0]
    c("the route is recorded", o["route"] == classify.MODEL)
    c("the reason is recorded", bool(o["reason"]))
    c("the deciding constructs are named", len(o["deciding_constructs"]) >= 1)
    c("a deciding construct carries its tier",
      o["deciding_constructs"][0]["tier"] in ("model", "manual"))
    c("a deciding construct names its PostgreSQL form",
      bool(o["deciding_constructs"][0]["postgres"]))

    p = project.project(_inv([_obj("A", "FUNCTION", "F1", _RULE_SRC)]))
    c("a rule object names no deciding construct",
      p["objects"][0]["deciding_constructs"] == [])

    print("\nthe shape sizing reads")
    p = project.project(inv, owners=["PROD"])
    for field in ("convertible", "ready", "handwork", "model_tier", "manual",
                  "blocked", "excluded_broken_on_source", "pct_automatic", "basis"):
        c(f"the summary carries {field}", field in p)

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
