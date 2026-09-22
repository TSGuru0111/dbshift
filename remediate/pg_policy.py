"""What a target-side fix is allowed to be. PostgreSQL, not Oracle.

**Why this is a separate file and not a flag on `policy.py`.**
`remediate/policy.py` guards a client's **production Oracle source**. Its
allow-list is deliberately tiny -- `CREATE INDEX`, `ALTER TABLE`, `DBMS_STATS`
-- and every widening of it is a new way to damage a database that is still
serving traffic. An SCT target-side fix needs statements that list would never
permit: `CREATE EXTENSION`, `CREATE TABLE`, `ALTER ... VALIDATE CONSTRAINT`.

Adding those to the Oracle list to make the target work would quietly widen what
may be run against the source, which is the one mistake this project cannot
afford. So the two live apart and never import each other's rules. The gate
chooses which policy applies from the finding's **route** (`sct/route.py`), and
routing is deterministic -- a model cannot move a statement from one policy to
the other.

**The target is also a different risk.** It is being built, not served: at the
point a target-side fix runs there is no traffic on it, and Phase 6 can rebuild
it from scratch. So this list is wider than Oracle's on purpose -- but it is
still a list, and it still refuses anything that destroys data or grants
privileges, because a target with real data in it during Phase 7 is no longer
disposable.
"""

from __future__ import annotations

import re

# Things a target-side fix may never do, whatever routed it here.
#
# The reasoning differs from the Oracle list in one place worth stating: DROP
# TABLE is refused even though Phase 6 could rebuild the target, because by
# Phase 7 the target holds migrated data and a statement that was safe during
# the build is destructive afterwards. A fix must not depend on which phase it
# happens to run in.
PROHIBITED = [
    (r"\bDROP\s+(TABLE|DATABASE|SCHEMA|ROLE|USER|TABLESPACE)\b",
     "drops a target object that may hold migrated data"),
    (r"\bTRUNCATE\b", "truncates data"),
    (r"\bDELETE\s+FROM\b", "deletes rows"),
    (r"\bUPDATE\s+\w", "rewrites data; a schema fix must not change rows"),
    (r"\bGRANT\b", "alters privileges"),
    (r"\bREVOKE\b", "alters privileges"),
    (r"\bALTER\s+(ROLE|USER|SYSTEM)\b", "changes an account or the instance"),
    (r"\bDROP\s+OWNED\b", "drops everything an account owns"),
    # `pg_terminate_backend` and friends are operational, not remediation.
    (r"\bpg_(terminate_backend|cancel_backend|reload_conf)\b",
     "is an operational action, not a schema fix"),
    (r"\bCOPY\b.*\bFROM\s+PROGRAM\b", "executes a shell command"),
    (r"\bCREATE\s+(EXTENSION\s+)?(plpython|plperlu|pltclu)", "installs an untrusted language"),
]

