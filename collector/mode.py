"""The migration mode: full load, or full load plus change data capture.

Asked in Phase 1, because it changes what the rest of the run *means*.

A full-load migration copies the estate once into an outage window. CDC keeps
replicating afterwards, so the cutover can be near-zero-downtime. That single
choice decides whether three of this project's CRITICAL rules are blockers or
irrelevancies:

    OPS-001  NOARCHIVELOG           -- CDC reads redo. A full load reads tables.
    OPS-002  no supplemental logging -- only needed to build an UPDATE from redo.
    DQ-001   no primary key          -- CDC cannot reliably apply updates or
                                       deletes without one. A full load of the
                                       same table is fine.

Until now the mode was a Phase 7 flag (`--migration-type`), which meant the
assessment reported all three as CRITICAL on every run and the gate halted on
them -- sending a client to ask their DBA for a database restart they may not
need. Asking in Phase 1 makes the choice explicit, records who made it and what
the evidence was, and lets every later phase read it from the run.

**The mode never suppresses a finding.** A CDC-only blocker on a full-load run
is still found, still reported, and keeps its original severity on the record.
It is marked not-applicable *with the reason*, which is a different claim from
"clean" and must never be allowed to look like one.
"""

from __future__ import annotations

# Named as DMS names them, so the value passes through to Phase 7 without a
# translation table that could drift.
FULL_LOAD = "full-load"
FULL_LOAD_AND_CDC = "full-load-and-cdc"

MODES = (FULL_LOAD, FULL_LOAD_AND_CDC)

LABEL = {
    FULL_LOAD: "Full load",
    FULL_LOAD_AND_CDC: "Full load + CDC",
}

# One line each. The console shows these on the card; the longer version below
# is a tooltip, because the difference between the two is a decision someone
# makes once and then does not want restated on every screen.
DESCRIPTION = {
    FULL_LOAD: "One copy, applications stopped. Outage lasts the load.",
    FULL_LOAD_AND_CDC: "Copy, then replicate. Outage is minutes.",
}

DETAIL = {
    FULL_LOAD: (
        "The estate is copied once while applications are stopped. Simplest path, "
        "no redo configuration on the source, and the outage lasts as long as the "
        "load. Nothing replicates afterwards."
    ),
    FULL_LOAD_AND_CDC: (
        "The full load runs while applications stay up, then change data capture "
        "replicates everything written since. The cutover waits for replication "
        "to catch up, so the outage is minutes rather than hours. Requires "
        "ARCHIVELOG and supplemental logging on the source."
    ),
}

# The same two descriptions for a MySQL source, whose change capture reads the
# binary log. DETAIL is Oracle's words; a MySQL Connect screen showed "no redo
# configuration" and "Requires ARCHIVELOG" until 2026-09-30.
DETAIL_MYSQL = {
    FULL_LOAD: (
        "The estate is copied once while applications are stopped. Simplest path, "
        "no binary-log configuration needed on the source, and the outage lasts as long "
        "as the load. Nothing replicates afterwards."
    ),
    FULL_LOAD_AND_CDC: (
        "The full load runs while applications stay up, then change data capture "
        "replicates everything written since, read from the binary log. The cutover "
        "waits for replication to catch up, so the outage is minutes rather than hours. "
        "Requires the binary log in ROW format with FULL row images, and REPLICATION "
        "CLIENT for the migration account."
    ),
}


def detail(mode: str, source_engine: str | None = None) -> str:
    return (DETAIL_MYSQL if (source_engine or "").upper() == "MYSQL" else DETAIL)[mode]


# The default. A full load is the weaker claim: it needs nothing from the source
# that is not already true, and choosing it cannot make a migration less correct
# -- only slower to cut over. Defaulting to CDC would silently assert a source
# configuration this tool has not verified.
DEFAULT = FULL_LOAD

# The phases in blocker/policy.py that only exist when CDC is in scope. Kept
# here rather than in the blocker so the mode owns its own consequence.
CDC_ONLY_PHASES = ("migrate_cdc",)

