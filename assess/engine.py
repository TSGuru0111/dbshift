from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("assess.engine")

RULES_PATH = Path(__file__).resolve().parent / "rules.json"

REQUIRED_FIELDS = (
    "rule_id",
    "category",
    "severity",
    "remediation_level",
    "title",
    "rationale",
    "sql",
)
VALID_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}
VALID_LEVELS = {"L1", "L2", "L3", "L4"}
VALID_CATEGORIES = {
    "rds_compatibility",
    "data_quality",
    "performance",
    "security",
    "operational_risk",
}


def load_rules(path: Path = RULES_PATH) -> list[dict]:
    rules = json.loads(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    for rule in rules:
        missing = [f for f in REQUIRED_FIELDS if not rule.get(f)]
        if missing:
            raise ValueError(f"rule {rule.get('rule_id','?')} missing fields: {missing}")
        if rule["rule_id"] in seen:
            raise ValueError(f"duplicate rule_id: {rule['rule_id']}")
        if rule["severity"] not in VALID_SEVERITIES:
            raise ValueError(f"{rule['rule_id']}: bad severity {rule['severity']}")
        if rule["remediation_level"] not in VALID_LEVELS:
            raise ValueError(f"{rule['rule_id']}: bad remediation_level")
        if rule["category"] not in VALID_CATEGORIES:
            raise ValueError(f"{rule['rule_id']}: bad category {rule['category']}")
        seen.add(rule["rule_id"])
    return rules


def install_rules(conn: sqlite3.Connection, rules: list[dict]) -> None:
    """Rules live in a table, not in code. Adding rule 49 is inserting a row."""
    conn.execute("DROP TABLE IF EXISTS rules")
    conn.execute(
        """CREATE TABLE rules (
               rule_id TEXT PRIMARY KEY, category TEXT, severity TEXT,
               remediation_level TEXT, title TEXT, rationale TEXT, sql TEXT)"""
    )
    conn.executemany(
        "INSERT INTO rules VALUES (:rule_id, :category, :severity, "
        ":remediation_level, :title, :rationale, :sql)",
        [{k: r[k] for k in REQUIRED_FIELDS} for r in rules],
    )
    conn.commit()


def _finding_id(rule_id: str, owner, object_name) -> str:
    key = f"{rule_id}|{owner or ''}|{object_name or ''}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def evaluate(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """Run every rule. Severity and remediation level come from the rule row,
    never from anything computed at runtime -- that is what keeps a model out of
    the decision and makes the result reproducible."""
    conn.row_factory = sqlite3.Row
    rules = [dict(r) for r in conn.execute("SELECT * FROM rules ORDER BY rule_id")]
    findings: list[dict] = []
    errors: list[dict] = []

    for rule in rules:
        try:
            rows = [dict(r) for r in conn.execute(rule["sql"])]
        except sqlite3.Error as exc:
            errors.append({"rule_id": rule["rule_id"], "error": str(exc)})
            log.warning("rule=%s FAILED %s", rule["rule_id"], exc)
            continue

        for row in rows:
            owner = row.get("owner")
            object_name = row.get("object_name")
            findings.append(
                {
                    "finding_id": _finding_id(rule["rule_id"], owner, object_name),
                    "rule_id": rule["rule_id"],
                    "category": rule["category"],
                    "severity": rule["severity"],
                    "remediation_level": rule["remediation_level"],
                    "title": rule["title"],
                    "rationale": rule["rationale"],
                    "owner": owner,
                    "object_name": object_name,
                    "object_type": row.get("object_type"),
                    "detail": row.get("detail"),
                }
            )
        log.info("rule=%s findings=%d", rule["rule_id"], len(rows))

    findings.sort(key=lambda f: (f["rule_id"], f["owner"] or "", f["object_name"] or ""))
    return findings, errors
