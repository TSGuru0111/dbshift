"""Phase 4 over AWS SCT's action items, routed by where the fix belongs.

`plan.py` remediates the 50-rule engine's findings. This does the same job for
**SCT's** action items, which is what the client now reads -- and it has to make
a distinction `plan.py` never needed:

    route = source    -> a statement against the client's Oracle. The narrow
                         Oracle allow-list, a rehearsal dry run, a named
                         approver. Exactly as before, because the risk is the
                         same: a production database serving traffic.
    route = target     -> a statement against the PostgreSQL target being built.
                         A *separate* allow-list (`pg_policy`), compiled for
                         real on the Phase 4b PostgreSQL.
    route = decision   -> no statement exists. Advice, recorded, routed to a
                         person. Nothing is drafted, because drafting SQL for
                         "choose an architecture" produces something that looks
                         actionable and is not.
    route = human      -> a person writes it. A model may draft, and the draft
                         is labelled a draft; nothing is ever auto-applied.

**The routing is not decided here and not decided by the model.** It comes from
`sct/route.py`, a reviewed table. This module reads it. That separation is what
lets two runs of the same estate agree about what may be automated -- the
property `docs/phases/phase-02-assess.md` protects for severity, applied to
remediation.

Nothing here executes. A plan is a decision record; `remediate/apply.py` is the
only writer, and it is not wired to this path yet.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sct import route as sct_route

from . import gates, pg_policy, policy

# Terminal states. The first four mirror `plan.py` so a reader moving between
# them is not learning a second vocabulary; the last two are this path's own.
AUTO_APPLY = "AUTO_APPLY"
READY_TO_APPLY = "READY_TO_APPLY"
BLOCKED = "BLOCKED"
REJECTED = "REJECTED"
ADVICE_DRAFTED = "ADVICE_DRAFTED"
DECISION_REQUIRED = "DECISION_REQUIRED"     # no statement can exist
HUMAN_AUTHORED = "HUMAN_AUTHORED_REQUIRED"  # a person writes it


def _fix_id(item: dict) -> str:
    key = f"sct|{item.get('issue_code')}|{item.get('owner')}|{item.get('object_name')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------- target gates
#
# The Oracle gates in `gates.py` are reused unchanged for source-side fixes.
# Target-side fixes need their own policy and their own dry run, so those two
# gates are re-implemented here against `pg_policy` and the Phase 4b PostgreSQL.
# The static and approval gates are shared, because "names its target" and
# "a person said yes" mean the same thing on either engine.

PASS, FAIL, BLOCKED_GATE = "pass", "fail", "blocked"


def _gate(name, status, detail, remedy=None):
    return {"gate": name, "status": status, "detail": detail, "remedy": remedy}


def pg_policy_check(fix: dict) -> dict:
    """The target allow-list. Separate from Oracle's, deliberately."""
    violations = pg_policy.check_statement(fix.get("sql", ""))

    rollback = [v for v in pg_policy.check_statement(fix.get("rollback_sql", ""))
                if "not one of the shapes" not in v]
    if pg_policy.rollback_undoes_own_object(fix.get("sql", ""), fix.get("rollback_sql", "")):
        # A rollback that drops exactly what the fix created is its inverse.
        rollback = [v for v in rollback
                    if "drops a target object" not in v]
    violations += [f"rollback {v}" for v in rollback]

    if violations:
        return _gate(
            "pg_policy", FAIL, "; ".join(violations),
            "Target-side fixes are checked against remediate/pg_policy.py, which is a "
            "separate allow-list from the Oracle one. Widening the Oracle list to admit "
            "target DDL would widen what may run against the source.",
        )
    return _gate("pg_policy", PASS,
                 "allowed against the PostgreSQL target, and the rollback undoes only "
                 "what the fix creates")


