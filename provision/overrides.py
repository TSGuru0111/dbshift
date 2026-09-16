"""What a person is allowed to change about the target, and what it costs to say so.

Phase 6 renders the instance from the Phase 3 decision, and every value carries a
line saying where it came from. That is the point of the screen: a client can ask
"why this instance?" and get an answer from evidence rather than from taste.

A manual override is therefore not a free-form edit. It is a *claim that the
evidence was wrong*, and it has to be recorded as one:

  1. **The derived value is never discarded.** It stays in the provenance row
     next to what the person chose, so "where each value came from" still answers
     the question honestly -- it now says "a person, over the evidence, because".
  2. **A reason is required.** Not a checkbox. An override without a reason is
     indistinguishable from a mis-click three months later.
  3. **Overrides that cost money say so.** Multi-AZ doubles the instance bill;
     backup retention and storage add to it. The warning is attached to the
     field, not buried in a note at the bottom.
  4. **Some things cannot be overridden at all.** Not because a person could not
     be trusted with them, but because changing them silently invalidates
     something already proven: the engine version is what Phase 4b compiled
     against, and the character set cannot be altered after the instance exists.

The split between this module and `policy.py` is deliberate. `policy` holds what
the project has *decided* -- the cost guardrails for a beta on $100 of credit.
This holds what a *client* may decide differently on their own account, which is
a different question with a different answer.
"""

from __future__ import annotations

from . import policy

# ---------------------------------------------------------------------------
# Instance classes offered in the console's picker.
#
# Not a free-text field: a typo like "db.t3.medum" fails at deploy time with a
# CloudFormation error, minutes later, after a stack has begun. The list is
# checked against what the region actually offers by the existing
# `preflight.aws_checks` call, so an entry here that AWS will not sell is caught
# before anything is created.
#
# vCPU/GiB are carried so the console can show what a choice means next to what
# Phase 3 derived, rather than making a person look it up.
INSTANCE_CLASSES = [
    {"class": "db.t3.small",    "vcpu": 2,  "memory_gib": 2,   "family": "burstable"},
    {"class": "db.t3.medium",   "vcpu": 2,  "memory_gib": 4,   "family": "burstable"},
    {"class": "db.t3.large",    "vcpu": 2,  "memory_gib": 8,   "family": "burstable"},
    {"class": "db.t3.xlarge",   "vcpu": 4,  "memory_gib": 16,  "family": "burstable"},
    {"class": "db.t3.2xlarge",  "vcpu": 8,  "memory_gib": 32,  "family": "burstable"},
    {"class": "db.m5.large",    "vcpu": 2,  "memory_gib": 8,   "family": "general purpose"},
    {"class": "db.m5.xlarge",   "vcpu": 4,  "memory_gib": 16,  "family": "general purpose"},
    {"class": "db.m5.2xlarge",  "vcpu": 8,  "memory_gib": 32,  "family": "general purpose"},
    {"class": "db.m5.4xlarge",  "vcpu": 16, "memory_gib": 64,  "family": "general purpose"},
    {"class": "db.r5.large",    "vcpu": 2,  "memory_gib": 16,  "family": "memory optimised"},
    {"class": "db.r5.xlarge",   "vcpu": 4,  "memory_gib": 32,  "family": "memory optimised"},
    {"class": "db.r5.2xlarge",  "vcpu": 8,  "memory_gib": 64,  "family": "memory optimised"},
    {"class": "db.r5.4xlarge",  "vcpu": 16, "memory_gib": 128, "family": "memory optimised"},
]

INSTANCE_BY_CLASS = {i["class"]: i for i in INSTANCE_CLASSES}

# Burstable instances accrue CPU credits and throttle when they run out. That is
# fine for a rehearsal and wrong for a production cutover, and the difference is
# not visible in vCPU/GiB -- so it is said out loud where it is chosen.
BURSTABLE_NOTE = ("burstable: CPU is credit-limited and throttles under sustained "
                  "load, which a migration cutover usually is")


# ---------------------------------------------------------------------------
# The database configuration a person fills in.
#
# Each field carries its default, its validation, and -- where relevant -- what
# it does to the bill. `policy` holds the same defaults; they are referenced
# rather than repeated so the two cannot drift.
def _f(name, label, default, kind, **kw):
    return {"name": name, "label": label, "default": default, "kind": kind, **kw}


