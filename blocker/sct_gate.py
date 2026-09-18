"""Phase 5 over AWS SCT's action items, split by where the work belongs.

`gate.py` decides HALT or PROCEED from the 50-rule engine's CRITICAL findings.
This does the same from SCT's action items -- SCT is the assessment a client
reads since 2026-09-17 -- and answers the question `gate.py` never had to:

    **what must be fixed in the source before migrating, and what the target
    absorbs when it is built?**

A single "halted" list sends people to fix the wrong thing. On `DBMIG_APP`,
SCT 5200 (external tables) is a source-side decision that stops the load, while
SCT 5581 (index-organized tables) is absorbed by the target's DDL and stops
nothing. Both are `complex`. Only the routing separates them.

**What halts, and what does not.** An action item halts a phase only when
`sct/route.py` says it blocks that phase *and* the phase is in scope for this
migration. Everything else is reported as work. That is deliberately narrow:
SCT raises 14 items on `DBMIG_APP` and two of them block anything. A gate that
halted on all 14 would be ignored, which is worse than one that halts on two.

**CDC readiness does not come from SCT.** SCT never reads redo configuration,
so ARCHIVELOG and supplemental logging -- which the rules engine raised as
`OPS-001` and `OPS-002` -- are taken from the **Connect preflight's** own
evidence via `collector.mode.readiness`. That is a better source than either:
it is measured at connect time from `v$database`, not inferred from a rule.

Nothing here is computed by a model, and nothing here consults one.
"""

from __future__ import annotations

from datetime import datetime, timezone

from collector import mode as migration_mode
from sct import route as sct_route

from . import policy

PROCEED = "PROCEED"
HALT = "HALT"
PROCEED_WITH_WAIVERS = "PROCEED_WITH_WAIVERS"

# The groups a client acts on, in the order the work happens: the source team
# fixes theirs before migration, the target build absorbs its own, decisions
# are made before Phase 6 renders anything, and code is rewritten in Phase 4b.
GROUP_ORDER = [sct_route.SOURCE, sct_route.TARGET,
               sct_route.DECISION, sct_route.HUMAN]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cdc_requirements(facts: dict | None, mode: str) -> dict:
    """Whether the source is configured for the migration that was declared.

    SCT cannot answer this -- it never looks at redo. The evidence is the
    Connect preflight's reading of `v$database`, interpreted by the one function
    that decides what "ready" means (`collector.mode.readiness`), so the gate
    and the mode picker cannot drift apart.
    """
    wants_cdc = migration_mode.wants_cdc(mode)
    facts = facts or {}
    readiness = migration_mode.readiness(
        facts.get("log_mode"), facts.get("supplemental_logging"),
    )

    if not wants_cdc:
        return {
            "applies": False,
            "status": "not_in_scope",
            "readiness": readiness,
            "detail": (
                f"A {mode} migration does not read redo, so ARCHIVELOG and supplemental "
                "logging are not requirements of it. Reported because the source's "
                "configuration is still worth knowing: "
                + (", ".join(readiness["unmet"]) if readiness["unmet"]
                   else "both are already in place.")
            ),
        }

    if readiness["ready"]:
        return {
            "applies": True,
            "status": "clear",
            "readiness": readiness,
            "detail": (f"ARCHIVELOG and supplemental logging are both in place, so change "
                       f"data capture can read redo."),
        }

    return {
        "applies": True,
        "status": "blocked",
        "readiness": readiness,
        "blocks": ["migrate_cdc", "cutover"],
        "detail": (
            "Change data capture was declared but the source is not configured for it: "
            + "; ".join(readiness["unmet"]) + "."
        ),
        "clears_when": (
            "ALTER DATABASE ARCHIVELOG (which needs a restart, so a maintenance window) "
            "and ALTER DATABASE ADD SUPPLEMENTAL LOG DATA. Neither is something this "
            "project applies to a client's source -- both are a DBA's scheduled change."
        ),
        "why_not_from_sct": (
            "AWS SCT assesses schema and stored-code conversion and never reads redo "
            "configuration, so this requirement cannot come from it. The evidence is the "
            "Connect preflight's own reading of v$database."
        ),
    }


