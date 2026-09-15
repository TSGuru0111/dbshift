"""Create converted code on a real PostgreSQL target.

Everything else in `convert/` is deliberately consequence-free: it compiles into
a transaction and rolls it back, so a mistake costs nothing. **This module is
the exception**, and it is the first thing in this project that writes business
logic to a database. The whole design follows from that.

Four rules, each of which exists because of a specific way this could go wrong:

  1. **Only APPROVED objects.** Not READY_FOR_APPROVAL, not BLOCKED. An object
     that has passed four gates but not the fifth is one a person has not read,
     and the fifth gate is a person reading it.

  2. **The target is re-checked here, not trusted from the plan.** A plan can be
     hours old. The database it was compiled against may not be the one now
     configured, and "it compiled" is not "it compiles here".

  3. **One transaction, all or nothing.** A half-applied schema where a package
     body exists and the function it calls does not is worse than no schema:
     it looks finished. Any failure rolls the whole thing back.

  4. **Every statement is recorded before it runs.** If the process dies
     mid-apply, the record says what had been attempted -- the database rolled
     back, but a person still needs to know what was in flight.

`nothing_applied` is false in this module's output, and it is the only place in
`convert/` where that is true. That flag is what the console and the Phase 10
report read to decide whether to say "nothing was applied".
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import target as target_mod

# Statuses that may be applied. Deliberately a set of one: widening it is a
# decision someone should have to make explicitly in this file.
APPLIABLE = {"APPROVED"}

# Statuses that exist and are *not* appliable, with the reason, so a refusal can
# explain itself rather than saying "not approved".
NOT_APPLIABLE = {
    "READY_FOR_APPROVAL": "it has passed every gate except approval, and approval is a person "
                          "reading the converted code. Approve it first.",
    "BLOCKED": "a gate could not reach a verdict -- usually no PostgreSQL target was configured "
               "when the plan was built, so nothing is proven to compile.",
    "REJECTED": "a gate failed. It would fail on the target too.",
    "MANUAL": "there is no automatic conversion; a person must write this.",
    "MODEL_REQUIRED": "it needs the model tier, which cannot run while Bedrock is blocked.",
    "EXCLUDED_BROKEN_ON_SOURCE": "it does not compile on Oracle today, so there is nothing "
                                 "correct to apply.",
    "ABSORBED_INTO_BODY": "a package specification produces no object of its own; its body "
                          "produces one function per member.",
}

# Statements that must never reach a target from this path, whatever a gate
# said. The policy gate checks the converted body; this is a last check on the
# exact text about to be executed, because the two are not the same thing after
# a plan has been serialised, stored and read back.
FORBIDDEN = [
    (re.compile(r"\bDROP\s+(TABLE|SCHEMA|DATABASE|INDEX|TYPE|SEQUENCE)\b", re.I),
     "drops an object; converting code never requires dropping one"),
    (re.compile(r"\bTRUNCATE\b", re.I), "truncates a table"),
    (re.compile(r"\bDELETE\s+FROM\b", re.I), "deletes rows"),
    (re.compile(r"\bGRANT\b|\bREVOKE\b", re.I), "changes privileges, which is a security decision"),
    (re.compile(r"\bSECURITY\s+DEFINER\b", re.I),
     "runs with the definer's privileges, which escalates what the code may do"),
    (re.compile(r"\bCOPY\b.*\bFROM\s+PROGRAM\b", re.I), "executes a shell command"),
]


class ApplyRefused(RuntimeError):
    """The apply did not start. Nothing was touched."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def selectable(entries: list[dict]) -> dict:
    """Split a plan's entries into what may be applied and what may not."""
    ok, held = [], []
    for e in entries or []:
        status = e.get("status")
        if status in APPLIABLE:
            ok.append(e)
        else:
            held.append({**e, "why_not": NOT_APPLIABLE.get(
                status, f"status {status!r} is not one this path applies")})
    return {"appliable": ok, "held_back": held}


def screen(statements: list[str]) -> list[dict]:
    """Last look at the exact text about to run. Returns the reasons to refuse."""
    problems = []
    for stmt in statements:
        for pattern, why in FORBIDDEN:
            if pattern.search(stmt):
                problems.append({"statement": stmt[:160], "why": why})
    return problems


