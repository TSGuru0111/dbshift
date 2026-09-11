"""What a generated fix is allowed to be.

Every rule here is deterministic and none of it consults a model. A model may
*propose* SQL; whether that SQL is allowed to exist, and who may apply it, is
decided entirely by this file.
"""

from __future__ import annotations

import re

# Levels, from docs/02-architecture.md. The level is a property of the assessment
# rule, never of the fix or of anything computed at generation time.
AUTO_APPLY_LEVELS = {"L1"}            # applied without a prompt
APPROVAL_LEVELS = {"L2"}              # generated, but a human must approve
HUMAN_AUTHORED_LEVELS = {"L3"}        # the agent explains; a person writes it
NEVER_FIX_LEVELS = {"L4"}             # not a SQL problem at all

MAX_ATTEMPTS = 3

# Things a generated fix may never do, whatever level it carries and however
# confident anything is about it. Matched against the statement text.
PROHIBITED = [
    (r"\bDROP\s+(TABLE|USER|TABLESPACE|DATABASE|SCHEMA)\b", "drops a production object"),
    (r"\bTRUNCATE\b", "truncates data"),
    (r"\bDELETE\s+FROM\b", "deletes rows"),
    (r"\bGRANT\b", "alters privileges"),
    (r"\bREVOKE\b", "alters privileges"),
    (r"\bALTER\s+USER\b", "alters an account"),
    (r"\bALTER\s+SYSTEM\b", "changes instance configuration"),
    (r"\bSHUTDOWN\b|\bSTARTUP\b", "restarts the database"),
    (r"\bDROP\s+CONSTRAINT\b", "removes an integrity constraint"),
    (r"\bCREATE\s+OR\s+REPLACE\s+(PACKAGE|PROCEDURE|FUNCTION|TRIGGER)\b",
     "rewrites business logic"),
]

# Statements a fix is allowed to be. Anything outside this set needs a human.
ALLOWED_STATEMENT = re.compile(
    r"^\s*(CREATE\s+(UNIQUE\s+)?INDEX|ALTER\s+TABLE|ALTER\s+INDEX|BEGIN\s+DBMS_STATS)",
    re.IGNORECASE,
)

# A fix that cannot be undone is not a fix. Enforced at generation time, not at
# apply time -- by apply time it is too late to ask.
REQUIRES_ROLLBACK = True


def rollback_undoes_own_constraint(fix_sql: str, rollback_sql: str) -> bool:
    """True only when the rollback drops the very constraint the fix adds.

    DROP CONSTRAINT is prohibited everywhere, rollbacks included -- a rollback
    that removes an existing integrity constraint is as dangerous as a fix that
    does. But adding a primary key has exactly one inverse, and refusing it
    meant every correct PK fix was REJECTED for carrying its own undo.

    So the exemption is as narrow as it can be: the fix must ADD CONSTRAINT X
    and the rollback must DROP CONSTRAINT X, same name. Constraint names are
    unique within a schema, so this cannot reach any constraint the fix did
    not create. Any other DROP CONSTRAINT in a rollback is still refused.
    """
    added = re.search(r'\bADD\s+CONSTRAINT\s+("?)([A-Za-z0-9_$#]+)\1', fix_sql or "", re.IGNORECASE)
    dropped = re.search(r'\bDROP\s+CONSTRAINT\s+("?)([A-Za-z0-9_$#]+)\1', rollback_sql or "",
                        re.IGNORECASE)
    return bool(added and dropped and added.group(2).upper() == dropped.group(2).upper())


def classify(level: str) -> str:
    if level in NEVER_FIX_LEVELS:
        return "never_fix"
    if level in HUMAN_AUTHORED_LEVELS:
        return "human_authored"
    if level in APPROVAL_LEVELS:
        return "needs_approval"
    if level in AUTO_APPLY_LEVELS:
        return "auto_apply"
    return "unknown"


def check_statement(sql: str) -> list[str]:
    """Every reason this statement may not be applied. Empty means allowed."""
    violations = []
    flat = " ".join((sql or "").split())
    if not flat:
        return ["statement is empty"]
    for pattern, reason in PROHIBITED:
        if re.search(pattern, flat, re.IGNORECASE):
            violations.append(reason)
    if not ALLOWED_STATEMENT.match(flat):
        violations.append(
            "statement is not one of the shapes a fix may take "
            "(CREATE INDEX, ALTER TABLE, ALTER INDEX, DBMS_STATS)"
        )
    if _statement_count(flat) > 1:
        violations.append("more than one statement")
    return violations


def _statement_count(flat: str) -> int:
    """Count statements, not semicolons.

    A PL/SQL anonymous block legitimately contains semicolons inside it --
    `BEGIN DBMS_STATS.GATHER_TABLE_STATS(...); END;` is one statement with two.
    Counting semicolons rejected every valid DBMS_STATS fix.
    """
    stripped = flat.strip()
    if re.match(r"^BEGIN\b", stripped, re.IGNORECASE):
        # One block. Reject anything after its terminating END;
        end = re.search(r"\bEND\s*;", stripped, re.IGNORECASE)
        trailing = stripped[end.end():].strip() if end else ""
        return 1 if not trailing else 2
    return len([s for s in stripped.split(";") if s.strip()])