CONFIG_FIELDS = [
    _f("db_name", "Database name", policy.DB_NAME, "text",
       maxlen=8, pattern=r"^[A-Za-z][A-Za-z0-9]*$",
       help="RDS Oracle allows at most 8 characters, starting with a letter. "
            "It cannot be changed after the instance is created."),
    _f("master_username", "Master username", policy.MASTER_USERNAME, "text",
       maxlen=16, pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
       help="The admin account RDS creates. Not a person's name -- it is shared, "
            "and its password lives in SSM Parameter Store."),
    _f("port", "Port", policy.PORT, "int", min=1150, max=65535,
       help="1521 for Oracle, 5432 for PostgreSQL. Changing it is a firewall "
            "decision, not a database one."),
    _f("backup_retention_days", "Backup retention (days)", policy.BACKUP_RETENTION_DAYS,
       "int", min=0, max=35, billing=True,
       help="0 disables automated backups entirely -- and with them, "
            "point-in-time recovery. Each retained day costs storage."),
    _f("multi_az", "Multi-AZ standby", policy.MULTI_AZ, "bool", billing=True,
       help="A standby in a second availability zone. It DOUBLES the instance "
            "bill. A rehearsal target does not need one; a production cutover "
            "usually does."),
    _f("deletion_protection", "Deletion protection", policy.DELETION_PROTECTION, "bool",
       help="Blocks deletion until it is turned off. The kill switch cannot "
            "remove an instance that has it, which is the point -- and the risk, "
            "on an account running on credit."),
    _f("auto_minor_version_upgrade", "Auto minor version upgrade",
       policy.AUTO_MINOR_UPGRADE, "bool",
       help="Off keeps rehearsal and validation reproducible: the version you "
            "validated is the version you cut over to."),
    _f("performance_insights", "Performance Insights", policy.PERFORMANCE_INSIGHTS, "bool",
       billing=True,
       help="Query-level performance history. Free for 7 days of retention on "
            "most classes, billed beyond that, and unavailable on some small ones."),
    _f("storage_encrypted", "Encrypt storage", policy.STORAGE_ENCRYPTED, "bool",
       help="AWS-managed key. Turning this OFF is almost always wrong, and it "
            "cannot be turned on afterwards without rebuilding the instance."),
]

CONFIG_BY_NAME = {f["name"]: f for f in CONFIG_FIELDS}

# Deliberately not overridable, with the reason each refusal exists.
LOCKED = {
    "engine_version": "Phase 4b compiled the converted code against this major version. "
                      "Changing it here would invalidate a gate that has already passed.",
    "character_set": "A character set cannot be altered after the instance is created, "
                     "and Phase 8 compares against the source's.",
    "engine": "The engine is the Phase 3 target decision. Changing it is a different "
              "migration, not a different instance.",
    "region": "The account, the S3 exchange bucket and the DMS instance are all in "
              f"{policy.REGION}. A target elsewhere would cross-charge egress.",
}


class OverrideError(ValueError):
    """A proposed override that will not be accepted, with why."""


def _require_reason(reason: str | None, what: str) -> str:
    reason = (reason or "").strip()
    if len(reason) < 8:
        raise OverrideError(
            f"{what} needs a reason of at least 8 characters. The rendered plan "
            "records why every value is what it is, and an override with no reason "
            "cannot be audited later."
        )
    return reason


