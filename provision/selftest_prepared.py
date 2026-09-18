"""Offline checks for what Phases 4b, 4c and 4d prepared. No database, no AWS.

Phase 6 creates an empty instance, and until this module existed nothing told a
reader what the earlier phases had prepared for it. The failures worth
protecting against:

  1. **A mix of estates.** `provision/records.py` already refuses four records
     from different collector runs, because provisioning from a mix describes
     no estate. The prepared artefacts have the same problem and it is not
     hypothetical: the first run of this module found 4b and 4c output for
     DBMIG_TELCO sitting on disk while the console held DBMIG_APP.

  2. **Absent read as zero.** A phase that never ran must report *not run*,
     never "nothing to do". A target provisioned without 4c's schema is a
     legitimate choice; it is not the same choice as one whose schema was
     prepared and reviewed.

  3. **Invented or vanished work.** `EXCLUDED_BROKEN_ON_SOURCE` and
     `ABSORBED_INTO_BODY` are neither ready nor owed -- a package spec's body
     carries its members, and an object broken on Oracle today is an estate
     problem on either path. Counting them as outstanding invents tasks nobody
     can do; dropping them silently makes the totals not add up.

  4. **The apply order going wrong.** Constraints before the load, or routines
     before their tables, fails. The order is data here so it can be checked
     against the phase docs.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "provision"

from . import prepared


def _ddl(estate="DBMIG_APP", compiled=True, tables=3):
    return {
        "estate": estate,
        "schema": f"CREATE SCHEMA IF NOT EXISTS {estate.lower()};",
        "tables": [f"CREATE TABLE t{i} (a int)" for i in range(tables)],
        "primary_unique": ["ALTER TABLE t0 ADD PRIMARY KEY (a)"],
        "foreign": ["ALTER TABLE t1 ADD FOREIGN KEY (a) REFERENCES t0(a)"],
        "check": ["ALTER TABLE t0 ADD CHECK (a > 0)"],
        "indexes": ["CREATE INDEX ix0 ON t0 (a)"],
        "notes": [{"kind": "reserved_word", "severity": "warn", "subject": "ORDER",
                   "detail": "renamed"},
                  {"kind": "type_mapping", "severity": "info", "subject": "X.Y",
                   "detail": "NUMBER -> BIGINT"}],
        "compile": {"ran": 7, "failed": 0, "ok": compiled, "rolled_back": True},
    }


def _code(estate="DBMIG_APP", approver=None):
    return {"estate": estate, "approved_by": approver, "entries": [
        {"object_type": "TYPE", "object_name": "TY_ADDRESS", "status": "READY_FOR_APPROVAL"},
        {"object_type": "FUNCTION", "object_name": "FN_A", "status": "READY_FOR_APPROVAL"},
        {"object_type": "PROCEDURE", "object_name": "SP_B", "status": "MANUAL"},
        {"object_type": "PACKAGE", "object_name": "PKG", "status": "ABSORBED_INTO_BODY"},
        {"object_type": "PROCEDURE", "object_name": "SP_BROKEN",
         "status": "EXCLUDED_BROKEN_ON_SOURCE"},
    ]}


def _app(ready=1, person=2):
    return {"approved_by": None, "generated_at": "2026-09-18T00:00:00+00:00", "entries":
            [{"statement_id": f"r{i}", "status": "READY_FOR_APPROVAL"} for i in range(ready)]
            + [{"statement_id": f"p{i}", "status": "MANUAL"} for i in range(person)]}


class Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = Check()

    # -------------------------------------------------------------- loading
    print("loading is optional, and absent is not zero")
    with tempfile.TemporaryDirectory() as d:
        empty = {k: Path(d) / f"{k}.json" for k in prepared.PATHS}
        a = prepared.load(empty)
        c("a missing artefact loads as None", all(v is None for v in a.values()))
        s = prepared.summarise(a, "DBMIG_APP")
        c("a phase that never ran reports not run",
          all(not s["prepared"][k].get("ran") for k in
              ("schema_ddl", "stored_code", "application_sql")))
        c("every step reads not_run",
          all(st["state"] == "not_run" for st in s["apply_order"]
              if st["key"] != "__data_load__"))
        c("nothing is reported as ready", not any(
            st.get("count") for st in s["apply_order"] if st["key"] != "__data_load__"))
        c("the check warns rather than failing",
          prepared.check(a, "DBMIG_APP")["status"] == "warn")
        c("the warning names what is missing",
          "schema_ddl" in prepared.check(a, "DBMIG_APP")["detail"])
        c("it says the target will go without them",
          "without them" in prepared.check(a, "DBMIG_APP")["detail"])

        # Absent must produce tasks, not silence: a client provisioning an
        # empty target has to know DMS will improvise the schema.
        c("an absent 4c is called out", any("4c has not run" in o for o in s["outstanding"]))
        c("it names what DMS would do instead",
          any("DMS would create tables itself" in o for o in s["outstanding"]))
        c("an absent 4b is called out", any("4b has not run" in o for o in s["outstanding"]))
        c("an absent 4d is called out", any("4d has not run" in o for o in s["outstanding"]))

    # ------------------------------------------------------------- counting
    print("\ncounting never invents or loses work")
    a = {"schema_ddl": _ddl(), "stored_code": _code(), "application_sql": _app()}
    s = prepared.summarise(a, "DBMIG_APP")
    sc = s["prepared"]["stored_code"]
    c("ready counts only approved-and-gated states", sc["ready"] == 2, str(sc))
    c("a manual object needs a person", sc["needs_a_person"] == 1, str(sc))
    c("an absorbed spec is neither", sc["not_work"] == 2, str(sc))
    c("the three buckets account for every entry",
      sc["ready"] + sc["needs_a_person"] + sc["not_work"] == sc["total"], str(sc))
    c("no status is unrecognised", sc["unrecognised_statuses"] == [])

    odd = {"stored_code": {"estate": "DBMIG_APP", "entries":
                           [{"object_type": "FUNCTION", "status": "SOMETHING_NEW"}]},
           "schema_ddl": None, "application_sql": None}
    c("an unknown status is surfaced, not dropped",
      prepared.summarise(odd, "DBMIG_APP")["prepared"]["stored_code"]
      ["unrecognised_statuses"] == ["SOMETHING_NEW"])

    # ---------------------------------------------------------- apply order
    print("\nthe apply order is the one the phases document")
    keys = [st["key"] for st in s["apply_order"]]
    c("tables come first", keys[0] == "schema_ddl.tables", str(keys[:2]))
    c("types precede the routines that follow the tables",
      keys.index("stored_code.types") < keys.index("stored_code.routines"))
    c("the data load is marked", "__data_load__" in keys)
    load_at = keys.index("__data_load__")
    for after in ("schema_ddl.primary_unique", "schema_ddl.foreign",
                  "schema_ddl.check", "schema_ddl.indexes"):
        c(f"{after.split('.')[1]} comes after the load", keys.index(after) > load_at)
    for before in ("schema_ddl.tables", "stored_code.types", "stored_code.routines"):
        c(f"{before.split('.')[-1]} comes before the load", keys.index(before) < load_at)
    c("indexes are last of the database work",
      keys.index("schema_ddl.indexes") == max(
          keys.index(k) for k in keys if k.startswith("schema_ddl.")))
    c("every step carries a reason",
      all(len(st["why"]) > 30 for st in s["apply_order"]))
    c("the data-load step belongs to Phase 7",
      next(st for st in s["apply_order"] if st["key"] == "__data_load__")["phase"] == "7")

    # 4d is not applied to the database at all.
    app_step = next(st for st in s["apply_order"] if st["key"] == "application_sql")
    c("application SQL is not a database change",
      app_step["state"] == "for_the_application", app_step["state"])
    c("its label says so", "not applied to the database" in app_step["label"])

    # --------------------------------------------------------- estate mixes
    print("\na mix of estates is a failure, not a warning")
    mixed = {"schema_ddl": _ddl(estate="DBMIG_TELCO"), "stored_code": _code(),
             "application_sql": _app()}
    s2 = prepared.summarise(mixed, "DBMIG_APP")
    c("the mismatch is detected", len(s2["mismatched"]) == 1, str(s2["mismatched"]))
    c("it names both estates",
      "DBMIG_TELCO" in s2["mismatched"][0] and "DBMIG_APP" in s2["mismatched"][0])
    c("the check fails", prepared.check(mixed, "DBMIG_APP")["status"] == "fail")
    c("the remedy says to re-run the phase",
      "Re-run" in (prepared.check(mixed, "DBMIG_APP")["remedy"] or ""))
    c("a matching estate passes",
      prepared.check({"schema_ddl": _ddl(), "stored_code": _code(),
                      "application_sql": _app()}, "DBMIG_APP")["status"] == "pass")
    # No estate given: nothing to compare against, so nothing is claimed.
    c("no estate means no mismatch claimed",
      prepared.summarise(mixed, None)["mismatched"] == [])

    # ------------------------------------------------------- what is owed
    print("\nwhat a person still owes is said as a task")
    c("an unapproved 4b is called out",
      any("No approver is recorded" in o for o in s["outstanding"]))
    c("an approved 4b is not",
      not any("No approver" in o for o in prepared.summarise(
          {"schema_ddl": _ddl(), "stored_code": _code(approver="dba@client.example"),
           "application_sql": _app(ready=3, person=0)}, "DBMIG_APP")["outstanding"]))
    c("unready stored objects are counted",
      any("stored object(s) are not ready" in o for o in s["outstanding"]))
    c("unready application statements are counted",
      any("application statement(s) are not ready" in o for o in s["outstanding"]))
    c("the application point distinguishes target from application",
      any("cannot talk to it" in o for o in s["outstanding"]))

    uncompiled = {"schema_ddl": _ddl(compiled=False), "stored_code": _code(),
                  "application_sql": _app()}
    c("uncompiled DDL is called out",
      any("has not compiled cleanly" in o
          for o in prepared.summarise(uncompiled, "DBMIG_APP")["outstanding"]))
    c("compiled DDL is not",
      not any("has not compiled" in o for o in s["outstanding"]))

    print("\nthe record is honest about what it is")
    c("nothing is applied", s["nothing_applied"] is True)
    c("it says it is a readiness report",
      "not an apply" in s["what_this_is_not"])
    c("it says provisioning does not approve",
      "approval nobody gave" in s["what_this_is_not"])
    c("the DDL summary reports its compile state",
      s["prepared"]["schema_ddl"]["compiled"] is True)
    c("the DDL summary counts notes needing review",
      s["prepared"]["schema_ddl"]["notes_needing_review"] == 1,
      str(s["prepared"]["schema_ddl"]))

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
