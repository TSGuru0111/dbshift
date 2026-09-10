"""Deterministic fixes.

Where a finding has exactly one correct remedy, that remedy is a template and no
model is involved. A model adds nothing to "the foreign key needs an index" --
it adds cost, latency and a chance of being wrong.

A template returns SQL **and** its rollback, or it declines. Declining is a
normal outcome, not a failure: it routes the finding to generation or to a human.
"""

from __future__ import annotations

import re

IDENT = re.compile(r"^[A-Za-z0-9_$#]+$")


class TemplateError(ValueError):
    pass


def _q(name: str) -> str:
    if not IDENT.match(name or ""):
        raise TemplateError(f"refusing to build SQL around identifier {name!r}")
    return f'"{name}"'


def _split_object(object_name: str) -> tuple[str, str | None]:
    """`TABLE` or `TABLE.COLUMN` as the rules emit them."""
    parts = (object_name or "").split(".")
    if len(parts) == 1:
        return parts[0], None
    if len(parts) == 2:
        return parts[0], parts[1]
    raise TemplateError(f"cannot parse object name {object_name!r}")


def _index_name(table: str, column: str) -> str:
    # Oracle identifiers cap at 128 bytes on 12.2+; stay well inside it.
    base = f"IX_{table}_{column}"[:120]
    return base.upper()


def unindexed_foreign_key(finding: dict) -> dict:
    """PERF-001 -- create the missing index on the FK's leading column."""
    table, column = _split_object(finding["object_name"])
    if not column:
        raise TemplateError("PERF-001 needs TABLE.COLUMN")
    owner, name = finding["owner"], _index_name(table, column)
    return {
        "sql": f"CREATE INDEX {_q(owner)}.{_q(name)} ON {_q(owner)}.{_q(table)} ({_q(column)})",
        "rollback_sql": f"DROP INDEX {_q(owner)}.{_q(name)}",
        "explain": (
            f"An unindexed foreign key makes Oracle full-scan and share-lock {table} on every "
            "parent delete or update. The index removes both."
        ),
        "caveat": (
            "Creating an index on a large table takes time and temporary space. Consider "
            "ONLINE and a parallel degree during a maintenance window."
        ),
    }


def stale_statistics(finding: dict) -> dict:
    """DQ-007 -- regather, with a restore point so it is reversible."""
    table, _ = _split_object(finding["object_name"])
    owner = finding["owner"]
    return {
        "sql": (
            f"BEGIN DBMS_STATS.GATHER_TABLE_STATS(ownname => '{owner}', "
            f"tabname => '{table}', cascade => TRUE, "
            "estimate_percent => DBMS_STATS.AUTO_SAMPLE_SIZE); END;"
        ),
        # Statistics are derived, so "undo" means restoring the previous set.
        # Oracle keeps history, which is what makes this reversible at all.
        "rollback_sql": (
            f"BEGIN DBMS_STATS.RESTORE_TABLE_STATS(ownname => '{owner}', "
            f"tabname => '{table}', as_of_timestamp => SYSTIMESTAMP - INTERVAL '1' HOUR); END;"
        ),
        "explain": (
            f"Sizing and cardinality decisions read these statistics. Stale numbers on {table} "
            "produce a wrong target size and hide data-quality problems."
        ),
        "caveat": (
            "Regathering changes execution plans. Rollback restores the previous statistics "
            "from Oracle's history, which must still be within the retention window."
        ),
    }


def unusable_index(finding: dict) -> dict:
    """PERF-004 -- rebuild an index that is not being maintained."""
    table, _ = _split_object(finding["object_name"])
    owner = finding["owner"]
    if "UNUSABLE" not in (finding.get("detail") or "").upper():
        # Invisible is a deliberate state, not a defect. Do not "fix" it.
        raise TemplateError("index is invisible rather than unusable; that is a choice, not a fault")
    return {
        "sql": f"ALTER INDEX {_q(owner)}.{_q(table)} REBUILD",
        # A rebuild has no inverse: the prior state was "unusable", which is not
        # a state worth restoring. Say so rather than inventing a rollback.
        "rollback_sql": f"ALTER INDEX {_q(owner)}.{_q(table)} UNUSABLE",
        "explain": "An unusable index is neither maintained nor used. Rebuilding restores it.",
        "caveat": "Rollback returns the index to UNUSABLE, which is the state it was found in.",
    }


def byte_length_semantics(finding: dict) -> dict:
    """DQ-009 -- move a column from BYTE to CHAR semantics."""
    table, column = _split_object(finding["object_name"])
    if not column:
        raise TemplateError("DQ-009 needs TABLE.COLUMN")
    owner = finding["owner"]
    # The declared length is not in the finding, so this template deliberately
    # declines rather than guessing a width and silently truncating a column.
    raise TemplateError(
        "changing character semantics needs the column's declared length, which the finding "
        "does not carry. Route to generation or to a human."
    )


# rule_id -> template. Absence is meaningful: it means no deterministic fix
# exists and the finding must go to generation or to a person.
TEMPLATES = {
    "PERF-001": unindexed_foreign_key,
    "DQ-007": stale_statistics,
    "PERF-004": unusable_index,
    "DQ-009": byte_length_semantics,
}


def build(finding: dict) -> dict | None:
    """Return a templated fix, or None when no template applies."""
    template = TEMPLATES.get(finding["rule_id"])
    if not template:
        return None
    fix = template(finding)
    fix["source"] = "template"
    fix["model_id"] = None
    return fix
