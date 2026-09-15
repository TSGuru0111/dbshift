"""What must be true before DMS is allowed to run, and what it would cost.

Read-only. Creates nothing, bills nothing.

The checks fall into three groups:

  can it run at all   records agree, the gate allows it, both endpoints reachable
  can CDC run         archivelog, supplemental logging, primary keys
  is it safe          a non-empty target, a replication instance already running

The CDC group is the one that earns its place. DMS will happily *start* a
full-load-and-cdc task against a source in NOARCHIVELOG: the full load succeeds,
the CDC phase then fails, and the failure arrives after the load has finished
and the window has been spent. Checking first turns a wasted outage into a
refusal with a reason.
"""

from __future__ import annotations

from . import policy

PASS, WARN, FAIL, BLOCKED = "pass", "warn", "fail", "blocked"

# Findings that make change data capture impossible or unsafe, and what each
# one actually does to a CDC task. These are the same rule ids Phase 2 raises,
# so the reasoning is traceable to a finding rather than restated here.
CDC_BLOCKING_RULES = {
    "OPS-001": ("The source is in NOARCHIVELOG. DMS reads redo through LogMiner or binary reader, "
                "and neither can read logs that are not archived. The full load would succeed and "
                "the CDC phase would then fail, after the window was spent."),
    "OPS-002": ("Supplemental logging is off. Without it, redo records an UPDATE without the key "
                "columns needed to find the row on the target, so changes cannot be applied "
                "reliably -- and DMS does not always raise an error when it cannot."),
    "DQ-001": ("A table with no primary key or unique index cannot be identified row by row, so "
               "UPDATE and DELETE cannot be applied to it during CDC. The full load copies it; "
               "every later change to it is silently lost."),
}


def _c(name, status, detail, remedy=None, **extra):
    return {"name": name, "status": status, "detail": detail, "remedy": remedy, **extra}


def records_consistent(records: dict) -> dict:
    ids = {k: (v or {}).get("collector_run_id") for k, v in records.items() if v}
    distinct = {v for v in ids.values() if v}
    if len(distinct) > 1:
        return _c("records_consistent", FAIL,
                  "records come from different collector runs: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(ids.items())),
                  "Re-run the phases whose run id differs so every record describes the same estate.")
    if not distinct:
        return _c("records_consistent", FAIL, "no collector run id in any record",
                  "Run discovery first.")
    return _c("records_consistent", PASS,
              f"every record comes from collector run {distinct.pop()}")


def gate_allows(gate: dict, migration_type: str) -> dict:
    """Phase 5 decides which blockers stand in front of moving data.

    The gate distinguishes a full load from CDC, because the things that stop
    replication are not the things that stop a bulk copy.
    """
    phase = "migrate_cdc" if migration_type != policy.FULL_LOAD else "migrate_full_load"
    by_phase = (gate or {}).get("by_phase", {}).get(phase, {})
    status = by_phase.get("status")
    blocked_by = by_phase.get("blocked_by") or []
    if status == "blocked":
        return _c("gate_allows", FAIL,
                  f"Phase 5 blocks {phase.replace('_', ' ')}: " + ", ".join(blocked_by),
                  "Resolve those findings, or waive them by name in Phase 5. A waiver is recorded "
                  "against a person; it is not a setting.",
                  blocked_by=blocked_by, phase=phase)
    return _c("gate_allows", PASS, f"Phase 5 allows {phase.replace('_', ' ')}",
              phase=phase)