# Statements a target-side fix is allowed to be. Anything outside this set is
# routed to a person rather than run.
#
# `CREATE EXTENSION` earns its place because it is the entire fix for SCT 5639
# (postgres_fdw) and is available on RDS for PostgreSQL. It is bounded by the
# prohibition on untrusted languages above.
ALLOWED_STATEMENT = re.compile(
    r"""^\s*(
        CREATE\s+EXTENSION
      | CREATE\s+(UNIQUE\s+)?INDEX
      | CREATE\s+(OR\s+REPLACE\s+)?(VIEW|FUNCTION|PROCEDURE)
      | CREATE\s+TABLE
      | CREATE\s+SEQUENCE
      | CREATE\s+TYPE
      | ALTER\s+TABLE
      | ALTER\s+INDEX
      | ALTER\s+SEQUENCE
      | COMMENT\s+ON
      | ANALYZE
      | CLUSTER
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

REQUIRES_ROLLBACK = True

# A rollback may drop what its own fix created -- that is its exact inverse --
# but nothing else.
_CREATE_DROP_PAIRS = [
    (r"CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)",
     r"DROP\s+EXTENSION\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)"),
    (r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)",
     r"DROP\s+INDEX\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)"),
    (r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)",
     r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)"),
    (r"CREATE\s+SEQUENCE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)",
     r"DROP\s+SEQUENCE\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)"),
    (r"CREATE\s+TYPE\s+([\w.\"]+)", r"DROP\s+TYPE\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)"),
    (r"ALTER\s+TABLE\s+[\w.\"]+\s+ADD\s+CONSTRAINT\s+([\w.\"]+)",
     r"ALTER\s+TABLE\s+[\w.\"]+\s+DROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)"),
]


def _norm(name: str) -> str:
    return (name or "").strip().strip('"').lower().split(".")[-1]


def rollback_undoes_own_object(fix_sql: str, rollback_sql: str) -> bool:
    """True only when the rollback drops the very object the fix created.

    The same narrow exemption `policy.rollback_undoes_own_constraint` makes for
    Oracle, generalised to the objects a target-side fix may create. Names are
    compared, so a rollback cannot reach an object the fix did not make.
    """
    fix_sql, rollback_sql = fix_sql or "", rollback_sql or ""
    for create_pat, drop_pat in _CREATE_DROP_PAIRS:
        created = re.search(create_pat, fix_sql, re.IGNORECASE)
        dropped = re.search(drop_pat, rollback_sql, re.IGNORECASE)
        if created and dropped and _norm(created.group(1)) == _norm(dropped.group(1)):
            return True
    return False


def _statements(sql: str) -> list[str]:
    """Split on semicolons, ignoring empty fragments.

    Deliberately naive: a fix containing a semicolon inside a string literal or
    a dollar-quoted body is not something this policy should try to parse. Such
    a statement fails the allow-list and routes to a person, which is the right
    outcome -- guessing at SQL lexing to admit a harder statement is how a gate
    stops being trustworthy.
    """
    return [s.strip() for s in re.split(r";\s*", sql or "") if s.strip()]


def check_statement(sql: str) -> list[str]:
    """Every reason this statement may not run against the target. Empty is a pass."""
    problems = []
    flat = re.sub(r"\s+", " ", sql or "")

    for pattern, why in PROHIBITED:
        if re.search(pattern, flat, re.IGNORECASE):
            problems.append(why)

    parts = _statements(flat)
    if not parts:
        problems.append("no statement")
    for part in parts:
        if not ALLOWED_STATEMENT.match(part):
            head = part[:48]
            problems.append(f"not one of the shapes a target fix may be: {head!r}")

    # Dollar-quoted bodies are where arbitrary code hides. A function body is
    # legitimate for a converted procedure, but it is Phase 4b's business --
    # it compiles and gates code separately -- not a remediation statement's.
    if "$$" in flat or re.search(r"\$\w+\$", flat):
        problems.append("contains a dollar-quoted body; converted code belongs to Phase 4b")

    return problems


def describe() -> dict:
    """What this policy permits, for a record or the console."""
    return {
        "applies_to": "the PostgreSQL target",
        "why_separate": (
            "remediate/policy.py guards a client's production Oracle source and is "
            "deliberately narrow. Widening it to admit target DDL would widen what may "
            "run against the source, so the two policies never share rules."
        ),
        "allowed": [
            "CREATE EXTENSION", "CREATE INDEX", "CREATE TABLE", "CREATE SEQUENCE",
            "CREATE TYPE", "CREATE OR REPLACE VIEW/FUNCTION/PROCEDURE",
            "ALTER TABLE", "ALTER INDEX", "ALTER SEQUENCE",
            "COMMENT ON", "ANALYZE", "CLUSTER",
        ],
        "prohibited": [why for _, why in PROHIBITED],
        "requires_rollback": REQUIRES_ROLLBACK,
    }
