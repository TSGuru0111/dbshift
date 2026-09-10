"""User-curated settings: which probes and rules run, plus custom ones.

Built-in probes and rules are never edited in place. Disabling one records its
name here instead, so the shipped catalogue stays intact and a disabled check
can always be turned back on. Custom additions live alongside rather than being
merged into the shipped files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"

DEFAULTS = {
    "disabled_probes": [],
    "disabled_rules": [],
    "custom_probes": [],
    "custom_rules": [],
}

# Read-only enforcement, in depth. The collector account is already read-only at
# the database, but a typo should fail here with a clear message rather than
# relying on a grant to catch it.
FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|merge|drop|truncate|alter|create|grant|revoke|"
    r"commit|rollback|execute|begin|declare)\b",
    re.IGNORECASE,
)
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
RULE_ID = re.compile(r"^[A-Z][A-Z0-9]{1,9}-[0-9]{1,4}$")

VALID_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
VALID_LEVELS = ("L1", "L2", "L3", "L4")
VALID_CATEGORIES = (
    "rds_compatibility",
    "data_quality",
    "performance",
    "security",
    "operational_risk",
)


class SettingsError(ValueError):
    pass


def load() -> dict:
    if not SETTINGS_PATH.exists():
        return json.loads(json.dumps(DEFAULTS))
    data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    for key, default in DEFAULTS.items():
        data.setdefault(key, json.loads(json.dumps(default)))
    return data


def save(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def validate_select(sql: str, label: str) -> str:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise SettingsError(f"{label}: SQL is empty")
    if not sql.lower().lstrip("( \n\t").startswith("select"):
        raise SettingsError(f"{label}: must start with SELECT")
    hit = FORBIDDEN_SQL.search(sql)
    if hit:
        raise SettingsError(
            f"{label}: '{hit.group(0)}' is not allowed — discovery and assessment read only"
        )
    return sql


def add_custom_probe(name: str, description: str, sql: str, dataset: str | None = None) -> dict:
    data = load()
    name = (name or "").strip().lower()
    if not IDENTIFIER.match(name):
        raise SettingsError(
            "probe name must be lowercase letters, digits and underscores, starting with a letter"
        )
    if any(p["name"] == name for p in data["custom_probes"]):
        raise SettingsError(f"a custom probe named {name!r} already exists")
    probe = {
        "name": name,
        "description": (description or "").strip() or "Custom discovery query",
        "sql": validate_select(sql, f"probe {name}"),
        "dataset": dataset or f"source_inventory.custom_{name}",
    }
    data["custom_probes"].append(probe)
    save(data)
    return probe


def add_custom_rule(rule: dict) -> dict:
    data = load()
    rule_id = (rule.get("rule_id") or "").strip().upper()
    if not RULE_ID.match(rule_id):
        raise SettingsError("rule_id must look like ABC-123")

    from assess import engine as assess_engine

    builtin = {r["rule_id"] for r in assess_engine.load_rules()}
    if rule_id in builtin or any(r["rule_id"] == rule_id for r in data["custom_rules"]):
        raise SettingsError(f"rule_id {rule_id} is already taken")

    for field, allowed in (
        ("category", VALID_CATEGORIES),
        ("severity", VALID_SEVERITIES),
        ("remediation_level", VALID_LEVELS),
    ):
        if rule.get(field) not in allowed:
            raise SettingsError(f"{field} must be one of {', '.join(allowed)}")
    if not (rule.get("title") or "").strip():
        raise SettingsError("title is required")

    entry = {
        "rule_id": rule_id,
        "category": rule["category"],
        "severity": rule["severity"],
        "remediation_level": rule["remediation_level"],
        "title": rule["title"].strip(),
        "rationale": (rule.get("rationale") or "").strip() or "Custom rule.",
        "sql": validate_select(rule.get("sql", ""), f"rule {rule_id}"),
        "custom": True,
    }
    data["custom_rules"].append(entry)
    save(data)
    return entry


def delete_custom(kind: str, identifier: str) -> None:
    data = load()
    key = "custom_probes" if kind == "probe" else "custom_rules"
    field = "name" if kind == "probe" else "rule_id"
    before = len(data[key])
    data[key] = [x for x in data[key] if x[field] != identifier]
    if len(data[key]) == before:
        raise SettingsError(f"no custom {kind} named {identifier!r}")
    save(data)


def set_enabled(kind: str, identifier: str, enabled: bool) -> None:
    data = load()
    key = "disabled_probes" if kind == "probe" else "disabled_rules"
    disabled = set(data[key])
    disabled.discard(identifier) if enabled else disabled.add(identifier)
    data[key] = sorted(disabled)
    save(data)
