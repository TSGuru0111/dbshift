from __future__ import annotations

import json
import math
import os
from collections import Counter
from pathlib import Path

# Both of these are overridable so a second estate can be scored against its own
# seeded defects without editing this file. They are read at import time from the
# environment, defaulting to the DBMIG_APP reference estate that ships here.
#
#   DBSHIFT_ANSWER_KEY       path to the answer key JSON
#   DBSHIFT_REFERENCE_SCHEMA owner the key's defects were seeded into
#
# The pairing matters: an answer key is only meaningful against the schema it was
# written for, so pointing one of these at a new estate without the other would
# silently measure recall against the wrong database.
ANSWER_KEY_PATH = Path(
    os.environ.get("DBSHIFT_ANSWER_KEY")
    or Path(__file__).resolve().parent / "answer_key.json"
)

# The estate the seeded defects live in. Recall is only meaningful against it.
REFERENCE_SCHEMA = os.environ.get("DBSHIFT_REFERENCE_SCHEMA", "DBMIG_APP")

# Every estate that ships with seeded defects, and the key describing them.
#
# The env vars above configure ONE key, which suits the CLI: it is invoked per
# estate and its environment says which. A long-running process -- the console --
# serves whichever estate the operator connected to, and cannot re-read an
# env var it inherited at import. `resolve_answer_key` picks from this registry
# by the owners actually present in the findings, so the console reports real
# recall on either estate without a restart.
#
# Add a row when an estate gains a seeded-defect key. A schema absent here still
# assesses normally; it just gets `applicable: false` for recall, which is the
# honest answer rather than a misleading 0%.
_HERE = Path(__file__).resolve().parent
KNOWN_ANSWER_KEYS: dict[str, Path] = {
    "DBMIG_APP": _HERE / "answer_key.json",
    "DBMIG_TELCO": _HERE.parent / "scripts" / "telco-source" / "answer_key.json",
}


def resolve_answer_key(owners: set[str] | None) -> tuple[Path, str]:
    """Pick the answer key matching the estate these findings came from.

    An explicit DBSHIFT_ANSWER_KEY always wins -- it is how the CLI pins a key,
    including one not in the registry. Otherwise match the registry against the
    owners present. Falls back to the configured default, which then reports
    itself inapplicable rather than measuring the wrong estate.
    """
    if os.environ.get("DBSHIFT_ANSWER_KEY"):
        return ANSWER_KEY_PATH, REFERENCE_SCHEMA
    for schema, path in KNOWN_ANSWER_KEYS.items():
        if owners and schema in owners and path.exists():
            return path, schema
    return ANSWER_KEY_PATH, REFERENCE_SCHEMA

CATEGORIES = (
    "rds_compatibility",
    "data_quality",
    "performance",
    "security",
    "operational_risk",
)

SEVERITY_PENALTY = {"CRITICAL": 25, "HIGH": 10, "MEDIUM": 4, "LOW": 1, "INFO": 0}

# Penalty accrues per RULE, not per finding, scaled by how many objects it hit.
# One rule firing on 10 objects is one issue at wider blast radius, not ten
# issues -- log scaling makes 10 hits cost twice 1 hit, not ten times.
VOLUME_LOG_BASE = 10

# Score decays exponentially from the penalty rather than subtracting from 100.
# A linear subtraction saturates: past ~100 penalty every category reads 0, so a
# bad category and a catastrophic one look identical. Decay keeps resolution at
# every level and never reaches exactly zero.
DECAY_CONSTANT = 60

# docs/02-architecture.md: any critical finding caps the overall score.
CRITICAL_CAP = 60


def _category_penalty(findings: list[dict]) -> float:
    per_rule = Counter(f["rule_id"] for f in findings)
    severity_of = {f["rule_id"]: f["severity"] for f in findings}
    total = 0.0
    for rule_id, count in per_rule.items():
        base = SEVERITY_PENALTY[severity_of[rule_id]]
        total += base * (1 + math.log(count, VOLUME_LOG_BASE))
    return total


def _score_from_penalty(penalty: float) -> int:
    return round(100 * math.exp(-penalty / DECAY_CONSTANT))