def cdc_possible(facts: dict, findings: list[dict], migration_type: str) -> list[dict]:
    """Whether change data capture can actually run on this source.

    Returns one check per reason it cannot, or a single PASS. Skipped entirely
    for a full-load-only task, where none of it applies.
    """
    if migration_type == policy.FULL_LOAD:
        return [_c("cdc_requirements", PASS,
                   "full load only -- no change data capture, so archivelog, supplemental logging "
                   "and primary keys are not required for the migration to succeed")]

    checks: list[dict] = []
    rules = {f["rule_id"] for f in findings or []}

    # The two source settings, read from discovery rather than inferred from a
    # finding, because the finding could have been waived while the setting is
    # still what it is. A waiver is a decision to accept a risk, not a change
    # to the database.
    log_mode = (facts or {}).get("log_mode")
    if log_mode and log_mode.upper() != "ARCHIVELOG":
        checks.append(_c("cdc_archivelog", FAIL,
                         f"the source is in {log_mode}; DMS cannot read redo that is not archived",
                         "ALTER DATABASE ARCHIVELOG on the source, which needs a restart. Until "
                         "then only a full load is possible, and the cutover needs an outage "
                         "window that covers it.",
                         rule_id="OPS-001"))
    else:
        checks.append(_c("cdc_archivelog", PASS, f"source log mode {log_mode or 'ARCHIVELOG'}"))

    suppl = (facts or {}).get("supplemental_logging")
    if suppl and str(suppl).upper() in ("NO", "NONE", "FALSE"):
        checks.append(_c("cdc_supplemental_logging", FAIL,
                         "minimum supplemental logging is off, so redo does not carry the key "
                         "columns DMS needs to apply an UPDATE to the right target row",
                         "ALTER DATABASE ADD SUPPLEMENTAL LOG DATA. It is online and cheap, but it "
                         "increases redo volume, so the DBA should size for it.",
                         rule_id="OPS-002"))
    else:
        checks.append(_c("cdc_supplemental_logging", PASS,
                         f"supplemental logging {suppl or 'enabled'}"))

    # Keys are per table, so this is a warning about specific tables rather than
    # a blanket failure: the rest of the estate can still replicate.
    if "DQ-001" in rules:
        affected = sorted({f.get("object_name") for f in findings
                           if f["rule_id"] == "DQ-001" and f.get("object_name")})
        checks.append(_c("cdc_primary_keys", FAIL,
                         f"{len(affected)} table(s) have no primary key or unique index: "
                         + ", ".join(affected[:6]) + ("..." if len(affected) > 6 else ""),
                         "Add a key, or exclude those tables from the CDC task and move them by "
                         "full load during the outage. Replicating them would lose every UPDATE "
                         "and DELETE without raising an error.",
                         rule_id="DQ-001", tables=affected))
    else:
        checks.append(_c("cdc_primary_keys", PASS, "every table has a key DMS can identify rows by"))

    return checks


def target_is_empty(counts: dict | None) -> dict:
    """A non-empty target is a decision, not a detail.

    `TargetTablePrepMode` is DO_NOTHING, so DMS would insert *alongside* existing
    rows rather than replacing them -- duplicating data instead of overwriting
    it, which is worse because it looks like it worked.
    """
    if counts is None:
        return _c("target_empty", BLOCKED, "target row counts not read",
                  "Connect to the target so the check can run.")
    non_empty = {t: n for t, n in counts.items() if n}
    if non_empty:
        return _c("target_empty", FAIL,
                  f"{len(non_empty)} target table(s) already hold rows: "
                  + ", ".join(f"{t} ({n:,})" for t, n in sorted(non_empty.items())[:5]),
                  "DMS is configured never to truncate a table it did not create, so it would add "
                  "rows alongside these and the result would look successful while being wrong. "
                  "Empty the target, or drop and recreate the schema.",
                  tables=non_empty)
    return _c("target_empty", PASS, f"every one of the {len(counts)} target table(s) is empty")


def no_instance_running(instances: list[dict], name: str) -> dict:
    """An existing replication instance is already billing."""
    mine = [i for i in instances or [] if i.get("ReplicationInstanceIdentifier") == name]
    if mine:
        i = mine[0]
        return _c("instance_absent", WARN,
                  f"{name} already exists ({i.get('ReplicationInstanceStatus')}) and is billing",
                  "Reuse it, or delete it with the kill switch. A replication instance bills by "
                  "the hour whether or not a task is running.",
                  status_now=i.get("ReplicationInstanceStatus"))
    return _c("instance_absent", PASS, f"{name} does not exist; nothing is billing yet")


def summarise(checks: list[dict]) -> dict:
    failures = [c for c in checks if c["status"] == FAIL]
    warnings = [c for c in checks if c["status"] == WARN]
    blocked = [c for c in checks if c["status"] == BLOCKED]
    return {
        "checks": checks,
        "failures": len(failures),
        "warnings": len(warnings),
        "blocked": len(blocked),
        "ready": not failures and not blocked,
        "refused_because": [c["name"] for c in failures + blocked],
    }