def pg_dry_run(fix: dict, pg_target=None) -> dict:
    """Run the statement on the real PostgreSQL and roll it back.

    Uses the same Docker PostgreSQL Phase 4b compiles against, inside a
    transaction that is always rolled back -- so the check is a real parse and a
    real execution against a real engine, not a syntax guess. A blocked gate
    says what is missing rather than passing vacuously.
    """
    if pg_target is None:
        return _gate(
            "pg_dry_run", BLOCKED_GATE,
            "no PostgreSQL target configured",
            "Start the Phase 4b PostgreSQL (scripts/postgres-target/run_pg.ps1) and set "
            "DBSHIFT_PG_DSN and DBSHIFT_PG_PASSWORD. Until then a target fix cannot be "
            "proven to run, so none may be applied.",
        )

    # `pg8000` cursors are not context managers, so this follows the same
    # explicit connect/try/finally shape `convert/target.py` uses. Using `with`
    # here raised a TypeError that read as a failed statement -- a check that
    # reports its own bug as the estate's fault is worse than no check.
    try:
        conn = pg_target.connect()
    except Exception as exc:  # noqa: BLE001 -- the reason belongs in the gate
        return _gate(
            "pg_dry_run", BLOCKED_GATE,
            f"cannot connect to {pg_target.describe()}: {str(exc).strip()[:160]}",
            "The target must be reachable for a fix to be proven. Nothing is applied "
            "on the strength of a check that did not run.",
        )

    try:
        cur = conn.cursor()
        for statement in [s.strip() for s in (fix.get("sql") or "").split(";") if s.strip()]:
            cur.execute(statement)
        # Never committed. The target is being built, but a statement that
        # half-applies during a check is still a mess somebody has to find.
        conn.rollback()
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 -- already failing; do not mask the cause
            pass
        return _gate(
            "pg_dry_run", FAIL,
            f"the statement failed on the PostgreSQL target -- {type(exc).__name__}: "
            f"{str(exc).strip()[:200]}",
            "It would have failed when the target was built. Rejected here instead.",
        )
    finally:
        conn.close()

    return _gate("pg_dry_run", PASS,
                 "executed on the real PostgreSQL target and rolled back")


def pg_static_check(fix: dict, item: dict) -> dict:
    """Shape only, for a target-side statement.

    Deliberately **not** `gates.static_check`. That gate requires the statement
    to name the object the finding is about, which is right on Oracle -- a fix
    touching a different table is not a fix. It is wrong here, and SCT 5639 is
    the proof: the remedy is `CREATE EXTENSION postgres_fdw`, which cannot name
    the database link it exists to replace. Reusing the Oracle gate rejected the
    one item this whole path can fully automate.

    So the object-reference rule is dropped and the two that still mean
    something are kept: there must be a statement, and it must carry a rollback.
    Whether the statement is *appropriate* is `pg_policy_check`'s job, and
    whether it *works* is the dry run's.
    """
    problems = []
    if not (fix.get("sql") or "").strip():
        problems.append("no statement")
    if pg_policy.REQUIRES_ROLLBACK and not (fix.get("rollback_sql") or "").strip():
        problems.append("no rollback statement")

    if problems:
        return _gate(
            "static", FAIL, "; ".join(problems),
            "A target fix must be a statement carrying a rollback. Unlike the Oracle "
            "gate it need not name the object -- an extension fixes a link without "
            "mentioning it.",
        )
    return _gate("static", PASS, "a statement carrying a rollback")


def run_target_gates(fix: dict, item: dict, pg_target=None,
                     approved_by: str | None = None) -> list[dict]:
    """Static -> pg_policy -> pg_dry_run -> approval, stopping at the first failure."""
    results = [pg_static_check(fix, item)]
    if results[-1]["status"] == FAIL:
        return results

    results.append(pg_policy_check(fix))
    if results[-1]["status"] == FAIL:
        return results

    results.append(pg_dry_run(fix, pg_target))

    # A target fix is never auto-applied without a person, even the one
    # automatic route: `CREATE EXTENSION` on a client's database is a change
    # their DBA signs for.
    if approved_by:
        results.append(_gate("approval", PASS, f"approved by {approved_by}"))
    else:
        results.append(_gate(
            "approval", BLOCKED_GATE,
            "a target-side change requires a named approver",
            "Recorded against a person, not a session. Even the automatic route needs one: "
            "an extension on a client's database is their DBA's decision.",
        ))
    return results