def evaluate(
    sct_assessment: dict,
    *,
    facts: dict | None = None,
    migration_mode_record: dict | None = None,
    remediation: dict | None = None,
    waivers: list[dict] | None = None,
) -> dict:
    """Decide whether the run may continue, and say who must fix what."""
    waivers = waivers or []
    issues = sct_route.annotate(sct_assessment.get("issues") or [])

    mode_record = migration_mode_record or migration_mode.decide(None, declared=False)
    mode = mode_record["mode"]
    cdc = migration_mode.wants_cdc(mode)

    # Phases in scope for the migration that was declared. A CDC-only phase is
    # not "clear" on a full-load run -- it is not happening, and the gate says so
    # rather than reporting a pass it did not check.
    downstream = [p for p in policy.DOWNSTREAM
                  if cdc or p not in migration_mode.CDC_ONLY_PHASES]
    out_of_scope = [p for p in policy.DOWNSTREAM if p not in downstream]

    # Which items already have a drafted fix, so a blocker can say a remedy exists.
    drafted = {}
    for entry in (remediation or {}).get("entries", []):
        if entry.get("sql"):
            drafted.setdefault(str(entry.get("issue_code")), []).append(entry)

    accepted, rejected_waivers = {}, []
    for waiver in waivers:
        # Waivers are validated by the same rules the rules-engine gate uses;
        # `rule_id` carries the SCT code, prefixed so a record never confuses an
        # SCT item with a DBShift rule.
        problems = policy.validate_waiver(waiver)
        if problems:
            rejected_waivers.append({**waiver, "rejected_because": problems})
        else:
            accepted[str(waiver["rule_id"])] = waiver

    blockers, waived, out_of_scope_blockers, work = [], [], [], []

    for issue in issues:
        code = str(issue.get("issue_code"))
        blocks = issue.get("blocks") or []
        in_scope_phases = [p for p in blocks if p in downstream]

        entry = {
            "issue_code": code,
            "rule_id": f"SCT-{code}",   # for a waiver, and to keep records distinct
            "title": issue.get("title"),
            "complexity": issue.get("complexity"),
            "occurrences": issue.get("occurrences"),
            "objects": (issue.get("objects") or [])[:40],
            "owner": issue.get("owner"),
            "where": issue["where"],
            "where_label": issue["where_label"],
            "who": issue["who"],
            "who_label": issue["who_label"],
            "why": issue["route_why"],
            "clears_when": issue["clears_when"],
            "sct_recommendation": issue.get("recommendation"),
            "blocks": blocks,
            "blocks_in_scope": in_scope_phases,
            "routed": issue.get("route_mapped", True),
            "fix_drafted": code in drafted,
        }

        if not blocks:
            # Reported as work, not as a halt. Most SCT items are this.
            work.append(entry)
            continue

        if not in_scope_phases:
            entry["not_in_scope_because"] = (
                f"every phase this blocks ({', '.join(blocks)}) is out of scope for a "
                f"{mode} migration"
            )
            out_of_scope_blockers.append(entry)
            continue

        if code in accepted or entry["rule_id"] in accepted:
            entry["waiver"] = accepted.get(code) or accepted[entry["rule_id"]]
            waived.append(entry)
        else:
            blockers.append(entry)

    # CDC readiness, from the preflight rather than from SCT.
    cdc_check = cdc_requirements(facts, mode)
    cdc_blocks = cdc_check.get("blocks") if cdc_check["status"] == "blocked" else []

    by_phase = {}
    for phase in policy.DOWNSTREAM:
        if phase in out_of_scope:
            by_phase[phase] = {
                "status": "not_in_scope",
                "blocked_by": [],
                "why": f"a {mode} migration has no {phase} phase",
            }
            continue
        blocking = [b["rule_id"] for b in blockers if phase in b["blocks_in_scope"]]
        if phase in (cdc_blocks or []):
            blocking = blocking + ["CDC-READINESS"]
        by_phase[phase] = {
            "status": "blocked" if blocking else "clear",
            "blocked_by": blocking,
        }

    if blockers or cdc_blocks:
        verdict = HALT
    elif waived:
        verdict = PROCEED_WITH_WAIVERS
    else:
        verdict = PROCEED

    # Grouped by where the work lands -- the answer to "who must do what".
    groups = []
    for where in GROUP_ORDER:
        items = [e for e in (blockers + waived + out_of_scope_blockers + work)
                 if e["where"] == where]
        groups.append({
            "where": where,
            "label": sct_route.WHERE_LABEL[where],
            "meaning": sct_route.WHERE_MEANING[where],
            "items": items,
            "item_count": len(items),
            "occurrence_count": sum(i.get("occurrences", 0) for i in items),
            "blocking_count": sum(1 for i in items if i in blockers),
            "human_only": sum(1 for i in items if i["who"] == sct_route.PERSON),
        })

    return {
        "phase": "5-gate-sct",
        "evaluated_at_utc": _now(),
        "source_of_findings": "aws-sct",
        "collector_run_id": sct_assessment.get("collector_run_id"),
        "target": (sct_assessment.get("target") or {}).get("id"),
        "migration_mode": mode_record,
        "verdict": verdict,
        "has_critical_blockers": bool(blockers or cdc_blocks),
        "action_items": len(issues),
        "blockers": blockers,
        "waived": waived,
        "out_of_scope_blockers": out_of_scope_blockers,
        "work": work,
        "rejected_waivers": rejected_waivers,
        "cdc_readiness": cdc_check,
        "by_phase": by_phase,
        "groups": groups,
        "unrouted": [e for e in (blockers + work) if not e["routed"]],
        "summary": _summarise(verdict, blockers, waived, cdc_check, groups, mode,
                              out_of_scope_blockers),
    }