def preflight(plan: dict, pg_target, *, approved_by: str | None) -> dict:
    """Everything checked before a single statement runs. Changes nothing."""
    checks: list[dict] = []

    def add(name, ok, detail, remedy=None, *, advisory=False):
        """advisory=True records a fact without refusing the apply.

        The distinction matters: a check that refuses must be something that
        makes applying *wrong*, not merely worth knowing.
        """
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "remedy": remedy,
                       "advisory": advisory})

    split = selectable(plan.get("entries", []))
    add("something to apply", bool(split["appliable"]),
        f"{len(split['appliable'])} approved object(s); {len(split['held_back'])} held back",
        "Approve the converted code first. Nothing is applied without a named approver."
        if not split["appliable"] else None)

    # The plan records who approved it. Applying under a different identity than
    # the one that approved is a different act, and the record should show both.
    plan_approver = plan.get("approved_by")
    add("approval on record", bool(plan_approver),
        f"the plan was approved by {plan_approver}" if plan_approver
        else "the plan carries no approver",
        "Re-run the conversion with --approved-by, or approve it in the console.")
    if plan_approver and approved_by and plan_approver != approved_by:
        # Advisory, not a refusal. One person approving and another applying is
        # ordinary separation of duty, and refusing it would push people to
        # approve under the identity that happens to be running the apply --
        # which is exactly the record this is meant to protect.
        add("separate approver and applier", False,
            f"approved by {plan_approver}, applied by {approved_by}. Both are recorded.",
            None, advisory=True)

    # The target is re-checked here rather than trusted from the plan: a plan
    # can be hours old and may have compiled against a different database.
    if pg_target is None:
        add("target configured", False, "no PostgreSQL target",
            "Register one before applying.")
    else:
        info = target_mod.check_target(pg_target)
        add("target reachable", info["ok"], info["detail"],
            "Start the target and register it again." if not info["ok"] else None)
        add("target matches the plan",
            (plan.get("pg_target") or {}).get("detail", "") == info["detail"]
            or not plan.get("pg_target"),
            "the plan compiled against "
            f"{(plan.get('pg_target') or {}).get('detail', 'nothing recorded')}; "
            f"this target is {info['detail']}",
            "Re-run the conversion against this target, so what is applied is what was proven.")

    statements = [s for e in split["appliable"]
                  for s in (e.get("conversion") or {}).get("statements", [])]
    problems = screen(statements)
    add("nothing destructive", not problems,
        "no statement drops, truncates, deletes or changes privileges" if not problems
        else "; ".join(p["why"] for p in problems[:3]),
        "This is a last check on the exact text about to run. A conversion should never "
        "need any of these." if problems else None)

    failures = [c for c in checks if not c["ok"] and not c.get("advisory")]
    return {
        "checks": checks,
        "ready": not failures,
        "refused_because": [c["name"] for c in failures],
        "appliable": split["appliable"],
        "held_back": split["held_back"],
        "statement_count": len(statements),
    }


def execute(plan: dict, pg_target, *, approved_by: str, confirm_target: str,
            on_event=None) -> dict:
    """Create the approved objects. **This writes to the database.**

    `confirm_target` must equal the target's DSN, the same way a deploy asks for
    the account id: it turns "I ran this against the wrong database" into a
    refusal rather than a schema.
    """
    def emit(e):
        if on_event:
            on_event(e)

    if not approved_by:
        raise ApplyRefused("an apply is recorded against a person; no approver was supplied")
    if pg_target is None:
        raise ApplyRefused("no PostgreSQL target is configured")
    if confirm_target != pg_target.dsn:
        raise ApplyRefused(
            f"refusing to apply: the typed target must be {pg_target.dsn!r}, "
            f"and it was {confirm_target!r}")

    pre = preflight(plan, pg_target, approved_by=approved_by)
    if not pre["ready"]:
        raise ApplyRefused("preflight refused: " + ", ".join(pre["refused_because"]))

    record = {
        "started_at_utc": _now(),
        "applied_by": approved_by,
        "approved_by": plan.get("approved_by"),
        "target": pg_target.dsn,
        "estate": plan.get("estate"),
        "collector_run_id": plan.get("collector_run_id"),
        "objects": [],
        "status": "running",
        # The one place in convert/ where this is false.
        "nothing_applied": False,
    }

    conn = pg_target.connect()
    cur = conn.cursor()
    applied, failed = [], []
    # The same search_path the compile gate sets. Without it, code that was
    # *proven* to compile fails here: an unqualified `customer.full_name%TYPE`
    # resolves against the schema on the path and nowhere else. The gate and the
    # apply must run the statement in the same way, or the gate proves nothing
    # about the apply.
    schema = (plan.get("estate") or "").lower()
    try:
        cur.execute("BEGIN")
        if schema:
            cur.execute(f"SET LOCAL search_path TO {schema}, public")
        # The same setting the compile gate uses. Without it PostgreSQL stores a
        # plpgsql body without resolving what it references, so a function
        # calling something that does not exist is created happily and fails at
        # run time instead -- which is the one outcome this whole phase exists
        # to prevent. The gate and the apply must run under identical settings,
        # or the gate is not evidence about the apply.
        cur.execute("SET LOCAL check_function_bodies = on")
        for entry in pre["appliable"]:
            key = f"{entry['owner']}.{entry['object_type']}.{entry['object_name']}"
            stmts = (entry.get("conversion") or {}).get("statements", [])
            # Recorded before it runs: if the process dies here, the record says
            # what was in flight even though the database rolled back.
            record["objects"].append({"object": key, "statements": len(stmts),
                                      "status": "attempting", "at": _now()})
            emit({"event": "applying", "object": key, "statements": len(stmts)})
            try:
                for stmt in stmts:
                    cur.execute(stmt)
                record["objects"][-1]["status"] = "applied"
                applied.append(key)
                emit({"event": "applied", "object": key})
            except Exception as exc:  # noqa: BLE001
                message = str(exc).splitlines()[0][:300]
                record["objects"][-1].update({"status": "failed", "error": message})
                failed.append({"object": key, "error": message})
                emit({"event": "failed", "object": key, "message": message})
                # All or nothing: a half-applied schema looks finished and is not.
                raise

        conn.commit()
        record["status"] = "applied"
        record["applied"] = applied
        emit({"event": "complete", "status": "applied", "objects": len(applied)})
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        record["status"] = "rolled_back"
        record["applied"] = []
        record["failed"] = failed
        record["nothing_applied"] = True   # the rollback made it true again
        record["error"] = str(exc).splitlines()[0][:300]
        emit({"event": "rolled_back",
              "message": f"{record['error']} -- every object was rolled back; the target is "
                         "unchanged"})
    finally:
        record["finished_at_utc"] = _now()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return record