# --------------------------------------------------------------------- planning


def plan_item(
    item: dict,
    model_mode: str = "off",
    rehearsal_target=None,
    pg_target=None,
    approvals: dict | None = None,
) -> dict:
    """Plan one SCT action item. Returns a record; touches nothing."""
    approvals = approvals or {}
    fix_id = _fix_id(item)

    # The route is read, never computed here.
    r = sct_route.route(item.get("issue_code"))

    entry = {
        "fix_id": fix_id,
        "source_of_finding": "aws-sct",
        "issue_code": item.get("issue_code"),
        "title": item.get("title"),
        "complexity": item.get("complexity"),
        "occurrences": item.get("occurrences"),
        # Occurrences a person can act on, and the Oracle-generated ones held
        # apart. Carried through rather than recomputed: `sct/parse.py` decides
        # what counts as internal, and a second opinion here could disagree
        # with the assessment screen about the same item.
        "actionable_occurrences": item.get("actionable_occurrences",
                                           item.get("occurrences")),
        "internal_occurrences": item.get("internal_occurrences", 0),
        "internal_objects": item.get("internal_objects") or [],
        "all_internal": item.get("all_internal", False),
        "owner": item.get("owner"),
        "object_name": item.get("object_name"),
        "object_type": item.get("object_type"),
        "objects": item.get("objects") or [],
        # SCT's own words, kept verbatim -- a reviewer compares our draft to them.
        "sct_recommendation": item.get("recommendation"),
        # The routing, carried so the record explains itself without the table.
        "where": r["where"],
        "where_label": sct_route.WHERE_LABEL[r["where"]],
        "who": r["who"],
        "who_label": sct_route.WHO_LABEL[r["who"]],
        "route_why": r["why"],
        "clears_when": r["clears_when"],
        "blocks": r["blocks"],
        # The proposed solution, and what a person must confirm before it is
        # applied. Written in sct/route.py and reviewed there, not generated:
        # a client reading the same plan twice must see the same method.
        "how": r.get("how") or [],
        "verify": r.get("verify") or [],
        "route_mapped": r["mapped"],
        "engine": None,
        "sql": None,
        "rollback_sql": None,
        "advice": None,
        "explain": None,
        "caveat": None,
        "generated_by": None,
        "model_id": None,
        "gates": [],
        "status": None,
        "reason": None,
        "planned_at_utc": _now(),
    }

    # ---- nothing to act on ---------------------------------------------------
    # Every occurrence is an Oracle-generated object (DR$, AQ$ ...). SCT
    # reported it truthfully, but there is no change a person can make, and
    # drafting a fix for Oracle Text's index storage would be work invented
    # from a report rather than found in the estate.
    if item.get("all_internal"):
        entry["status"] = HUMAN_AUTHORED
        entry["advice"] = (
            "Every occurrence is an Oracle-generated object -- "
            + ", ".join((item.get("internal_objects") or [])[:6])
            + " -- which does not migrate. Confirm they are excluded from the "
              "migration scope; there is no change to make to them."
        )
        entry["reason"] = ("nothing to act on: all "
                           f"{item.get('internal_occurrences', 0)} occurrence(s) are "
                           "Oracle-generated objects that do not migrate")
        return entry

    # ---- routes that produce no statement, by definition --------------------
    if r["where"] == sct_route.DECISION:
        entry["status"] = DECISION_REQUIRED
        entry["advice"] = r["why"]
        entry["reason"] = (
            "There is no statement that fixes this. Drafting SQL for an architecture "
            "choice would produce something that looks actionable and is not."
        )
        return entry

    if r["who"] == sct_route.PERSON:
        entry["status"] = HUMAN_AUTHORED
        entry["advice"] = r["why"]
        entry["reason"] = (
            "Routed to a person by sct/route.py. A model draft here would need more "
            "review than writing it, and an unmapped item lands here on purpose."
        )
        return entry

    # ---- a statement may exist: draft it ------------------------------------
    from . import sct_generate

    fix, generated_by = sct_generate.build_fix(item, r, model_mode=model_mode)
    entry["generated_by"] = generated_by
    entry["engine"] = "oracle" if r["where"] == sct_route.SOURCE else "postgresql"

    if fix is None:
        entry["status"] = HUMAN_AUTHORED
        entry["reason"] = (
            f"No template covers SCT {item.get('issue_code')} and the model tier is "
            f"'{model_mode}', so nothing was drafted. The item is a person's."
        )
        return entry

    entry.update({k: fix.get(k) for k in
                  ("sql", "rollback_sql", "advice", "explain", "caveat", "model_id")})

    if not (fix.get("sql") or "").strip():
        # The model concluded there is no safe statement. That is a correct
        # answer and is recorded as advice, not as a failure.
        entry["status"] = ADVICE_DRAFTED
        entry["reason"] = ("advice drafted; no statement was proposed, so a person acts "
                           "on it")
        return entry

    # ---- gate it, on the engine it targets ---------------------------------
    approved_by = approvals.get(fix_id) or approvals.get(str(item.get("issue_code")))

    if r["where"] == sct_route.SOURCE:
        # The client's production Oracle. The existing five gates, unchanged and
        # unwidened -- `remediation_level` is set to L2 because every source-side
        # SCT fix needs a named human; nothing SCT raises is auto-applied there.
        finding = {
            "object_name": item.get("object_name"),
            "remediation_level": "L2",
            "rule_id": f"SCT-{item.get('issue_code')}",
        }
        entry["gates"] = gates.run_all(fix, finding, rehearsal_target, approved_by)
    else:
        entry["gates"] = run_target_gates(fix, item, pg_target, approved_by)

    verdict = gates.verdict(entry["gates"])
    entry["status"] = verdict
    failed = [g for g in entry["gates"] if g["status"] in (FAIL, BLOCKED_GATE)]
    entry["reason"] = failed[0]["detail"] if failed else "every gate passed"
    return entry


