"""What has to be true before a database is declared live on the target.

Each requirement answers one question with `met`, `unmet`, `waived`, or
`not_applicable` -- and `not_applicable` must say *why* it does not apply, or it
is just a skipped check wearing a better name. The certificate is `ready` only
when every requirement is met, waived by a named person, or genuinely does not
apply.

Nothing here changes anything. Building a certificate is free and read-only;
acting on one is a separate, explicit step.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from provision import policy as prov_policy
from provision import records

MET, UNMET, WAIVED, NOT_APPLICABLE = "met", "unmet", "waived", "not_applicable"

ROOT = Path(__file__).resolve().parent.parent
VALIDATION_REPORT = ROOT / "validate" / "output" / "validation_report.json"
VALIDATION_RUNS = ROOT / "validate" / "output" / "runs"
DEPLOYED = ROOT / "provision" / "output" / "deployed.json"

# A validation older than this is stale evidence: the target may have been
# written to since. Cutover is the one place where "it passed yesterday" is not
# good enough.
VALIDATION_MAX_AGE_HOURS = 24


def requirement(rid: str, title: str, status: str, detail: str, remedy: str = "", **evidence) -> dict:
    return {"id": rid, "title": title, "status": status, "detail": detail,
            "remedy": remedy, "evidence": evidence}


def _hours_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600


def target_run(session, plan: dict) -> dict:
    """Which estate the target actually holds. The instance's own tag is the
    authority; deployed.json is the fallback when AWS is not reachable."""
    if session is not None:
        try:
            db = session.client("rds", region_name=prov_policy.REGION).describe_db_instances(
                DBInstanceIdentifier=plan["stack_name"])["DBInstances"][0]
            tags = {t["Key"]: t["Value"] for t in db.get("TagList", [])}
            return {"run_id": tags.get("collector_run_id"), "estate": tags.get("estate"),
                    "status": db["DBInstanceStatus"], "source": "the instance's own tag"}
        except Exception as exc:  # noqa: BLE001
            error = str(exc).splitlines()[0]
    else:
        error = "no AWS session"
    if DEPLOYED.exists():
        dep = json.loads(DEPLOYED.read_text(encoding="utf-8"))
        return {"run_id": dep.get("collector_run_id"), "estate": None, "status": None,
                "source": f"provision/output/deployed.json ({error})"}
    return {"run_id": None, "estate": None, "status": None, "source": error}


# --------------------------------------------------------------------------- the requirements

def records_match_target(recs: dict, run_id: str | None) -> dict:
    ids = {name: r.get("collector_run_id") for name, r in recs.items()}
    wrong = {n: v for n, v in ids.items() if v != run_id}
    if not run_id:
        return requirement("records", "The records describe the estate on the target", UNMET,
                           "cannot tell which collector run the target holds",
                           "Check the instance's collector_run_id tag.")
    if wrong:
        return requirement(
            "records", "The records describe the estate on the target", UNMET,
            "the target holds collector run " + run_id[:8] + ", but "
            + ", ".join(f"{n} describes {str(v)[:8]}" for n, v in wrong.items()),
            f"Re-run the phases for that run: python -m assess.run --run {run_id}, then "
            "python -m remediate.run and python -m blocker.run. Certifying against another "
            "run's findings would certify a database nobody assessed.",
            run_ids=ids)
    return requirement("records", "The records describe the estate on the target", MET,
                       f"assessment, sizing, plan and gate all describe collector run {run_id[:8]}")


def validation_passed(run_id: str | None) -> dict:
    if not VALIDATION_REPORT.exists():
        return requirement("validation", "Phase 8 validated this target", UNMET,
                           "no validation report", "Run python -m validate.run.")
    report = json.loads(VALIDATION_REPORT.read_text(encoding="utf-8"))
    age = _hours_since(report.get("finished_at_utc"))
    if report.get("run_id") != run_id:
        return requirement("validation", "Phase 8 validated this target", UNMET,
                           f"the report covers collector run {str(report.get('run_id'))[:8]}, the target "
                           f"holds {str(run_id)[:8]}", "Run python -m validate.run against this target.")
    if report.get("status") != "validated":
        return requirement("validation", "Phase 8 validated this target", UNMET,
                           f"the last validation ended '{report.get('status')}'"
                           + (f" -- {report.get('reason')}" if report.get("reason") else ""),
                           "Resolve it and validate again. A cutover on an incomplete validation is a "
                           "guess.", mismatches=report.get("mismatches"),
                           not_comparable=report.get("not_comparable"))
    # Belt and braces: 'validated' already implies both are zero, but this is the
    # requirement that a cutover rests on, and a status string is one bug away
    # from lying -- as it did on 2026-09-12, when it reported validated after
    # failing to connect to the target eleven times.
    if report.get("mismatches") or report.get("not_comparable"):
        return requirement("validation", "Phase 8 validated this target", UNMET,
                           f"{report.get('mismatches')} mismatch(es) and "
                           f"{report.get('not_comparable')} uncomparable check(s) in a report that calls "
                           "itself validated", "Investigate the report before trusting its verdict.")
    if age is not None and age > VALIDATION_MAX_AGE_HOURS:
        return requirement("validation", "Phase 8 validated this target", UNMET,
                           f"the validation is {age:.0f} hours old",
                           f"Validate again. Anything written to the target since is unchecked, and "
                           f"evidence older than {VALIDATION_MAX_AGE_HOURS}h is not evidence at cutover.")
    levels = report.get("levels") or []
    return requirement("validation", "Phase 8 validated this target", MET,
                       f"{len(levels)} levels, 0 mismatches, nothing left uncomparable, "
                       f"{age:.1f} hours ago" if age is not None else "validated",
                       levels=[f"{lv['level']} {lv['title']}" for lv in levels])


def gate_allows_cutover(gate: dict, acknowledgement: dict | None) -> dict:
    phase = gate.get("by_phase", {}).get("cutover", {})
    blocked = phase.get("blocked_by") or []
    if not blocked:
        return requirement("gate", "The blocker gate allows cutover", MET,
                           f"cutover is clear; gate verdict {gate.get('verdict')}")
    if acknowledgement:
        return requirement("gate", "The blocker gate allows cutover", WAIVED,
                           f"cutover is blocked by {', '.join(blocked)}, accepted by "
                           f"{acknowledgement['approved_by']}",
                           evidence={"reason": acknowledgement["reason"],
                                     "blockers_accepted": blocked})
    return requirement(
        "gate", "The blocker gate allows cutover", UNMET,
        f"cutover is blocked by {', '.join(blocked)}",
        "Resolve them on the source, waive them in Phase 5, or accept them here with "
        "--approve, which records who accepted what.", blocked_by=blocked)


def cdc_lag(gate: dict) -> dict:
    """The architecture's CDC lag check. It does not apply to a full-outage
    cutover, and saying so beats a green tick nobody earned."""
    cdc_blocked = (gate.get("by_phase", {}).get("migrate_cdc", {}).get("blocked_by")) or []
    if cdc_blocked:
        return requirement(
            "cdc_lag", "Change data capture has caught up", NOT_APPLICABLE,
            f"there is no replication to measure: CDC is blocked by {', '.join(cdc_blocked)}",
            "This is a one-time full load, so the cutover needs an outage window in which the "
            "source takes no writes. Everything written to the source after the export is not on "
            "the target.", blocked_by=cdc_blocked)
    return requirement("cdc_lag", "Change data capture has caught up", UNMET,
                       "CDC is not blocked, but this phase cannot measure replication lag yet",
                       "Measure DMS latency before cutting over.")


def target_ready(run: dict) -> dict:
    status = run.get("status")
    if status is None:
        return requirement("target", "The target is running", UNMET,
                           "could not read the instance state", "Check AWS credentials.")
    if status != "available":
        return requirement("target", "The target is running", UNMET, f"the instance is {status}",
                           "Start it: aws rds start-db-instance --db-instance-identifier "
                           "<stack>. It takes a few minutes and bills from 'available'.")
    return requirement("target", "The target is running", MET, "available")


def outstanding_steps(rem: dict) -> dict:
    steps = []
    for entry in sorted(rem.get("entries", []), key=lambda e: e["rule_id"]):
        artefact = entry.get("artefact") or {}
        for step in artefact.get("steps", []):
            if step.get("applies_to_phase") == "cutover":
                steps.append({"rule_id": entry["rule_id"], "object": entry.get("object_name"),
                              "when": step.get("when"), "sql_on_target": step.get("sql_on_target"),
                              "source": entry.get("source")})
    detail = (f"{len(steps)} step(s) run at cutover: "
              + "; ".join(f"{s['rule_id']} {s['object']}" for s in steps)) if steps else \
        "no target-side steps were set aside for cutover"
    return requirement("steps", "Phase 4's cutover steps are known", MET, detail, steps=steps)


def declared_gaps(recs: dict, run_id: str | None) -> dict:
    """Everything knowingly left unfinished, said out loud on the certificate.

    A cutover certificate that lists only what passed is a sales document.
    """
    gaps = []
    by_rule = {f["rule_id"] for f in recs["assessment"].get("findings", [])}
    # Whatever Phase 4 wrote down as a post-cutover note is exactly this list's
    # business; read them rather than restate them here, or the two drift.
    for entry in sorted(recs["remediation"].get("entries", []), key=lambda e: e["rule_id"]):
        artefact = entry.get("artefact") or {}
        if artefact.get("type") == "post_cutover_note":
            gaps.append(f"{entry['rule_id']}: {artefact['note']}")
    if "RDS-005" in by_rule:
        gaps.append("The database link still points at the source host and will fail when used "
                    "(RDS-005). It needs recreating with a network path.")
    # RDS-016 postdates the run this target was built from, so it does not fire
    # here. It will on any run assessed after 2026-09-12, and the difference is
    # one a user will notice the day they cut over.
    if "RDS-016" in by_rule:
        gaps.append("The SOURCE's Oracle Text index indexes nothing (RDS-016), so searches there "
                    "return no rows today. The target's was rebuilt and works -- worth telling the "
                    "application owner, since the difference will be noticed after cutover.")
    if "SEC-006" in by_rule:
        gaps.append("The source's password profile was deliberately not copied (SEC-006 flagged it "
                    "for never expiring passwords). The target uses DEFAULT.")
    gaps.append("Applications are not repointed by this tool. Connection strings, DNS and any "
                "credentials remain the owner's step.")
    gaps.append("The source is untouched and remains authoritative until those applications move.")
    return requirement("gaps", "What is knowingly unfinished is declared", MET,
                       f"{len(gaps)} item(s) recorded on the certificate", gaps=gaps)


def rollback_plan(plan: dict) -> dict:
    steps = [
        "Nothing was changed on the source, so rolling back is repointing applications at it.",
        "The target can be removed entirely: python -m killswitch --destroy --confirm <account-id>.",
        f"Rebuilding it later is Phase 6 and 7 again from the same records "
        f"({plan.get('stack_name')} took about 23 minutes to create and 10 to migrate).",
    ]
    return requirement("rollback", "There is a way back", MET,
                       "the source is untouched and authoritative", steps=steps)


def build(recs: dict, plan: dict, run: dict, acknowledgement: dict | None) -> list[dict]:
    return [
        records_match_target(recs, run.get("run_id")),
        validation_passed(run.get("run_id")),
        gate_allows_cutover(recs["gate"], acknowledgement),
        cdc_lag(recs["gate"]),
        target_ready(run),
        outstanding_steps(recs["remediation"]),
        declared_gaps(recs, run.get("run_id")),
        rollback_plan(plan),
    ]


def is_ready(reqs: list[dict]) -> bool:
    return all(r["status"] in (MET, WAIVED, NOT_APPLICABLE) for r in reqs)
