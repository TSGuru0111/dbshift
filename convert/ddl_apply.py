"""Create Phase 4c's schema on a real PostgreSQL target.

Everything else about 4c is consequence-free: `ddl_run --compile` executes every
statement inside a transaction and rolls it back, so a mistake costs nothing.
**This module is the exception** -- it is what actually puts tables on a target.

It exists because nothing else did, and the gap was load-bearing. Phase 7's DMS
task runs with `TargetTablePrepMode = DO_NOTHING`: DMS creates nothing, so the
tables must already be there. Until this module, the only ways to get them there
were `DROP_AND_CREATE` (which is what 4c exists to avoid -- every NUMBER becomes
numeric, no constraints, no indexes) or a person running schema.sql by hand
against a target nobody verified.

Four rules, following `convert/apply.py`, each for a specific way this could go
wrong:

  1. **Pre-load statements only, by default.** 4c's own order puts the keys,
     checks and indexes *after* the data load: validating a foreign key row by
     row during a bulk insert is the slowest possible way to do it. So this
     applies the schema, the sequences, the types and the tables -- and stops.
     `--post-load` applies the rest, after Phase 7.

  2. **The target is re-checked here, not trusted from the plan.** A plan can be
     hours old, and "it compiled against the Docker container" is not "it
     compiles against this RDS instance".

  3. **One transaction, all or nothing.** A half-applied schema where eight of
     eleven tables exist is worse than none: DMS would load the eight and fail
     the three, leaving a target that looks half-migrated.

  4. **It refuses a target that already has these tables.** Re-applying would
     fail on the first CREATE, but the useful message is the refusal, not
     PostgreSQL's duplicate-object error partway through.

`nothing_applied` is false in this module's output. It is the only place in
`convert/` besides `apply.py` where that is true.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


# Phase 4c's own ordering, split at the data load. The keys are the plan's.
PRE_LOAD = ("schema", "tables")
POST_LOAD = ("primary_unique", "foreign", "check", "indexes")

# What a statement in this path may do. Narrower than `pg_policy`'s target
# allow-list because 4c produces exactly these shapes, and anything else in the
# plan is a defect in the generator rather than something to execute.
ALLOWED = re.compile(
    r"^\s*(CREATE\s+SCHEMA|CREATE\s+TABLE|CREATE\s+SEQUENCE|CREATE\s+TYPE|"
    r"CREATE(\s+UNIQUE)?\s+INDEX|ALTER\s+TABLE)\b", re.IGNORECASE)

FORBIDDEN = re.compile(
    r"\b(DROP\s+(TABLE|SCHEMA|DATABASE|ROLE)|TRUNCATE|DELETE\s+FROM|"
    r"GRANT|REVOKE|ALTER\s+(ROLE|USER|SYSTEM)|COPY\s+.*FROM\s+PROGRAM)\b",
    re.IGNORECASE)


class ApplyRefused(RuntimeError):
    """The apply will not be attempted, with why."""


def statements_for(plan: dict, *, post_load: bool) -> list[str]:
    """The statements this pass applies, in 4c's order.

    Pre-load is the schema, its sequences and types, then the tables. Post-load
    is everything 4c deliberately defers until the rows are in.
    """
    out: list[str] = []
    if not post_load:
        out.append(plan["schema"])
        owner = (plan.get("estate") or "").lower()
        for s in plan.get("sequences_needed") or []:
            out.append(f"CREATE SEQUENCE IF NOT EXISTS {owner}.{s}")
        for t in plan.get("depends_on_types") or []:
            # A placeholder only where 4b has not supplied the real type. 4b's
            # own apply creates the converted composite types; this keeps a
            # CREATE TABLE from failing when it has not run.
            out.append(f"CREATE TYPE {owner}.{t.lower()} AS (placeholder text)")
        out += plan.get("tables") or []
    else:
        for key in POST_LOAD:
            out += plan.get(key) or []
    return [s for s in out if (s or "").strip()]


def check_statements(statements: list[str]) -> list[str]:
    """Every statement is one of the shapes 4c produces. Returns the violations."""
    bad = []
    for s in statements:
        head = (s or "").strip().splitlines()[0][:80]
        if not ALLOWED.match(s or ""):
            bad.append(f"not an allowed shape: {head}")
        if FORBIDDEN.search(s or ""):
            bad.append(f"forbidden statement: {head}")
    return bad


def existing_tables(target, schema: str) -> set[str]:
    """What the target already has in this schema."""
    conn = target.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,))
        return {r[0].lower() for r in cur.fetchall()}
    finally:
        try:
            conn.close()
        except Exception:                                      # noqa: BLE001
            pass


def apply(plan: dict, target, *, approved_by: str, post_load: bool = False,
          allow_existing: bool = False) -> dict:
    """Create 4c's schema on the target. One transaction, all or nothing.

    `approved_by` is required: this writes to a database, and a schema reaching
    a target is a person's decision even when every gate passed.
    """
    if not approved_by:
        raise ApplyRefused(
            "no approver named. A schema reaching a real target is a person's decision, "
            "and the record has to say whose.")
    if target is None:
        raise ApplyRefused("no PostgreSQL target configured")
    if not (plan.get("compile") or {}).get("ok"):
        raise ApplyRefused(
            "Phase 4c's DDL has not compiled cleanly on a PostgreSQL, so it is generated "
            "but unproven. Run `ddl_run --compile` against a target first: applying "
            "unproven DDL is how a target ends up half-built.")

    statements = statements_for(plan, post_load=post_load)
    if not statements:
        raise ApplyRefused(
            f"nothing to apply for the {'post' if post_load else 'pre'}-load pass")
    violations = check_statements(statements)
    if violations:
        raise ApplyRefused("; ".join(violations[:5]))

    schema = (plan.get("estate") or "").lower()
    # Rule 4: refuse a target that already carries these tables, rather than
    # letting PostgreSQL fail partway with a duplicate-object error.
    if not post_load:
        already = existing_tables(target, schema)
        wanted = {m.group(1).lower() for s in (plan.get("tables") or [])
                  for m in [re.match(r"\s*CREATE\s+TABLE\s+(?:\w+\.)?(\w+)", s,
                                     re.IGNORECASE)] if m}
        clash = sorted(already & wanted)
        if clash and not allow_existing:
            raise ApplyRefused(
                f"the target already has {len(clash)} of these table(s) in schema "
                f"{schema}: {', '.join(clash[:6])}. Applying again would fail on the first "
                "CREATE. Drop them, or pass allow_existing if they are known-empty and "
                "you intend to add the rest.")

    attempted: list[dict] = []
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Post-load statements build indexes over the migrated rows: on 21M rows an
    # ADD PRIMARY KEY runs for minutes, and the compile gate's 10s read timeout
    # aborted it client-side while PostgreSQL was still working. The rollback
    # was correct but the message read as a database error rather than a
    # deadline. An hour is a ceiling, not an expectation.
    conn = target.connect(timeout=3600)
    ok, error = False, None
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        for s in statements:
            # Rule: recorded before it runs, so a process that dies mid-apply
            # leaves a record of what was in flight.
            attempted.append({"statement": s.strip()[:4000], "ran": False})
            cur.execute(s)
            attempted[-1]["ran"] = True
        cur.execute("COMMIT")
        ok = True
    except Exception as exc:                                   # noqa: BLE001
        from appsql import gates as appsql_gates
        error = appsql_gates._pg_message(exc)
        try:
            cur.execute("ROLLBACK")
        except Exception:                                      # noqa: BLE001
            pass
    finally:
        try:
            conn.close()
        except Exception:                                      # noqa: BLE001
            pass

    return {
        "phase": "4c apply",
        "pass": "post_load" if post_load else "pre_load",
        "estate": plan.get("estate"),
        "schema": schema,
        "target": target.describe() if hasattr(target, "describe") else None,
        "approved_by": approved_by,
        "started_at_utc": started,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "statements_attempted": len(attempted),
        "statements_applied": sum(1 for a in attempted if a["ran"]) if ok else 0,
        "applied": attempted if ok else [],
        "in_flight_when_it_failed": None if ok else next(
            (a["statement"][:200] for a in attempted if not a["ran"]), None),
        "ok": ok,
        "error": error,
        # False here and almost nowhere else in convert/. The console and the
        # Phase 10 report read this to decide whether to say nothing was applied.
        "nothing_applied": not ok,
        "what_is_left": (
            "The keys, checks and indexes are deferred until after the data load -- run "
            "this again with post_load once Phase 7 has finished."
            if ok and not post_load else
            "Nothing: the schema is complete." if ok else
            "Nothing was applied; the transaction rolled back."),
    }
