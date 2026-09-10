"""Phase 5 -- the blocker gate.

Nothing downstream runs while a critical finding is open. This is the last
purely deterministic step before anything costs money or touches a target, and
it is deliberately the smallest module in the project: a gate that is hard to
read is a gate nobody trusts.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import policy

PROCEED = "PROCEED"
HALT = "HALT"
PROCEED_WITH_WAIVERS = "PROCEED_WITH_WAIVERS"


def evaluate(
    assessment: dict,
    remediation: dict | None = None,
    waivers: list[dict] | None = None,
) -> dict:
    """Decide whether the run may continue, and say precisely what is in the way."""
    waivers = waivers or []
    findings = assessment.get("findings", [])
    criticals = [f for f in findings if f["severity"] == "CRITICAL"]

    # Index any drafted fixes so a blocker can say whether a remedy already exists.
    fixes = {}
    for entry in (remediation or {}).get("entries", []):
        if entry.get("sql"):
            fixes.setdefault(entry["rule_id"], []).append(entry)

    accepted, rejected_waivers = {}, []
    for waiver in waivers:
        problems = policy.validate_waiver(waiver)
        if problems:
            rejected_waivers.append({**waiver, "rejected_because": problems})
        else:
            accepted[waiver["rule_id"]] = waiver

    blockers, waived = [], []
    for rule_id in sorted({f["rule_id"] for f in criticals}):
        hits = [f for f in criticals if f["rule_id"] == rule_id]
        radius = policy.blast_radius(rule_id)
        entry = {
            "rule_id": rule_id,
            "title": hits[0]["title"],
            "occurrences": len(hits),
            "objects": [h.get("object_name") for h in hits if h.get("object_name")],
            "blocks": radius["phases"],
            "why": radius["why"],
            "clears_when": radius["clears_when"],
            "known_blast_radius": rule_id in policy.BLOCKS,
            "fix_drafted": rule_id in fixes,
        }
        if rule_id in accepted:
            entry["waiver"] = accepted[rule_id]
            waived.append(entry)
        else:
            blockers.append(entry)

    # A phase is blocked if any unwaived blocker names it.
    by_phase = {}
    for phase in policy.DOWNSTREAM:
        blocking = [b["rule_id"] for b in blockers if phase in b["blocks"]]
        by_phase[phase] = {
            "status": "blocked" if blocking else "clear",
            "blocked_by": blocking,
        }

    if blockers:
        verdict = HALT
    elif waived:
        verdict = PROCEED_WITH_WAIVERS
    else:
        verdict = PROCEED

    return {
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "collector_run_id": assessment.get("collector_run_id"),
        "verdict": verdict,
        "has_critical_blockers": bool(blockers),
        "critical_findings": len(criticals),
        "blockers": blockers,
        "waived": waived,
        "rejected_waivers": rejected_waivers,
        "by_phase": by_phase,
        "summary": _summarise(verdict, blockers, waived, by_phase),
    }


def _summarise(verdict, blockers, waived, by_phase) -> str:
    if verdict == PROCEED:
        return "No critical findings. Every downstream phase is clear to run."

    clear = [p for p, v in by_phase.items() if v["status"] == "clear"]
    blocked = [p for p, v in by_phase.items() if v["status"] == "blocked"]

    if verdict == PROCEED_WITH_WAIVERS:
        return (
            f"{len(waived)} critical finding(s) waived by a named approver. The run may proceed, "
            "and the waivers are recorded against the people who granted them."
        )

    parts = [f"{len(blockers)} critical finding(s) open, so the run halts."]
    if blocked:
        parts.append("Blocked: " + ", ".join(blocked) + ".")
    if clear:
        parts.append(
            "Not blocked by these findings: " + ", ".join(clear) +
            " -- but the gate halts the run as a whole until they are resolved or waived."
        )
    return " ".join(parts)
