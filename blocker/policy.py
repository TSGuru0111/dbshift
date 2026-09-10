"""What a critical finding actually blocks.

A binary halt is correct but blunt. `NOARCHIVELOG` does not stop you provisioning
an instance or running a full-load migration -- it stops change data capture, and
therefore a low-downtime cutover. Saying "halted" without saying *what* is halted
sends people to fix the wrong thing.

So the gate answers two questions: may the run proceed at all, and which
downstream phases is each blocker actually standing in front of.
"""

from __future__ import annotations

# The phases downstream of the gate. Ordered as they execute.
DOWNSTREAM = [
    "provision",
    "migrate_full_load",
    "migrate_cdc",
    "validate",
    "cutover",
]

# Per-rule blast radius. A rule absent from here blocks everything -- an
# unrecognised critical must never be quietly assumed harmless.
BLOCKS = {
    "OPS-001": {
        "phases": ["migrate_cdc", "cutover"],
        "why": (
            "DMS change data capture reads redo. Without ARCHIVELOG there is no redo to read, "
            "so CDC cannot run and a low-downtime cutover is impossible. A full-load migration "
            "into an outage window is unaffected."
        ),
        "clears_when": "The source is switched to ARCHIVELOG, which requires a database restart.",
    },
    "OPS-002": {
        "phases": ["migrate_cdc", "cutover"],
        "why": (
            "Without minimum supplemental logging, redo omits the column data DMS needs to build "
            "an UPDATE on the target. CDC starts and then applies incomplete changes silently, "
            "which is worse than not running."
        ),
        "clears_when": "ALTER DATABASE ADD SUPPLEMENTAL LOG DATA on the source.",
    },
    "DQ-001": {
        "phases": ["migrate_cdc"],
        "why": (
            "DMS CDC cannot reliably apply updates or deletes to a table with no primary key or "
            "unique index. Rows can be duplicated or missed with no error raised. The full load "
            "of that table is fine."
        ),
        "clears_when": "A primary key or unique index is added, or the table is excluded from CDC.",
    },
    "RDS-004": {
        "phases": ["migrate_full_load", "validate"],
        "why": (
            "An external table reads a file from the database server's filesystem. RDS has none, "
            "so the object exists on the target and returns nothing. Validation of that object "
            "will fail by design."
        ),
        "clears_when": (
            "The data source moves to S3 with an RDS directory and S3 integration, or the table "
            "is replaced, or it is explicitly excluded from scope."
        ),
    },
}

UNKNOWN_BLOCKS_EVERYTHING = {
    "phases": list(DOWNSTREAM),
    "why": (
        "This critical rule has no recorded blast radius, so the gate assumes the worst and "
        "blocks every downstream phase. Add it to blocker/policy.py once its real effect is known."
    ),
    "clears_when": "Resolve the finding, or record its blast radius in blocker/policy.py.",
}


def blast_radius(rule_id: str) -> dict:
    return BLOCKS.get(rule_id, UNKNOWN_BLOCKS_EVERYTHING)


# A waiver is a named person accepting a specific blocker for a stated reason.
# Real migrations need this -- a client may knowingly accept a full-outage
# cutover rather than enable ARCHIVELOG. What they must never get is a blocker
# that disappears without anyone's name on it.
REQUIRED_WAIVER_FIELDS = ("rule_id", "approved_by", "reason")


def validate_waiver(waiver: dict) -> list[str]:
    missing = [f for f in REQUIRED_WAIVER_FIELDS if not str(waiver.get(f) or "").strip()]
    if missing:
        return [f"waiver is missing {', '.join(missing)}"]
    if len(str(waiver["reason"]).strip()) < 15:
        return ["waiver reason is too short to be a reason"]
    return []
