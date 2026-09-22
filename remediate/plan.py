"""Turn an assessment into a remediation plan.

The plan is a decision record, not an execution. Nothing here touches a
database: it says, for every finding, what would be done, by whom, whether it
passed the gates, and what is still in the way. Applying it is a separate step
that does not exist yet, deliberately -- a plan you can read is worth more than
an apply you cannot audit.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from . import gates, generate, policy

# Terminal states a finding can reach.
AUTO_APPLY = "AUTO_APPLY"                        # L1, gates clear
READY_TO_APPLY = "READY_TO_APPLY"                # gates clear, approval held
BLOCKED = "BLOCKED"                              # a gate needs something missing
REJECTED = "REJECTED"                            # policy refused the statement
MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"  # a person must author it
ADVICE_DRAFTED = "ADVICE_DRAFTED"                # a recommendation, no statement to gate
NOT_A_FIX = "NOT_A_FIX"                          # L4: a decision, not a statement


def _fix_id(finding: dict) -> str:
    key = f"{finding['rule_id']}|{finding.get('owner')}|{finding.get('object_name')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def plan_finding(
    finding: dict,
    model_mode: str = "off",
    rehearsal_target=None,
    approvals: dict | None = None,
) -> dict:
    approvals = approvals or {}
    fix_id = _fix_id(finding)
    stance = policy.classify(finding["remediation_level"])

    entry = {
        "fix_id": fix_id,
        "rule_id": finding["rule_id"],
        "severity": finding["severity"],
        "remediation_level": finding["remediation_level"],
        "stance": stance,
        "owner": finding.get("owner"),
        "object_name": finding.get("object_name"),
        "title": finding["title"],
        "detail": finding.get("detail"),
        "attempts": 0,
        "sql": None,
        "rollback_sql": None,
        "source": None,
        "model_id": None,
        "fixture_id": None,
        "evidence": None,
        "advice": None,
        "artefact": None,
        "gates": [],
        "status": None,
        "reason": None,
    }

    fix, source = generate.build_fix(finding, model_mode=model_mode)
    entry["source"] = source

    if fix is not None and not fix.get("sql"):
        # Advice with no statement. Most of what a model would say about these
        # findings is not SQL on the source at all -- a DMS setting, a step to
        # run on the target after load, a parameter for provisioning. There is
        # nothing to gate, so it is recorded for a person to act on.
        entry.update({k: fix.get(k) for k in
                      ("advice", "artefact", "evidence", "explain", "fixture_id", "model_id")})
        entry["source"] = fix.get("source", source)
        entry["status"] = ADVICE_DRAFTED
        entry["reason"] = "advice drafted; there is no statement to gate, so a person acts on it"
        return entry

    if fix is None:
        if stance == "never_fix":
            entry["status"] = NOT_A_FIX
            entry["reason"] = (
                "L4 findings are decisions, not statements. This one is answered by the target "
                "decision or by a procurement conversation, not by SQL."
            )
        else:
            entry["status"] = MANUAL_ACTION_REQUIRED
            entry["reason"] = source
        return entry

    entry["attempts"] = 1
    entry.update({
        "sql": fix["sql"],
        "rollback_sql": fix["rollback_sql"],
        "explain": fix.get("explain"),
        "caveat": fix.get("caveat"),
        "source": fix.get("source", source),
        "model_id": fix.get("model_id"),
        "fixture_id": fix.get("fixture_id"),
        "evidence": fix.get("evidence"),
    })

    results = gates.run_all(
        fix, finding, rehearsal_target=rehearsal_target, approved_by=approvals.get(fix_id)
    )
    entry["gates"] = results
    outcome = gates.verdict(results)

    if outcome == "REJECTED":
        entry["status"] = REJECTED
        entry["reason"] = next(g["detail"] for g in results if g["status"] == gates.FAIL)
    elif outcome == "BLOCKED":
        entry["status"] = BLOCKED
        entry["reason"] = next(g["detail"] for g in results if g["status"] == gates.BLOCKED)
    else:
        entry["status"] = AUTO_APPLY if stance == "auto_apply" else READY_TO_APPLY
    return entry


def build(
    assessment: dict,
    model_mode: str | None = None,
    rehearsal_target=None,
    approvals: dict | None = None,
    on_event=None,
    allow_model: bool = False,
) -> dict:
    # allow_model is the older switch, kept so an existing caller does not
    # silently change meaning: True meant "call Bedrock", which is now "live".
    model_mode = model_mode or ("live" if allow_model else "off")
    findings = assessment["findings"]
    entries = []
    for i, finding in enumerate(findings, start=1):
        if on_event:
            on_event({
                "event": "planning",
                "index": i,
                "total": len(findings),
                "rule_id": finding["rule_id"],
                "object_name": finding.get("object_name"),
            })
        entry = plan_finding(
            finding, model_mode=model_mode, rehearsal_target=rehearsal_target, approvals=approvals
        )
        entries.append(entry)
        if on_event:
            on_event({
                "event": "planned",
                "index": i,
                "fix_id": entry["fix_id"],
                "rule_id": entry["rule_id"],
                "status": entry["status"],
            })

    counts: dict[str, int] = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1

    blockers = sorted({
        g["remedy"]
        for e in entries
        for g in e["gates"]
        if g["status"] == gates.BLOCKED and g.get("remedy")
    })

    return {
        "collector_run_id": assessment.get("collector_run_id"),
        "planned_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_mode": model_mode,
        # True only when a model actually generates. Static fixtures are not a
        # model, so "static" reports False here -- deliberately.
        "model_generation_enabled": model_mode == "live",
        "static_outputs_used": sum(1 for e in entries if e["source"] == "static_fixture"),
        "rehearsal_configured": bool(rehearsal_target),
        "totals": counts,
        "fixes_with_sql": sum(1 for e in entries if e["sql"]),
        "what_is_in_the_way": blockers,
        "max_attempts": policy.MAX_ATTEMPTS,
        "entries": entries,
    }