# Rules whose entire rationale is CDC. Each is still evaluated and reported; on
# a full-load run they are recorded as not applicable, with this reason.
#
# Deliberately a short, explicit list rather than anything inferred from the
# rule text. A rule that matches "CDC" in its rationale is not the same as a
# rule that *only* matters for CDC, and guessing that wrong would either hide a
# real blocker or keep a phantom one.
CDC_ONLY_RULES = {
    "OPS-001": (
        "ARCHIVELOG is what change data capture reads. A full-load migration "
        "copies tables directly and never reads redo, so log mode does not "
        "affect it."
    ),
    "OPS-002": (
        "Supplemental logging exists so redo carries enough column data to "
        "rebuild an UPDATE. A full-load migration builds no UPDATEs."
    ),
    "DQ-001": (
        "A primary key is what lets CDC apply an update or delete to the right "
        "target row. A full load copies every row once and needs no such key. "
        "The missing key remains a real data-quality problem on the target and "
        "is still reported -- it is simply not a migration blocker here."
    ),
}


class ModeError(ValueError):
    pass


def normalize(value: str | None) -> str:
    """Accept the DMS spelling, plus the obvious shorthands a person will type."""
    if value is None or not str(value).strip():
        return DEFAULT
    raw = str(value).strip().lower().replace("_", "-").replace(" ", "")
    aliases = {
        "full": FULL_LOAD,
        "fullload": FULL_LOAD,
        "full-load": FULL_LOAD,
        "cdc": FULL_LOAD_AND_CDC,
        "fullloadandcdc": FULL_LOAD_AND_CDC,
        "full-load-and-cdc": FULL_LOAD_AND_CDC,
        "full-loadandcdc": FULL_LOAD_AND_CDC,
        "fullload+cdc": FULL_LOAD_AND_CDC,
    }
    if raw not in aliases:
        raise ModeError(
            f"unknown migration mode {value!r}. Use {FULL_LOAD!r} or {FULL_LOAD_AND_CDC!r}."
        )
    return aliases[raw]


def wants_cdc(mode: str) -> bool:
    return normalize(mode) == FULL_LOAD_AND_CDC


# What "configured for CDC" means, and how to fix it, per source engine. The
# verdict keys (`archivelog`, `supplemental_logging`, `ready`, `unmet`) are the
# same on both so every reader keeps working; only the words differ. Before
# 2026-09-29 a MySQL source was told to run ALTER DATABASE ARCHIVELOG -- the
# facts were mapped onto Oracle's vocabulary and so was the advice.
CDC_WORDING = {
    "ORACLE": {
        "requirements": "ARCHIVELOG and supplemental logging",
        "reads": "redo",
        "evidence": "the Connect preflight's own reading of v$database",
        "clears_when": (
            "ALTER DATABASE ARCHIVELOG (which needs a restart, so a maintenance window) "
            "and ALTER DATABASE ADD SUPPLEMENTAL LOG DATA. Neither is something this "
            "project applies to a client's source -- both are a DBA's scheduled change."
        ),
        "restart_note": "which needs a database restart for ARCHIVELOG",
    },
    "MYSQL": {
        "requirements": "a ROW-format binary log with FULL row images",
        "reads": "the binary log",
        "evidence": ("the Connect preflight's own reading of @@log_bin, "
                     "@@binlog_format and @@binlog_row_image"),
        "clears_when": (
            "log_bin on (a restart if it is off), SET PERSIST binlog_format = 'ROW' and "
            "binlog_row_image = 'FULL', binlog retention (binlog_expire_logs_seconds) "
            "longer than the full load, and REPLICATION CLIENT plus REPLICATION SLAVE "
            "for the DMS account. On an RDS source the first three are parameter-group "
            "settings and retention is mysql.rds_set_configuration('binlog retention "
            "hours', N). None of it is applied by this project -- it is a DBA's change."
        ),
        "restart_note": "which needs a server restart if the binary log is off",
    },
}


def wording(source_engine: str | None) -> dict:
    return CDC_WORDING["MYSQL" if (source_engine or "").upper() == "MYSQL" else "ORACLE"]


