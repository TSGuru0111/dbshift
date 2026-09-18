"""Checks the seeded application SQL against its own answer key.

The answer key is the thing Phase 4d is measured against, so it must not be
allowed to drift from the mapper files it describes. It already had two
arithmetic errors when first written -- a tier total of 6 where the entries said
7, and 17 statements where the mappers hold 16 -- both caught here rather than
by reading it.

This verifies the *fixture*, not the phase. It answers: does the answer key
describe these mapper files accurately, and is every seeded construct actually
present in the SQL? A phase that reports on itself proves nothing; a phase
measured against a key that is wrong proves less.

Run: python scripts/demo-app/verify_appsql.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAPPERS = HERE / "mappers"
KEY = HERE / "answer_key.json"

# One probe per construct id: a regex that must match the mapper text if the
# construct is genuinely seeded. Kept here rather than in the answer key so the
# key stays a statement of intent and this stays the check on it.
PROBES = {
    "APPSQL-001": r"FROM\s+DUAL",
    "APPSQL-002": r"\w+\.NEXTVAL",
    "APPSQL-003": r"=\s*\w+\.\w+\(\+\)",
    "APPSQL-004": r"ROWNUM",
    "APPSQL-005": r"\|\|",
    "APPSQL-006": r"NVL\(",
    "APPSQL-007": r"NVL\([^)]*,\s*''\s*\)",
    "APPSQL-008": r"DECODE\(",
    "APPSQL-009": r"TO_CHAR\([^)]*'DD-MON-YYYY'",
    "APPSQL-010": r"^\s*MINUS\s*$",
    "APPSQL-011": r"/\*\+[^*]*\*/",
    "APPSQL-012": r"MERGE\s+INTO",
    "APPSQL-013": r"SYSDATE",
    "APPSQL-014": r"FOR\s+UPDATE\s+NOWAIT",
    "APPSQL-015": r"CONNECT\s+BY",
    "APPSQL-016": r"LISTAGG\(",
    "APPSQL-017": r"TRUNC\(",
    "APPSQL-018": r"MONTHS_BETWEEN\(",
    "APPSQL-019": r"ADD_MONTHS\(",
    "APPSQL-020": r"SUBSTR\([^)]*,\s*-\d+\s*\)",
    "APPSQL-021": r"INSTR\([^)]*,[^)]*,[^)]*,[^)]*\)",
    "APPSQL-022": r"\bROWID\b",
    "APPSQL-023": r"TO_NUMBER\(",
    "APPSQL-024": r"\$\{\w+\}",
    "APPSQL-025": r"<selectKey",
    "APPSQL-026": r"\bTO_DATE\s*\(",
    # ORDER as an object name, not the ORDER BY clause.
    "APPSQL-027": r"(?:FROM|UPDATE)\s+ORDER\b",
}

TIERS = ("rule", "model", "manual")


class Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = Check()
    key = json.loads(KEY.read_text(encoding="utf-8"))
    constructs = key["constructs"]

    files = sorted(MAPPERS.glob("*.xml"))
    text = "".join(f.read_text(encoding="utf-8") for f in files)

    print("the fixture exists")
    c("mapper files are present", len(files) >= 2, f"found {len(files)}")
    c("the answer key parses", isinstance(constructs, list) and constructs)
    c("it says it is seeded", "seeded_on" in key and key["seeded_on"])
    c("it explains why it exists", len(key.get("why", "")) > 80)

    print("\nthe key is internally consistent")
    ids = [x["id"] for x in constructs]
    c("construct ids are unique", len(ids) == len(set(ids)))
    c("the construct count matches the entries",
      key["expected_totals"]["constructs"] == len(constructs),
      f'claims {key["expected_totals"]["constructs"]}, has {len(constructs)}')

    tiers = Counter(x["tier"] for x in constructs)
    for t in TIERS:
        c(f"the {t} total matches the entries",
          key["tier_totals"].get(t) == tiers.get(t, 0),
          f'claims {key["tier_totals"].get(t)}, has {tiers.get(t, 0)}')
    c("every tier is one of rule/model/manual",
      set(tiers) <= set(TIERS), f"saw {sorted(set(tiers))}")

    print("\nevery construct is described, not just named")
    for x in constructs:
        c(f'{x["id"]} names its PostgreSQL form', bool(x.get("postgresql")))
    missing_why = [x["id"] for x in constructs
                   if len(x.get("why_not_a_text_swap", "")) < 40]
    c("every construct explains why it is not a text swap",
      not missing_why, f"thin: {missing_why}")

    print("\nthe statements the key names really exist")
    declared = {s for x in constructs for s in x["statements"]}
    real = set(re.findall(r'<(?:select|insert|update|delete)\s+id="(\w+)"', text))
    c("the statement count matches the mappers",
      key["expected_totals"]["statements"] == len(real),
      f'claims {key["expected_totals"]["statements"]}, mappers hold {len(real)}')
    c("no construct names a statement that does not exist",
      not (declared - real), f"phantom: {sorted(declared - real)}")
    c("every statement carries at least one construct",
      not (real - declared), f"uncovered: {sorted(real - declared)}")

    print("\nevery seeded construct is actually in the SQL")
    c("there is a probe for every construct",
      set(PROBES) == set(ids),
      f"key-only {sorted(set(ids) - set(PROBES))}, probe-only {sorted(set(PROBES) - set(ids))}")
    for cid, pattern in sorted(PROBES.items()):
        c(f"{cid} is present in the mapper text",
          re.search(pattern, text, re.IGNORECASE | re.MULTILINE) is not None)

    print("\nthe fixture is honest about being one")
    c("each mapper says it is seeded",
      all("SEEDED APPLICATION SQL" in f.read_text(encoding="utf-8") for f in files))
    c("the key records the real schema it references",
      "DBMIG_APP" in key.get("scope_note", ""))

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    sys.exit(main())
