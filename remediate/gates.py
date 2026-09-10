"""The five gates every generated statement passes before it reaches a database.

    static -> policy -> syntax -> dry run on rehearsal -> approval -> production

Two of these need infrastructure that does not exist yet. They report BLOCKED
with what is missing rather than passing vacuously -- a gate that always passes
is not a gate, and a plan that looks approved because a check was skipped is
worse than no plan.
"""

from __future__ import annotations

from . import policy

PASS, FAIL, BLOCKED = "pass", "fail", "blocked"


def _gate(name, status, detail, remedy=None):
    return {"gate": name, "status": status, "detail": detail, "remedy": remedy}


def static_check(fix: dict, finding: dict) -> dict:
    """Shape only. Is this the kind of thing a fix may be at all?"""
    problems = []
    if not (fix.get("sql") or "").strip():
        problems.append("no statement")
    if policy.REQUIRES_ROLLBACK and not (fix.get("rollback_sql") or "").strip():
        problems.append("no rollback statement")

    # The statement must name the object the finding is about. A fix that
    # touches something else is not a fix for this finding.
    target = (finding.get("object_name") or "").split(".")[0].upper()
    if target and target not in (fix.get("sql") or "").upper():
        problems.append(f"statement does not reference {target}")

    if problems:
        return _gate(
            "static", FAIL, "; ".join(problems),
            "Every fix must be a single statement naming its target and carrying a rollback. "
            "Rejected at generation time, not at apply time.",
        )
    return _gate("static", PASS, "single statement, names its target, carries a rollback")


def policy_check(fix: dict, finding: dict) -> dict:
    """The prohibitions. This is the gate that does not negotiate."""
    violations = policy.check_statement(fix.get("sql", ""))
    violations += [f"rollback {v}" for v in policy.check_statement(fix.get("rollback_sql", ""))
                   if "not one of the shapes" not in v]

    stance = policy.classify(finding["remediation_level"])
    if stance == "never_fix":
        return _gate(
            "policy", FAIL,
            f"{finding['remediation_level']} is never auto-fixed",
            "This is not a SQL problem. It needs a decision, not a statement.",
        )
    if violations:
        return _gate("policy", FAIL, "; ".join(violations),
                     "The AI never drops production objects, deletes data, or alters privileges.")
    return _gate("policy", PASS, f"allowed at {finding['remediation_level']} ({stance})")


def syntax_check(fix: dict, finding: dict) -> dict:
    """Offline validation only.

    A real parse is deliberately NOT attempted against the source. Oracle
    executes DDL at parse time -- DBMS_SQL.PARSE on a CREATE INDEX creates the
    index -- so "just syntax checking" a fix against production would apply it.
    Real parsing belongs on the rehearsal sandbox, one gate later.
    """
    sql = (fix.get("sql") or "").strip()
    problems = []
    if sql.count("(") != sql.count(")"):
        problems.append("unbalanced parentheses")
    if sql.count("'") % 2:
        problems.append("unterminated string literal")
    if sql.count('"') % 2:
        problems.append("unbalanced quoted identifier")
    if problems:
        return _gate("syntax", FAIL, "; ".join(problems))
    return _gate(
        "syntax", PASS,
        "balanced offline check passed; a real parse runs on the rehearsal sandbox because "
        "Oracle executes DDL at parse time",
    )


def dry_run(fix: dict, finding: dict, target=None) -> dict:
    """Apply and roll back on a copy of the estate. Requires that copy to exist."""
    if target is None:
        return _gate(
            "dry_run", BLOCKED,
            "no rehearsal database configured",
            "Restore the estate into a rehearsal schema and set DBSHIFT_REHEARSAL_DSN and "
            "DBSHIFT_REHEARSAL_PASSWORD. Until then no fix can be proven safe, so none may "
            "be applied. See scripts/oracle-source/06_create_rehearsal.sql.",
        )

    from . import rehearsal

    result = rehearsal.dry_run(fix, target)
    if result["ok"]:
        return _gate("dry_run", PASS, result["detail"])

    if result.get("dirty"):
        # The fix applied and its rollback did not. That is a defective fix and a
        # rehearsal copy that now needs restoring -- both must be said plainly.
        return _gate(
            "dry_run", FAIL,
            f"rollback failed after the fix applied -- {result['detail']}",
            "The rollback is wrong, so this fix is rejected. The rehearsal copy has been "
            "changed and should be re-imported before the next run.",
        )

    stage = result["stage"]
    if stage in ("connect", "remap"):
        return _gate("dry_run", BLOCKED, f"{stage}: {result['detail']}")
    return _gate(
        "dry_run", FAIL, f"the fix failed to apply on the rehearsal copy -- {result['detail']}",
        "It would have failed on production too. Rejected here instead.",
    )


def approval(fix: dict, finding: dict, approved_by: str | None = None) -> dict:
    """L1 carries its own approval. Everything else needs a named human."""
    stance = policy.classify(finding["remediation_level"])
    if stance == "auto_apply":
        return _gate("approval", PASS, "L1 is applied without a prompt by policy")
    if approved_by:
        return _gate("approval", PASS, f"approved by {approved_by}")
    return _gate(
        "approval", BLOCKED,
        f"{finding['remediation_level']} requires a named human approver",
        "Approval is recorded against a person, not a session. Nothing applies without one.",
    )


def run_all(fix: dict, finding: dict, rehearsal_target=None, approved_by=None) -> list[dict]:
    """Gates run in order and stop at the first failure -- there is no value in
    syntax-checking a statement that policy has already refused."""
    results = []
    for gate in (static_check, policy_check, syntax_check):
        result = gate(fix, finding)
        results.append(result)
        if result["status"] == FAIL:
            return results
    results.append(dry_run(fix, finding, rehearsal_target))
    results.append(approval(fix, finding, approved_by))
    return results


def verdict(gate_results: list[dict]) -> str:
    if any(g["status"] == FAIL for g in gate_results):
        return "REJECTED"
    if any(g["status"] == BLOCKED for g in gate_results):
        return "BLOCKED"
    return "READY_TO_APPLY"