def validate_instance_class(proposed: str, derived: str, reason: str | None) -> dict:
    """Check a manual instance choice. Returns the override record."""
    if proposed not in INSTANCE_BY_CLASS:
        raise OverrideError(
            f"{proposed!r} is not an instance class this console offers. "
            f"Choose one of {len(INSTANCE_CLASSES)} listed, or leave it derived."
        )
    if proposed == derived:
        raise OverrideError(
            f"{proposed} is already what Phase 3 derived. Nothing to override."
        )
    reason = _require_reason(reason, "an instance class override")
    spec = INSTANCE_BY_CLASS[proposed]
    derived_spec = INSTANCE_BY_CLASS.get(derived)

    notes = []
    if spec["family"] == "burstable":
        notes.append(BURSTABLE_NOTE)
    # Smaller than derived is the case worth naming: Phase 3 sized from measured
    # load or a capacity floor, and going under it is a decision to accept less.
    if derived_spec:
        if spec["vcpu"] < derived_spec["vcpu"] or spec["memory_gib"] < derived_spec["memory_gib"]:
            notes.append(
                f"smaller than the derived {derived}: "
                f"{spec['vcpu']} vCPU / {spec['memory_gib']} GiB against "
                f"{derived_spec['vcpu']} / {derived_spec['memory_gib']}"
            )
        elif spec["vcpu"] > derived_spec["vcpu"] or spec["memory_gib"] > derived_spec["memory_gib"]:
            notes.append(f"larger than the derived {derived}, so it costs more per hour")

    return {
        "property": "DBInstanceClass",
        "derived": derived,
        "chosen": proposed,
        "reason": reason,
        "notes": notes,
        "vcpu": spec["vcpu"],
        "memory_gib": spec["memory_gib"],
    }


def _coerce(field: dict, value):
    kind = field["kind"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "yes", "no"}:
            return value.strip().lower() in {"true", "yes"}
        raise OverrideError(f"{field['label']} must be true or false, not {value!r}")
    if kind == "int":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise OverrideError(f"{field['label']} must be a whole number, not {value!r}")
        if "min" in field and n < field["min"]:
            raise OverrideError(f"{field['label']} must be at least {field['min']}")
        if "max" in field and n > field["max"]:
            raise OverrideError(f"{field['label']} must be at most {field['max']}")
        return n
    # text
    s = str(value).strip()
    if not s:
        raise OverrideError(f"{field['label']} cannot be empty")
    if "maxlen" in field and len(s) > field["maxlen"]:
        raise OverrideError(
            f"{field['label']} is at most {field['maxlen']} characters; {s!r} is {len(s)}")
    if "pattern" in field:
        import re
        if not re.match(field["pattern"], s):
            raise OverrideError(
                f"{s!r} is not a valid {field['label'].lower()}. {field.get('help', '')}".strip())
    return s


def validate_config(values: dict, reason: str | None) -> dict:
    """Check a set of database-configuration values against the defaults.

    Only values that actually *differ* from the default count as overrides --
    a form that posts every field should not record nine overrides when the
    person changed one.
    """
    if not isinstance(values, dict):
        raise OverrideError("configuration must be a set of named values")

    unknown = set(values) - set(CONFIG_BY_NAME)
    if unknown:
        locked = sorted(unknown & set(LOCKED))
        if locked:
            raise OverrideError(f"{locked[0]} cannot be overridden. {LOCKED[locked[0]]}")
        raise OverrideError(f"not a configurable field: {sorted(unknown)[0]}")

    changed, accepted, billing_changes = [], {}, []
    for name, raw in values.items():
        field = CONFIG_BY_NAME[name]
        value = _coerce(field, raw)
        accepted[name] = value
        if value != field["default"]:
            changed.append({"field": name, "label": field["label"],
                            "default": field["default"], "chosen": value,
                            "billing": bool(field.get("billing"))})
            if field.get("billing"):
                billing_changes.append(field["label"])

    if changed:
        reason = _require_reason(reason, "a configuration override")
    return {
        "values": accepted,
        "changed": changed,
        "billing_changes": billing_changes,
        "reason": reason if changed else None,
    }


def effective_policy(config: dict | None) -> dict:
    """The policy values the render should use, defaults unless overridden.

    Returned as a plain dict rather than mutating `policy`, because `policy` is
    module state shared by the kill switch and the CLI -- a console override
    must not leak into a later CLI run in the same process.
    """
    out = {f["name"]: f["default"] for f in CONFIG_FIELDS}
    if config:
        out.update(config.get("values") or {})
    return out


def describe() -> dict:
    """What the console needs to draw the two forms."""
    return {
        "instance_classes": INSTANCE_CLASSES,
        "config_fields": CONFIG_FIELDS,
        "locked": [{"property": k, "why": v} for k, v in LOCKED.items()],
    }