def _summarise(verdict, blockers, waived, cdc_check, groups, mode,
               out_of_scope_blockers) -> str:
    """One paragraph a client can read without the JSON."""
    parts = [f"Judged for a {mode} migration, from AWS SCT's action items."]

    counts = ", ".join(
        f"{g['item_count']} {g['label'].lower()}" for g in groups if g["item_count"]
    )
    if counts:
        parts.append(f"{counts}.")

    if verdict == HALT:
        names = [b["issue_code"] for b in blockers]
        if cdc_check["status"] == "blocked":
            names = names + ["CDC readiness"]
        parts.append(
            f"{len(names)} blocker(s) open -- {', '.join(names)} -- so the run halts."
        )
        blocked_phases = sorted({p for b in blockers for p in b["blocks_in_scope"]}
                                | set(cdc_check.get("blocks") or []))
        if blocked_phases:
            parts.append(f"Blocked: {', '.join(blocked_phases)}.")
    elif verdict == PROCEED_WITH_WAIVERS:
        parts.append(
            f"No unwaived blockers. {len(waived)} waived against a named person, "
            "which is recorded and carried into Phase 9."
        )
    else:
        parts.append("No action item blocks a phase in scope, so the run may proceed.")

    # Never let "proceeds" read as "nothing to do".
    remaining = sum(g["item_count"] for g in groups) - len(blockers)
    if remaining > 0:
        parts.append(
            f"{remaining} action item(s) remain as work rather than blockers -- they do "
            "not stop a phase, and they do not go away."
        )

    if out_of_scope_blockers:
        parts.append(
            f"{len(out_of_scope_blockers)} blocker(s) apply only to phases this "
            f"{mode} migration does not run; they are listed, not enforced."
        )

    if cdc_check["status"] == "not_in_scope" and cdc_check["readiness"]["unmet"]:
        parts.append(
            "The source is not configured for change data capture, which this "
            "migration does not need -- but declaring CDC later would make it a blocker."
        )

    return " ".join(parts)
