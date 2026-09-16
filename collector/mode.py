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
    FULL_LOAD: "Full load (one copy, into an outage window)",
    FULL_LOAD_AND_CDC: "Full load + CDC (ongoing replication, low-downtime cutover)",
}

DESCRIPTION = {
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


def readiness(log_mode: str | None, supplemental_min: str | None) -> dict:
    """Is the source actually configured for CDC, on the evidence collected?

    Returns the facts and a verdict, never a decision. A client may declare CDC
    against a source that is not ready yet -- that is a remediation task with a
    restart window attached, not a reason to refuse the declaration. Phase 2 and
    Phase 5 are where an unmet requirement becomes a blocker.
    """
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


def decide(mode: str | None, *, chosen_by: str | None = None,
           log_mode: str | None = None, supplemental_min: str | None = None,
           declared: bool = True) -> dict:
    """The record written into the discovery manifest.

    `declared` is False when nobody chose and the default applied. That
    distinction matters downstream: a full-load run that a person declared is a
    decision, and one that fell through is an assumption. The report should not
    present them identically.
    """
    resolved = normalize(mode)
    facts = readiness(log_mode, supplemental_min)
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
            + ". This stays a blocker until the source is changed, which needs a "
              "database restart for ARCHIVELOG."
        )
    if not record["cdc_in_scope"] and facts["ready"]:
        record["note"] = (
            "The source is already configured for CDC (ARCHIVELOG plus supplemental "
            "logging), so a low-downtime cutover is available if the outage window "
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
