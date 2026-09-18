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


def target_has_the_tables(counts: dict | None, include: list[str] | None) -> dict:
    """Every table DMS is about to load exists on the target, under the right name.

    `TargetTablePrepMode` is DO_NOTHING, so DMS creates nothing: the tables must
    already be there, put there by Phase 4c. That makes a missing table a
    guaranteed per-table failure partway through a load -- after the replication
    instance has been billing for however long the earlier tables took.

    **`target_is_empty` does not catch this.** An empty target with the *wrong*
    tables passes it: every table it can see holds zero rows, which is true and
    useless. On 2026-09-18 the prepared DDL on disk built `DBMIG_TELCO`'s eleven
    tables while the console held `DBMIG_APP`, and nothing between Phase 4c and
    the load would have said so.

    A target with extra tables is fine and not reported as a problem -- other
    things live in a database, and DMS is given an explicit table list.

    Names are compared through `validate.crossengine.target_name`, the one
    function that decides what a table is called on the target, so this cannot
    report a table missing that is sitting there under a different spelling.
    """
    if counts is None:
        # Says the consequence, not just the state. A BLOCKED check refuses
        # execution, so a reader needs to know why it matters rather than only
        # that something was not read.
        return _c("target_has_tables", BLOCKED,
                  "target tables not read, so it is unknown whether the tables DMS will "
                  "load exist. TargetTablePrepMode is DO_NOTHING: DMS creates nothing, so "
                  "a missing table fails the load per table partway through, while the "
                  "replication instance bills.",
                  "Pass --pg-dsn so the check can read the target, or run this from the "
                  "console where the target is registered.")
    if not include:
        return _c("target_has_tables", BLOCKED, "no table selection to check against",
                  "The selection comes from dms.mappings.select_tables; without it there "
                  "is nothing to compare the target's tables to.")

    from validate import crossengine

    present = {str(t).lower() for t in counts}
    wanted = {crossengine.target_name(t): t for t in include}
    missing = sorted(src for pg, src in wanted.items() if pg not in present)

    if missing:
        return _c("target_has_tables", FAIL,
                  f"{len(missing)} of {len(wanted)} table(s) DMS will load do not exist on "
                  "the target: " + ", ".join(missing[:8])
                  + (f" and {len(missing) - 8} more" if len(missing) > 8 else ""),
                  "DMS is configured never to create a table (TargetTablePrepMode is "
                  "DO_NOTHING), so it cannot make these and the load will fail per table "
                  "partway through -- while the replication instance bills. Apply Phase 4c's "
                  "table DDL to the target first, and check it was generated for this estate: "
                  "an empty target with another schema's tables passes the empty check.",
                  tables=missing)
    return _c("target_has_tables", PASS,
              f"all {len(wanted)} table(s) DMS will load exist on the target")


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
