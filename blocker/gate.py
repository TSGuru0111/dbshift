"""Phase 5 -- the blocker gate.

Nothing downstream runs while a critical finding is open. This is the last
purely deterministic step before anything costs money or touches a target, and
it is deliberately the smallest module in the project: a gate that is hard to
read is a gate nobody trusts.
"""

from __future__ import annotations

from datetime import datetime, timezone

from collector import mode as migration_mode

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

    # The migration mode declared in Phase 1. On a full-load run the CDC phases
    # are not part of this migration, so a finding that only blocks them blocks
    # nothing. Phase 2 has already marked those findings not-applicable and
    # dropped them out of CRITICAL; this is the other half -- the phases they
    # would have blocked leave the downstream list entirely, so the gate does
    # not report `migrate_cdc` as "clear" when it is simply not happening.
    mode_record = assessment.get("migration_mode") or migration_mode.decide(
        None, declared=False
    )
    mode = mode_record["mode"]
    cdc = migration_mode.wants_cdc(mode)
    downstream = [
        p for p in policy.DOWNSTREAM
        if cdc or p not in migration_mode.CDC_ONLY_PHASES
    ]
    out_of_scope = [p for p in policy.DOWNSTREAM if p not in downstream]

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

    blockers, waived, out_of_scope_blockers = [], [], []
    for rule_id in sorted({f["rule_id"] for f in criticals}):
        hits = [f for f in criticals if f["rule_id"] == rule_id]
        radius = policy.blast_radius(rule_id)
        # A blocker whose entire blast radius is out of scope blocks nothing.
        # It is still listed, with the reason, rather than disappearing.
        in_scope_phases = [p for p in radius["phases"] if p in downstream]
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
            "blocks_in_scope": in_scope_phases,
        }
        if not in_scope_phases:
            entry["not_in_scope_because"] = (
                f"every phase this blocks ({', '.join(radius['phases'])}) is out of "
                f"scope for a {mode} migration"
            )
            out_of_scope_blockers.append(entry)
            continue
        if rule_id in accepted:
            entry["waiver"] = accepted[rule_id]
            waived.append(entry)
        else:
            blockers.append(entry)

    # A phase is blocked if any unwaived blocker names it. Built in
    # policy.DOWNSTREAM order -- the order they execute -- so a reader sees the
    # pipeline, not the scope calculation that produced it.
    by_phase = {}
    for phase in policy.DOWNSTREAM:
        if phase in out_of_scope:
            by_phase[phase] = {
                "status": "not_in_scope",
                "blocked_by": [],
                "why": f"a {mode} migration has no {phase} phase",
            }
            continue
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
        "migration_mode": mode_record,
        "verdict": verdict,
        "has_critical_blockers": bool(blockers),
        "critical_findings": len(criticals),
        "blockers": blockers,
        "waived": waived,
        "out_of_scope_blockers": out_of_scope_blockers,
        "rejected_waivers": rejected_waivers,
        "by_phase": by_phase,
        "summary": _summarise(verdict, blockers, waived, by_phase, mode,
                              out_of_scope_blockers),
    }


def _summarise(verdict, blockers, waived, by_phase, mode, out_of_scope_blockers) -> str:
    # Said on every verdict: a reader must never have to infer which migration
    # this gate was judging.
    scope = f"Judged for a {mode} migration."
    if out_of_scope_blockers:
        ids = ", ".join(b["rule_id"] for b in out_of_scope_blockers)
        scope += (
            f" {len(out_of_scope_blockers)} critical finding(s) ({ids}) block only "
            "phases this migration does not have, so they do not halt the run. They "
            "remain on the record and would block a CDC migration."
        )

    if verdict == PROCEED:
        return scope + " No critical findings in scope. Every phase is clear to run."

    clear = [p for p, v in by_phase.items() if v["status"] == "clear"]
    blocked = [p for p, v in by_phase.items() if v["status"] == "blocked"]

    if verdict == PROCEED_WITH_WAIVERS:
        return scope + (
            f" {len(waived)} critical finding(s) waived by a named approver. The run may "
            "proceed, and the waivers are recorded against the people who granted them."
        )

    parts = [scope, f"{len(blockers)} critical finding(s) open, so the run halts."]
    if blocked:
        parts.append("Blocked: " + ", ".join(blocked) + ".")
    if clear:
        parts.append(
            "Not blocked by these findings: " + ", ".join(clear) +
            " -- but the gate halts the run as a whole until they are resolved or waived."
        )
    return " ".join(parts)
