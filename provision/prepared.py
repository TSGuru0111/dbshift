"""What Phases 4b, 4c and 4d prepared, and whether it is ready for the target.

Phase 6 creates an empty instance. Everything that makes it *this estate's*
target -- the tables, the stored code, the application's SQL -- was prepared by
three earlier phases and, until now, was invisible here. A person provisioned a
bare database and the next phase improvised against it.

That was a real gap rather than a cosmetic one. The order those artefacts must
be applied in is not obvious and getting it wrong fails:

    4c tables            first, or the stored code has nothing to compile
                         against and the data load has nowhere to go
    4b types             before the tables that declare a column of one
    4b routines          after the tables their bodies reference
    [ THE DATA LOAD ]    Phase 7
    4c keys and indexes  after the load, never during it
    4d application SQL   not applied to the database at all -- it is a change
                         to the application's own source, and it is listed here
                         because a target nobody changed the application for is
                         a target the application cannot talk to

**This module reads and reports. It applies nothing.** Every artefact is a
proposal each phase already gated and left for a person to approve, and
provisioning does not turn an approval nobody gave into one.

**Absent is not zero.** A phase that never ran is reported as not run, never as
"nothing to do" -- the distinction Phase 3 already protects for stored-code
evidence. A target provisioned without 4c's schema is a legitimate choice (DMS
will improvise one); it is not the same choice as a target whose schema was
prepared and reviewed, and the record says which.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Optional by design: a provision must not fail because a preparation phase has
# not run. The required records are `provision/records.py`'s four.
PATHS = {
    "schema_ddl": ROOT / "convert" / "output" / "schema_ddl.json",
    "stored_code": ROOT / "convert" / "output" / "conversion_plan.json",
    "application_sql": ROOT / "appsql" / "output" / "appsql_plan.json",
}

# The order the artefacts must reach the target in, with the reason. Data, not
# code, so a reader can check it against `docs/phases/phase-04c-schema-ddl.md`
# without reading Python.
ORDER = [
    ("schema_ddl.tables", "4c", "Tables, columns only",
     "The data load needs somewhere to go and the stored code needs something to "
     "compile against. Nothing else can be first."),
    ("stored_code.types", "4b", "User-defined types",
     "A table declaring a column of type TY_ADDRESS fails if the type does not "
     "exist, so the types precede the tables that name them."),
    ("stored_code.routines", "4b", "Functions, procedures, triggers",
     "Their bodies reference the tables, so the tables exist first. Each was "
     "already compiled on a real PostgreSQL and rolled back."),
    ("__data_load__", "7", "THE DATA LOAD",
     "Phase 7. Everything above must exist; everything below must not, yet."),
    ("schema_ddl.primary_unique", "4c", "Primary and unique keys",
     "After the load. Validating a key row by row during a bulk insert is the "
     "slowest possible way to do it."),
    ("schema_ddl.foreign", "4c", "Foreign keys",
     "After the keys they reference exist, and after the rows they check."),
    ("schema_ddl.check", "4c", "Check constraints",
     "After the load, for the same reason as the keys: validating a condition row "
     "by row during a bulk insert is the slowest way to do it. A check whose "
     "condition used SYSDATE or DECODE was not translated -- 4c reports it rather "
     "than guessing at a business rule, so some of these are a person's."),
    ("schema_ddl.indexes", "4c", "Indexes",
     "Last. An index maintained during a load is rebuilt far faster afterwards."),
    ("application_sql", "4d", "Application SQL (not applied to the database)",
     "A change to the application's own source, not to the target. Listed because "
     "a target nobody changed the application for is one the application cannot "
     "talk to -- Phase 4c renames an object whose Oracle name is a PostgreSQL "
     "keyword, and every statement naming it must follow."),
]

# Statuses that mean an artefact is approved-and-ready versus still owed work.
# Read from each phase's own vocabulary rather than re-deciding it here.
_READY = {"APPROVED", "READY_FOR_APPROVAL", "READY_TO_APPLY", "AUTO_APPLY"}
_NEEDS_PERSON = {"MANUAL", "MODEL_REQUIRED", "NEEDS_MODEL_TIER", "REJECTED",
                 "DECISION_REQUIRED", "HUMAN_AUTHORED_REQUIRED",
                 "CONVERTED_UNPROVEN", "BLOCKED"}
# Neither ready nor owed: these are objects that correctly produce nothing for
# the target. A package specification's body carries its members, and an object
# that fails to compile on Oracle today is an estate problem on either path.
# Counting them as outstanding work would invent tasks nobody can do.
_NOT_WORK = {"EXCLUDED_BROKEN_ON_SOURCE", "ABSORBED_INTO_BODY"}


def load(paths: dict | None = None) -> dict:
    """Each artefact, or None where its phase has not run.

    Never raises on a missing file: a provision is legitimate without any of
    these, and the caller distinguishes absent from empty.
    """
    paths = paths or PATHS
    out: dict = {}
    for name, path in paths.items():
        p = Path(path)
        out[name] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    return out


def _counts(plan: dict | None) -> dict:
    """How much of a 4b or 4d plan is ready, and how much still needs a person."""
    if not plan:
        return {"ran": False}
    entries = plan.get("entries") or []
    ready = sum(1 for e in entries if e.get("status") in _READY)
    person = sum(1 for e in entries if e.get("status") in _NEEDS_PERSON)
    not_work = sum(1 for e in entries if e.get("status") in _NOT_WORK)
    unknown = [e.get("status") for e in entries
               if e.get("status") not in _READY | _NEEDS_PERSON | _NOT_WORK]
    return {
        "ran": True,
        "total": len(entries),
        "ready": ready,
        "needs_a_person": person,
        # Reported so `ready + needs_a_person` never has to equal the total for
        # the record to be legible.
        "not_work": not_work,
        # A status this module has never seen must be visible, not silently
        # dropped from every count -- the same instinct as an unmapped SCT code
        # routing to a person rather than to "automatic".
        "unrecognised_statuses": sorted(set(s for s in unknown if s)),
        # An approver is what turns a gated proposal into something that may be
        # applied. Absent, every "ready" object is still only ready.
        "approved_by": plan.get("approved_by"),
        "generated_at": plan.get("generated_at") or plan.get("generated_at_utc"),
        "collector_run_id": plan.get("collector_run_id"),
    }


def summarise(artefacts: dict, estate: str | None = None) -> dict:
    """What is prepared for this target, in the order it must be applied.

    `estate` is checked against each artefact where it records one. An artefact
    prepared for a different schema is reported rather than counted -- the same
    reason `provision/records.py` refuses four records from different collector
    runs.
    """
    ddl = artefacts.get("schema_ddl")
    code = artefacts.get("stored_code")
    app = artefacts.get("application_sql")

    mismatched = []
    for name, rec, key in (("schema_ddl", ddl, "estate"),
                           ("stored_code", code, "estate")):
        if rec and estate and rec.get(key) and rec[key] != estate:
            mismatched.append(f"{name} was prepared for {rec[key]}, not {estate}")

    steps = []
    for key, phase, label, why in ORDER:
        if key == "__data_load__":
            steps.append({"key": key, "phase": phase, "label": label, "why": why,
                          "count": None, "state": "phase_7"})
            continue
        if key == "application_sql":
            c = _counts(app)
            steps.append({
                "key": key, "phase": phase, "label": label, "why": why,
                "count": c.get("ready") if c["ran"] else None,
                "total": c.get("total"),
                # Never "applied": 4d changes source files, and this phase
                # touches a database.
                "state": "not_run" if not c["ran"] else "for_the_application",
                "needs_a_person": c.get("needs_a_person"),
            })
            continue
        if key.startswith("schema_ddl."):
            group = key.split(".", 1)[1]
            n = len((ddl or {}).get(group) or []) if ddl else None
            steps.append({"key": key, "phase": phase, "label": label, "why": why,
                          "count": n,
                          "state": "not_run" if ddl is None else
                                   ("ready" if n else "none_needed")})
            continue
        # 4b, split into the types that must precede the tables and the
        # routines that must follow them.
        c = _counts(code)
        if not c["ran"]:
            steps.append({"key": key, "phase": phase, "label": label, "why": why,
                          "count": None, "state": "not_run"})
            continue
        entries = (code or {}).get("entries") or []
        is_type = key.endswith(".types")
        sel = [e for e in entries
               if (e.get("object_type") or "").upper().startswith("TYPE") == is_type]
        ready = sum(1 for e in sel if e.get("status") in _READY)
        steps.append({"key": key, "phase": phase, "label": label, "why": why,
                      "count": ready, "total": len(sel),
                      "state": "ready" if ready else
                               ("none_needed" if not sel else "needs_a_person"),
                      "needs_a_person": sum(1 for e in sel
                                            if e.get("status") in _NEEDS_PERSON)})

    prepared = {
        "schema_ddl": {"ran": ddl is not None,
                       **({"tables": len(ddl.get("tables") or []),
                           "statements": sum(len(ddl.get(k) or []) for k in
                                             ("tables", "primary_unique", "foreign",
                                              "check", "indexes")),
                           "compiled": bool((ddl.get("compile") or {}).get("ok")),
                           "estate": ddl.get("estate"),
                           "notes_needing_review": sum(
                               1 for n in (ddl.get("notes") or [])
                               if n.get("severity") in ("error", "warn"))}
                          if ddl else {})},
        "stored_code": _counts(code),
        "application_sql": _counts(app),
    }

    # What a person must do before any of this reaches the target. Said as
    # tasks, not as a status, because a status names a state and a task names
    # the next action.
    outstanding: list[str] = []
    if ddl is None:
        outstanding.append(
            "Phase 4c has not run, so the target has no prepared schema. DMS would "
            "create tables itself with a fixed mapping and no constraints or indexes "
            "at all -- run 4c, or accept that.")
    elif not (ddl.get("compile") or {}).get("ok"):
        outstanding.append(
            "Phase 4c's DDL has not compiled cleanly on a PostgreSQL, so it is "
            "generated but unproven. Register a target and re-run 4c.")
    if code is None:
        outstanding.append(
            "Phase 4b has not run, so no stored code is prepared. On the heterogeneous "
            "path the target has no procedures, functions or triggers until it does.")
    elif prepared["stored_code"].get("needs_a_person"):
        outstanding.append(
            f"{prepared['stored_code']['needs_a_person']} stored object(s) are not ready: "
            "each needs a person, and the target will be missing them.")
    if code is not None and not prepared["stored_code"].get("approved_by"):
        outstanding.append(
            "No approver is recorded on Phase 4b's conversions. Converted business logic "
            "reaches a target only after a named person accepts it.")
    if app is None:
        outstanding.append(
            "Phase 4d has not run, so nothing has checked the SQL the application sends. "
            "Every statement still names Oracle constructs the target will refuse.")
    elif prepared["application_sql"].get("needs_a_person"):
        outstanding.append(
            f"{prepared['application_sql']['needs_a_person']} application statement(s) are "
            "not ready. The target can be provisioned without them; the application "
            "cannot talk to it until they are changed.")
    for m in mismatched:
        outstanding.append(m + " -- provisioning from a mix describes no estate.")

    return {
        "prepared": prepared,
        "apply_order": steps,
        "outstanding": outstanding,
        "mismatched": mismatched,
        "nothing_applied": True,
        "what_this_is_not": (
            "A readiness report, not an apply. Every artefact here was gated by its own "
            "phase and left for a person; provisioning does not turn an approval nobody "
            "gave into one, and this module writes to no database."),
    }


def check(artefacts: dict, estate: str | None = None) -> dict:
    """A preflight-shaped check: does the prepared work describe this estate?

    Shaped like `provision/records.consistency` so the preflight can carry it
    beside the others. A mismatch fails; a phase that has not run warns, because
    provisioning an empty target is a choice rather than an error.
    """
    s = summarise(artefacts, estate)
    if s["mismatched"]:
        return {"name": "prepared_artefacts", "status": "fail",
                "detail": "; ".join(s["mismatched"]),
                "remedy": "Re-run the preparation phase against this estate so every "
                          "record describes the same schema."}
    missing = [n for n in ("schema_ddl", "stored_code", "application_sql")
               if not s["prepared"][n].get("ran")]
    if missing:
        return {"name": "prepared_artefacts", "status": "warn",
                "detail": "not run: " + ", ".join(missing)
                          + " -- the target will be provisioned without them",
                "remedy": "Run the missing phase, or accept an empty target and let "
                          "Phase 7 improvise what it can."}
    return {"name": "prepared_artefacts", "status": "pass",
            "detail": f"schema, stored code and application SQL all prepared for "
                      f"{estate or 'this estate'}",
            "remedy": None}
