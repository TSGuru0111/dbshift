"""Apply proven fixes to the rehearsal copy. **This writes to a database.**

Everything else in `remediate/` is consequence-free. `rehearsal.py` applies a fix
and then rolls it back, and requires both to succeed -- so a mistake costs
nothing and leaves nothing behind. **This module is the exception**: it applies a
fix and keeps it.

It writes to the rehearsal copy, never to the source. That is the same rule the
rest of the phase follows, and it is not a limitation to route around:

  - The source is the system of record until cutover. Phase 9 is what changes
    that, deliberately and with a certificate.
  - Two of the four CRITICALs on `DBMIG_APP` are `OPS-001` and `OPS-002`, which
    need `ALTER DATABASE` and a restart. That is a maintenance window a DBA
    schedules, not something an agent does while a client watches.
  - A fix proven on a copy and applied to that same copy is a claim this project
    can actually stand behind. "We corrected your production database" is not,
    on the evidence available here.

Five rules, each for a specific way this could go wrong:

  1. **Only fixes that passed every gate.** `AUTO_APPLY` and `READY_TO_APPLY`.
     `BLOCKED` means a gate said no, and the commonest reason is that a person
     has not approved it -- which is the gate working, not an obstacle.

  2. **The dry run must have happened, and passed.** A fix whose `dry_run` gate
     did not run is not proven, whatever its status says. This is checked against
     the gate record rather than inferred from the status, because a status can
     be widened by a later policy change and a gate result cannot.

  3. **The target is re-checked here, not trusted from the plan.** A plan can be
     hours old, and the rehearsal copy it was proven against may not be the
     database now configured.

  4. **Each fix commits on its own, in order.** This differs from
     `convert/apply.py`, deliberately. Converted code is one schema and a
     half-applied schema is worse than none. Remediation fixes are independent
     -- an index here, statistics there -- and a failure on the fourth is no
     reason to undo the three that worked. Each is recorded with its outcome.

  5. **The statement is remapped onto the rehearsal schema and re-checked.**
     `rehearsal.remap()` already refuses to produce SQL that still names the
     source schema. That check runs again here, on the exact text about to
     execute, because this is the last place it can be caught.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import rehearsal as rehearsal_mod

# Statuses that may be applied, and why each is allowed.
APPLIABLE = {
    "AUTO_APPLY": "L1: deterministic template, every gate green, no approval needed",
    "READY_TO_APPLY": "every gate green including a named approver",
}

# Statuses that exist and are not appliable, with the reason, so a refusal can
# explain itself rather than saying "not appliable".
NOT_APPLIABLE = {
    "BLOCKED": "a gate refused it -- most often approval, which is a person reading it",
    "REJECTED": "a gate failed outright; applying it would apply something known broken",
    "ADVICE_DRAFTED": "there is no statement to run -- it is a recommendation for a person",
    "MANUAL_ACTION_REQUIRED": "it was routed to a human by policy, and no SQL was generated",
    "NOT_A_FIX": "it is a decision, not a statement",
}

# The same screen convert/apply.py runs, for the same reason: a last look at the
# exact text about to execute. A remediation fix should never contain any of it.
FORBIDDEN = [
    (re.compile(r"\bDROP\s+(TABLE|SCHEMA|DATABASE|USER|TABLESPACE)\b", re.I),
     "drops a table, schema, user or tablespace"),
    (re.compile(r"\bTRUNCATE\b", re.I), "truncates"),
    (re.compile(r"\bDELETE\s+FROM\b", re.I), "deletes rows"),
    (re.compile(r"\bGRANT\b|\bREVOKE\b", re.I), "changes privileges"),
    (re.compile(r"\bSHUTDOWN\b|\bALTER\s+DATABASE\b", re.I),
     "alters or stops the database itself"),
]


class ApplyRefused(RuntimeError):
    """The apply will not proceed, with the reason."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def screen(sql: str) -> list[dict]:
    """Forbidden constructs in the exact text about to run."""
    return [{"why": why, "statement": sql[:160]}
            for pattern, why in FORBIDDEN if pattern.search(sql or "")]


