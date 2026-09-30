"""Checks the seeded MySQL application SQL against its own answer key.

The fixture, not the phase: does answer_key.json describe these mapper files
accurately, and is every seeded construct actually present in the statement
the key names? Independent of `appsql/` on purpose -- its own probes, its own
XML reading -- so the phase is measured against something it did not produce.

Run: python scripts/demo-app-mysql/verify_appsql.py
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEY = json.loads((HERE / "answer_key.json").read_text(encoding="utf-8"))

PROBES = {
    "MYAPP-001": r"`\w+`",
    "MYAPP-002": r"IFNULL\(",
    "MYAPP-003": r"LIMIT\s+#\{\w+\}\s*,\s*#\{\w+\}",
    "MYAPP-004": r"CURDATE\(\)",
    "MYAPP-005": r"NOW\(\)",
    "MYAPP-006": r"\bLIKE\b",
    "MYAPP-007": r"CONCAT\(",
    "MYAPP-008": r"\bIF\(",
    "MYAPP-009": r"INSERT\s+IGNORE",
    "MYAPP-010": r"SQL_CALC_FOUND_ROWS|FOUND_ROWS\(\)",
    "MYAPP-011": r"@rnk",
    "MYAPP-012": r"\$\{\w+\}",
    "MYAPP-013": r"ON\s+DUPLICATE\s+KEY\s+UPDATE",
    "MYAPP-014": r"GROUP_CONCAT\(",
    "MYAPP-015": r"DATE_FORMAT\(",
    "MYAPP-016": r"DATE_SUB\(|INTERVAL\s+#\{\w+\}\s+DAY",
    "MYAPP-017": r"STR_TO_DATE\(",
    "MYAPP-018": r"REPLACE\s+INTO",
    "MYAPP-019": r"JSON_EXTRACT\(",
    "MYAPP-020": r"LAST_INSERT_ID\(\)",
    "MYAPP-021": r"LOCK\s+IN\s+SHARE\s+MODE",
    "MYAPP-022": r"MATCH\s*\([^)]*\)\s*AGAINST",
    "MYAPP-023": r"`order`",
}
TIER_RANK = {"rule": 0, "model": 1, "manual": 2}


def statements() -> dict[str, str]:
    out = {}
    for f in sorted((HERE / "mappers").glob("*.xml")):
        root = ET.parse(f).getroot()
        for el in root:
            if el.tag in ("select", "insert", "update", "delete"):
                out[el.get("id")] = "".join(el.itertext())
    return out


def main() -> int:
    ok = bad = 0

    def check(label, cond, detail=""):
        nonlocal ok, bad
        ok, bad = (ok + 1, bad) if cond else (ok, bad + 1)
        print(f"  [{'ok' if cond else 'FAIL'}] {label}" + ("" if cond else f" -- {detail}"))

    stmts = statements()
    exp = KEY["expected_totals"]
    check(f"{exp['statements']} statements in the mappers", len(stmts) == exp["statements"], str(len(stmts)))
    check("every construct has a probe", {c["id"] for c in KEY["constructs"]} == set(PROBES))

    occurrences, worst = 0, {}
    for c in KEY["constructs"]:
        for sid in c["statements"]:
            text = stmts.get(sid)
            check(f"{c['id']} {c['construct']} is really in {sid}",
                  text is not None and re.search(PROBES[c["id"]], text, re.IGNORECASE),
                  "statement missing" if text is None else "probe did not match")
            occurrences += 1
            if TIER_RANK[c["tier"]] >= TIER_RANK.get(worst.get(sid), -1):
                worst[sid] = c["tier"]
    check(f"{exp['construct_occurrences']} construct occurrences",
          occurrences == exp["construct_occurrences"], str(occurrences))
    check("every statement carries at least one seeded construct", set(worst) == set(stmts),
          str(sorted(set(stmts) - set(worst))))
    tiers = dict(Counter(worst.values()))
    check(f"statement tiers {exp['statement_tiers']}", tiers == exp["statement_tiers"], str(tiers))

    print(f"\n{ok}/{ok + bad} checks passed" + (f", {bad} FAILED" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
