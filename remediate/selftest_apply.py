"""Offline checks for applying fixes to the rehearsal copy. No database.

This is the only module in `remediate/` that writes and keeps what it writes, so
the checks are mostly about what it REFUSES. The ways this could go wrong are
not subtle -- apply something a gate rejected, apply to the source by mistake,
apply a statement that still names the source schema -- and each has a check
here that fails loudly if the guard is ever removed.

The apply itself was proven against the real rehearsal copy on 2026-09-16: two
fixes applied, the index and the statistics verified present afterwards, and
`DBMIG_APP` confirmed unchanged (90 objects, statistics still dated 2026-09-08).
That is recorded in the phase doc; what is here is everything that can be
checked without a database.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "remediate"

from . import apply as A

# `check_target` connects, and these checks are the offline half of the proof --
# the online half ran against the real rehearsal copy and is recorded in the
# phase doc. Stubbed so a machine with no database still exercises every refusal.
A.check_target = lambda t: {"ok": True, "detail": f"stub: {t.schema}@{t.dsn}"}

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}\n       got  {got!r}\n       want {want!r}")


class FakeTarget:
    def __init__(self, schema="DBMIG_REHEARSAL", source="DBMIG_APP"):
        self.dsn, self.user, self.password = "localhost:1521/XEPDB1", "u", "p"
        self.schema, self.source_schema = schema, source


def entry(rule_id="PERF-001", status="AUTO_APPLY", sql=None, dry="pass", **kw):
    return {
        "rule_id": rule_id, "status": status, "owner": "DBMIG_APP",
        "object_name": "COLLATERAL_NOTE",
        "sql": sql if sql is not None else
               'CREATE INDEX "DBMIG_APP"."IX_X" ON "DBMIG_APP"."COLLATERAL_NOTE" ("LOAN_ID")',
        "rollback_sql": 'DROP INDEX "DBMIG_APP"."IX_X"',
        "gates": [{"gate": "static", "status": "pass"},
                  {"gate": "policy", "status": "pass"},
                  {"gate": "syntax", "status": "pass"},
                  {"gate": "dry_run", "status": dry, "detail": "on DBMIG_REHEARSAL"},
                  {"gate": "approval", "status": "pass"}],
        **kw,
    }


def held_reason(split, rule_id):
    return next((h["why"] for h in split["held_back"] if h["entry"].startswith(rule_id)), None)


def main() -> int:
    print("what may be applied")
    s = A.selectable([entry()])
    check("an AUTO_APPLY fix with a passing dry run is appliable", len(s["appliable"]), 1)
    s = A.selectable([entry(status="READY_TO_APPLY")])
    check("so is READY_TO_APPLY", len(s["appliable"]), 1)

    for status in ("BLOCKED", "REJECTED", "ADVICE_DRAFTED",
                   "MANUAL_ACTION_REQUIRED", "NOT_A_FIX"):
        s = A.selectable([entry(status=status)])
        check(f"{status} is held back", len(s["appliable"]), 0)
        check(f"{status} says why", bool(held_reason(s, "PERF-001")), True)

    print("\nproven, not merely permitted")
    # The check that matters most: status and gate disagreeing.
    s = A.selectable([entry(dry="fail")])
    check("a failed dry run is held back even at AUTO_APPLY", len(s["appliable"]), 0)
    check("and says the fix is not proven",
          "not proven" in (held_reason(s, "PERF-001") or ""), True)
    no_dry = entry()
    no_dry["gates"] = [g for g in no_dry["gates"] if g["gate"] != "dry_run"]
    s = A.selectable([no_dry])
    check("a MISSING dry run is held back too", len(s["appliable"]), 0)
    s = A.selectable([entry(sql="")])
    check("an appliable status with no SQL is held back", len(s["appliable"]), 0)

    print("\nthe destructive screen")
    for sql, why in [
        ('DROP TABLE "DBMIG_APP"."CUSTOMER"', "drop table"),
        ("TRUNCATE TABLE CUSTOMER", "truncate"),
        ("DELETE FROM CUSTOMER", "delete"),
        ("GRANT DBA TO SCOTT", "grant"),
        ("ALTER DATABASE ARCHIVELOG", "alter database"),
    ]:
        check(f"{why} is caught", bool(A.screen(sql)), True)
    check("an ordinary index is not caught",
          A.screen('CREATE INDEX "S"."IX" ON "S"."T" ("C")'), [])
    check("gathering statistics is not caught",
          A.screen("BEGIN DBMS_STATS.GATHER_TABLE_STATS(ownname => 'S'); END;"), [])

    print("\npreflight refusals")
    plan = {"entries": [entry()], "collector_run_id": "run-1"}
    p = A.preflight(plan, None, applied_by="a@b.com")
    check("no target is refused", p["ready"], False)
    check("and names the check", "rehearsal target configured" in p["refused_because"], True)

    p = A.preflight(plan, FakeTarget(), applied_by=None)
    check("no named applier is refused", p["ready"], False)
    check("and names the check", "a person is named" in p["refused_because"], True)

    # The one that would be catastrophic: rehearsal and source the same schema.
    p = A.preflight(plan, FakeTarget(schema="DBMIG_APP"), applied_by="a@b.com")
    check("rehearsal == source is refused", p["ready"], False)
    check("and names the check",
          "rehearsal is not the source" in p["refused_because"], True)
    check("it REPORTS rather than raises -- a caller checks `ready`, not exceptions",
          isinstance(p, dict), True)

    p = A.preflight({"entries": [entry(status="BLOCKED")]}, FakeTarget(), applied_by="a@b.com")
    check("nothing appliable is refused", p["ready"], False)
    check("and names the check", "something to apply" in p["refused_because"], True)

    print("\nexecute refuses before writing")
    try:
        A.execute(plan, FakeTarget(), applied_by="a@b.com", confirm_schema="WRONG")
        check("a wrong confirmation is refused", "accepted", "refused")
    except A.ApplyRefused as exc:
        check("a wrong confirmation is refused", "refused", "refused")
        # It must name what was typed AND what was expected: "wrong confirmation"
        # alone leaves a person guessing which schema they were meant to type.
        check("and the message names both schemas",
              "DBMIG_REHEARSAL" in str(exc) and "WRONG" in str(exc), True)
    try:
        A.execute({"entries": [entry(status="BLOCKED")]}, FakeTarget(),
                  applied_by="a@b.com", confirm_schema="DBMIG_REHEARSAL")
        check("an unready plan is refused", "accepted", "refused")
    except A.ApplyRefused:
        check("an unready plan is refused", "refused", "refused")

    print("\nremapping onto the copy")
    p = A.preflight(plan, FakeTarget(), applied_by="a@b.com")
    check("preflight is ready on a good plan", p["ready"], True)
    sql = p["remapped"][0][1]
    check("the statement is remapped onto the rehearsal schema",
          "DBMIG_REHEARSAL" in sql, True)
    check("and no longer names the source", "DBMIG_APP" in sql, False)

    print("\nevery status has a reason on record")
    check("appliable statuses are explained",
          all(A.APPLIABLE.values()), True)
    check("non-appliable statuses are explained",
          all(A.NOT_APPLIABLE.values()), True)
    check("the two sets do not overlap",
          set(A.APPLIABLE) & set(A.NOT_APPLIABLE), set())

    print(f"\n{PASS}/{PASS + FAIL} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