def _gate(entry: dict, name: str) -> dict | None:
    """One gate's result by name. The key is `gate`, not `name`."""
    for g in entry.get("gates") or []:
        if g.get("gate") == name:
            return g
    return None


def _sql(entry: dict, key: str = "sql") -> str | None:
    """The statement. Plan entries carry it top-level; a `fix` sub-dict is
    tolerated so a caller holding an older shape is not silently skipped."""
    return entry.get(key) or (entry.get("fix") or {}).get(key)


def selectable(entries: list[dict]) -> dict:
    """Split the plan into what may be applied and what may not, with reasons."""
    appliable, held_back = [], []
    for e in entries:
        status = e.get("status")
        sql = _sql(e)
        label = f"{e.get('rule_id')} {e.get('owner', '')}.{e.get('object_name', '')}".strip()

        if status not in APPLIABLE:
            held_back.append({"entry": label, "status": status,
                              "why": NOT_APPLIABLE.get(status, "status is not appliable")})
            continue
        if not sql:
            held_back.append({"entry": label, "status": status,
                              "why": "the status says appliable but the entry carries no SQL"})
            continue
        # Rule 2: proven, not merely permitted. A status can be widened by a
        # policy change; a gate result is a fact about this run.
        dry = _gate(e, "dry_run")
        if not dry or dry.get("status") != "pass":
            held_back.append({
                "entry": label, "status": status,
                "why": "the dry run did not pass on the rehearsal copy, so the fix is not proven"
                       + (f" -- {dry.get('detail')}" if dry and dry.get("detail") else "")})
            continue
        appliable.append(e)
    return {"appliable": appliable, "held_back": held_back}


def preflight(plan: dict, target, *, applied_by: str | None) -> dict:
    """Everything checked before a single statement runs. Changes nothing."""
    checks: list[dict] = []

    def add(name, ok, detail, remedy=None, *, advisory=False):
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "remedy": remedy, "advisory": advisory})

    split = selectable(plan.get("entries", []))
    add("something to apply", bool(split["appliable"]),
        f"{len(split['appliable'])} fix(es) proven and appliable; "
        f"{len(split['held_back'])} held back",
        "Nothing has passed every gate. Approve a fix, or supply a rehearsal target "
        "so the dry run can prove one." if not split["appliable"] else None)

    add("a person is named", bool(applied_by),
        f"applying as {applied_by}" if applied_by else "no applier named",
        "Applying is recorded against a person. Pass --applied-by.")

    if target is None:
        add("rehearsal target configured", False, "no rehearsal target",
            "Set DBSHIFT_REHEARSAL_DSN and DBSHIFT_REHEARSAL_PASSWORD.")
    else:
        # Rule 3: re-checked here, not trusted from the plan.
        info = check_target(target)
        add("rehearsal target reachable", info["ok"], info["detail"],
            "Start the rehearsal database and try again." if not info["ok"] else None)
        # The one refusal that is about safety rather than readiness. If these
        # are the same schema, every "rehearsal" write lands on the source.
        same = target.schema.upper() == target.source_schema.upper()
        add("rehearsal is not the source", not same,
            f"rehearsal {target.schema}, source {target.source_schema}",
            "These must differ. Applying with them equal would write to the source."
            if same else None)

    # Rule 5: the exact text, after remapping.
    #
    # Skipped when the schemas are equal: remap() raises rather than returning,
    # and correctly so, but preflight must REPORT a refusal rather than raise one
    # -- a caller checking `ready` should never have to catch an exception to
    # learn the answer is no.
    problems, remapped = [], []
    if target is not None and target.schema.upper() != target.source_schema.upper():
        for e in split["appliable"]:
            sql = _sql(e)
            try:
                out = rehearsal_mod.remap(sql, target.source_schema, target.schema)
            except ValueError as exc:
                problems.append({"entry": e.get("rule_id"), "why": str(exc)})
                continue
            found = screen(out)
            if found:
                problems.append({"entry": e.get("rule_id"), "why": found[0]["why"]})
            remapped.append((e, out))
    add("nothing destructive, after remapping", not problems,
        "no statement drops, truncates, deletes or changes privileges" if not problems
        else "; ".join(p["why"] for p in problems[:3]),
        "A remediation fix should never need any of these." if problems else None)

    failures = [c for c in checks if not c["ok"] and not c.get("advisory")]
    return {
        "checks": checks,
        "ready": not failures,
        "refused_because": [c["name"] for c in failures],
        "appliable": split["appliable"],
        "held_back": split["held_back"],
        "remapped": remapped,
    }