def readiness(log_mode: str | None, supplemental_min: str | None, *,
              source_engine: str | None = None, native: dict | None = None) -> dict:
    """Is the source actually configured for CDC, on the evidence collected?

    Returns the facts and a verdict, never a decision. A client may declare CDC
    against a source that is not ready yet -- that is a remediation task with a
    restart window attached, not a reason to refuse the declaration. Phase 2 and
    Phase 5 are where an unmet requirement becomes a blocker.

    On MySQL `log_mode`/`supplemental_min` are the identity probe's mapping of
    the binlog settings onto Oracle's columns; `native` carries the readings
    themselves (binlog_format, binlog_row_image, replication_client) so the
    unmet list names what a MySQL DBA would actually change.
    """
    if (source_engine or "").upper() == "MYSQL":
        return _mysql_readiness(log_mode, supplemental_min, native or {})
    archivelog = (log_mode or "").upper() == "ARCHIVELOG"
    supplemental = (supplemental_min or "").upper() in ("YES", "IMPLICIT")
    unmet = []
    if not archivelog:
        unmet.append(f"log mode is {log_mode or 'unknown'}, not ARCHIVELOG")
    if not supplemental:
        unmet.append(f"minimum supplemental logging is {supplemental_min or 'unknown'}")
    return {
        "log_mode": log_mode,
        "supplemental_log_data_min": supplemental_min,
        "archivelog": archivelog,
        "supplemental_logging": supplemental,
        "ready": archivelog and supplemental,
        "unmet": unmet,
    }


def _mysql_readiness(log_mode, supplemental_min, native: dict) -> dict:
    binlog_on = (log_mode or "").upper() == "ARCHIVELOG"
    fmt = native.get("binlog_format")
    image = native.get("binlog_row_image")
    unmet = []
    if log_mode is None:
        unmet.append("the binary log setting is unknown (not collected on this run)")
    elif not binlog_on:
        unmet.append("log_bin is off (turning it on needs a restart)")
    if fmt is not None or image is not None:
        if str(fmt or "").upper() != "ROW":
            unmet.append(f"binlog_format is {fmt or 'unknown'}, not ROW")
        if str(image or "").upper() != "FULL":
            unmet.append(f"binlog_row_image is {image or 'unknown'}, not FULL")
        row_ok = str(fmt or "").upper() == "ROW" and str(image or "").upper() == "FULL"
    else:
        # Only the mapped column is available: it is YES exactly when ROW + FULL.
        row_ok = (supplemental_min or "").upper() == "YES"
        if binlog_on and not row_ok:
            unmet.append("the binary log is not ROW format with FULL row images")
    if native.get("replication_client") is False:
        unmet.append("the account lacks REPLICATION CLIENT")
    return {
        "source_engine": "MYSQL",
        "log_mode": log_mode,
        "supplemental_log_data_min": supplemental_min,
        "log_bin": binlog_on,
        "binlog_format": fmt,
        "binlog_row_image": image,
        # The shared verdict keys, so the gate and console need no MySQL branch
        # to read them -- only to word them.
        "archivelog": binlog_on,
        "supplemental_logging": row_ok,
        "ready": binlog_on and row_ok and native.get("replication_client") is not False,
        "unmet": unmet,
    }


def decide(mode: str | None, *, chosen_by: str | None = None,
           log_mode: str | None = None, supplemental_min: str | None = None,
           declared: bool = True, source_engine: str | None = None,
           native: dict | None = None) -> dict:
    """The record written into the discovery manifest.

    `declared` is False when nobody chose and the default applied. That
    distinction matters downstream: a full-load run that a person declared is a
    decision, and one that fell through is an assumption. The report should not
    present them identically.
    """
    resolved = normalize(mode)
    facts = readiness(log_mode, supplemental_min,
                      source_engine=source_engine, native=native)
    words = wording(source_engine)
    record = {
        "mode": resolved,
        "label": LABEL[resolved],
        "cdc_in_scope": resolved == FULL_LOAD_AND_CDC,
        "declared": bool(declared),
        "chosen_by": chosen_by,
        "cdc_readiness": facts,
    }
    if record["cdc_in_scope"] and not facts["ready"]:
        record["warning"] = (
            "CDC is in scope but the source is not configured for it: "
            + "; ".join(facts["unmet"])
            + f". This stays a blocker until the source is changed, "
              f"{words['restart_note']}."
        )
    if not record["cdc_in_scope"] and facts["ready"]:
        record["note"] = (
            f"The source is already configured for CDC ({words['requirements']}), "
            "so a low-downtime cutover is available if the outage window "
            "turns out to be a problem."
        )
    return record


def applies(rule_id: str, mode: str) -> tuple[bool, str | None]:
    """Does this rule bear on the declared mode? Returns (applies, why_not)."""
    if wants_cdc(mode):
        return True, None
    reason = CDC_ONLY_RULES.get(rule_id)
    if reason:
        return False, reason
    return True, None