def build(
    sct_assessment: dict,
    model_mode: str = "off",
    rehearsal_target=None,
    pg_target=None,
    approvals: dict | None = None,
) -> dict:
    """A remediation plan over an SCT assessment, grouped by where the work lands."""
    issues = sct_assessment.get("issues") or []
    entries = [
        plan_item(i, model_mode=model_mode, rehearsal_target=rehearsal_target,
                  pg_target=pg_target, approvals=approvals)
        for i in issues
    ]

    by_status: dict[str, int] = {}
    for e in entries:
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1

    by_where: dict[str, list] = {}
    for e in entries:
        by_where.setdefault(e["where"], []).append(e)

    return {
        "phase": "4-remediate-sct",
        "planned_at_utc": _now(),
        "source_of_findings": "aws-sct",
        "target": (sct_assessment.get("target") or {}).get("id"),
        "model_mode": model_mode,
        "entries": entries,
        "totals": {
            "items": len(entries),
            "by_status": by_status,
            "by_where": {w: len(v) for w, v in by_where.items()},
            "with_statement": sum(1 for e in entries if e.get("sql")),
            "ready": sum(1 for e in entries if e["status"] == READY_TO_APPLY),
            "blocked": sum(1 for e in entries if e["status"] == BLOCKED),
            "rejected": sum(1 for e in entries if e["status"] == REJECTED),
            "needs_a_person": sum(1 for e in entries if e["status"] in
                                  (HUMAN_AUTHORED, DECISION_REQUIRED)),
        },
        # Which policy governed which engine, so the record says it rather than
        # a reader having to know.
        "policies": {
            "oracle": "remediate/policy.py -- the narrow source allow-list, unchanged",
            "postgresql": pg_policy.describe(),
        },
        # Nothing here has run. Said in the record, not just in a docstring.
        "applied": False,
    }