def check_target(target) -> dict:
    """Connect, confirm the schema exists, and change nothing."""
    try:
        import oracledb
    except ImportError:  # pragma: no cover
        return {"ok": False, "detail": "oracledb is not installed"}
    try:
        conn = oracledb.connect(user=target.user, password=target.password, dsn=target.dsn)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc).splitlines()[0]}
    try:
        cur = conn.cursor()
        who = cur.execute("select user from dual").fetchone()[0]
        n = cur.execute("select count(*) from all_tables where owner = :o",
                        o=target.schema.upper()).fetchone()[0]
        return {"ok": True,
                "detail": f"{who}@{target.dsn}, {n} tables in {target.schema.upper()}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc).splitlines()[0]}
    finally:
        conn.close()


def execute(plan: dict, target, *, applied_by: str, confirm_schema: str,
            on_event=None) -> dict:
    """Apply the proven fixes to the rehearsal copy. **This writes.**

    `confirm_schema` must equal the target schema. It is not ceremony: it is the
    one thing standing between "apply to the rehearsal copy" and "apply to
    whatever DBSHIFT_REHEARSAL_SCHEMA happens to say right now".
    """
    def emit(evt):
        if on_event:
            on_event(evt)

    pre = preflight(plan, target, applied_by=applied_by)
    if not pre["ready"]:
        raise ApplyRefused("refused: " + ", ".join(pre["refused_because"]))
    if (confirm_schema or "").upper() != target.schema.upper():
        raise ApplyRefused(
            f"confirmation {confirm_schema!r} does not match the rehearsal schema "
            f"{target.schema!r}. Nothing was applied.")

    import oracledb
    started = _now()
    results: list[dict] = []
    conn = oracledb.connect(user=target.user, password=target.password, dsn=target.dsn)
    try:
        cur = conn.cursor()
        for entry, sql in pre["remapped"]:
            label = f"{entry.get('rule_id')} {entry.get('owner','')}.{entry.get('object_name','')}".strip()
            # Rule 4: recorded before it runs, so an interrupted apply still
            # leaves a trace of what was in flight.
            record = {"rule_id": entry.get("rule_id"), "entry": label, "sql": sql,
                      "rollback_sql": rehearsal_mod.remap(
                          _sql(entry, "rollback_sql") or "",
                          target.source_schema, target.schema),
                      "started_utc": _now(), "applied": False}
            emit({"event": "apply", "entry": label})
            try:
                cur.execute(sql)
                conn.commit()
                record.update(applied=True, finished_utc=_now())
                emit({"event": "applied", "entry": label})
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                record.update(applied=False, finished_utc=_now(),
                              error=str(exc).splitlines()[0])
                emit({"event": "failed", "entry": label, "error": record["error"]})
            results.append(record)
    finally:
        conn.close()

    applied = [r for r in results if r["applied"]]
    failed = [r for r in results if not r["applied"]]
    return {
        "started_utc": started,
        "finished_utc": _now(),
        "applied_by": applied_by,
        "target": {"dsn": target.dsn, "schema": target.schema,
                   "source_schema": target.source_schema},
        "collector_run_id": plan.get("collector_run_id"),
        "results": results,
        "applied_count": len(applied),
        "failed_count": len(failed),
        "held_back": pre["held_back"],
        # The flag the console and the Phase 10 report read. False here and
        # nowhere else in remediate/.
        "nothing_applied": not applied,
        "where": "the rehearsal copy. The source was not modified.",
    }