def score_findings(findings: list[dict]) -> dict:
    by_category: dict[str, dict] = {}
    for category in CATEGORIES:
        subset = [f for f in findings if f["category"] == category]
        penalty = _category_penalty(subset)
        counts = {sev: sum(1 for f in subset if f["severity"] == sev) for sev in SEVERITY_PENALTY}
        by_category[category] = {
            "score": _score_from_penalty(penalty),
            "findings": len(subset),
            "rules_fired": len({f["rule_id"] for f in subset}),
            "penalty": round(penalty, 1),
            "by_severity": counts,
        }

    raw_overall = round(sum(c["score"] for c in by_category.values()) / len(CATEGORIES))
    critical_count = sum(1 for f in findings if f["severity"] == "CRITICAL")
    overall = min(raw_overall, CRITICAL_CAP) if critical_count else raw_overall

    return {
        "overall_score": overall,
        "raw_overall_score": raw_overall,
        "capped_by_critical": bool(critical_count),
        "critical_findings": critical_count,
        "total_findings": len(findings),
        "by_category": by_category,
        "by_severity": {
            sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITY_PENALTY
        },
        "by_remediation_level": {
            lvl: sum(1 for f in findings if f["remediation_level"] == lvl)
            for lvl in ("L1", "L2", "L3", "L4")
        },
    }


def score_against_answer_key(
    findings: list[dict],
    path: Path | None = None,
    owners: set[str] | None = None,
) -> dict:
    """Measure recall against the seeded defects for this estate.

    Findings that match no seeded defect are reported as `additional`, not as
    false positives. Most are genuine facts about the estate -- NOARCHIVELOG,
    grants to PUBLIC -- and calling them false positives would be wrong. A real
    false-positive count needs human triage, so it is reported as pending.

    `path` omitted means resolve the key from `owners`, so a caller that serves
    more than one estate gets the right one without configuring anything.
    """
    if path is None:
        path, reference_schema = resolve_answer_key(owners)
    else:
        reference_schema = REFERENCE_SCHEMA
    key = json.loads(path.read_text(encoding="utf-8"))

    # The answer key describes defects seeded into one specific reference estate.
    # Against any other database it measures nothing, and reporting 0% recall
    # there would read as a broken engine rather than an inapplicable metric.
    if owners is not None and reference_schema not in owners:
        return {
            "applicable": False,
            "reason": (
                f"The seeded-defect answer key applies to the {reference_schema} reference "
                "estate, which is not present in this database. Recall is not measured here — "
                "the findings above are still real."
            ),
            "total_seeded": len(key),
            "detectable": 0,
            "detected": 0,
            "missed": 0,
            "severity_exact": 0,
            "recall": None,
            "not_present_in_source": 0,
            "additional_findings": len(findings),
            "false_positives_confirmed": 0,
            "triage_pending": len(findings),
            "defects": [],
        }

    matched_ids: set[str] = set()
    results = []

    for defect in key:
        needle = defect["match_object_like"].upper()
        hits = [
            f
            for f in findings
            if f["rule_id"] in defect["match_rules"]
            and needle in (f["object_name"] or "").upper()
        ]
        matched_ids.update(f["finding_id"] for f in hits)
        severities = sorted({f["severity"] for f in hits})
        results.append(
            {
                "defect": defect["defect"],
                "title": defect["title"],
                "object": defect["object"],
                "expected_severity": defect["expected_severity"],
                "detected": bool(hits),
                "not_present_in_source": bool(defect.get("not_present_in_source")),
                "note": defect.get("note"),
                "matched_rules": sorted({f["rule_id"] for f in hits}),
                "reported_severities": severities,
                "severity_matches": defect["expected_severity"] in severities,
            }
        )

    detected = [r for r in results if r["detected"]]
    additional = [f for f in findings if f["finding_id"] not in matched_ids]

    # A defect that was never successfully seeded is not an engine miss. Counting
    # it as one understates recall and hides a broken seed script.
    absent = [r for r in results if r["not_present_in_source"] and not r["detected"]]
    detectable = len(key) - len(absent)

    return {
        "applicable": True,
        "total_seeded": len(key),
        "not_present_in_source": len(absent),
        "detectable": detectable,
        "detected": len(detected),
        "missed": detectable - len(detected),
        "severity_exact": sum(1 for r in detected if r["severity_matches"]),
        "recall": round(len(detected) / detectable, 3) if detectable else 0.0,
        "recall_against_all_seeded": round(len(detected) / len(key), 3) if key else 0.0,
        "additional_findings": len(additional),
        "false_positives_confirmed": 0,
        "triage_pending": len(additional),
        "defects": results,
    }
