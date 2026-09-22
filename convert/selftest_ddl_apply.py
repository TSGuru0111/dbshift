"""Offline checks for applying Phase 4c's schema to a target. No database, no AWS.

This is the second module in `convert/` that writes to a real database, so the
checks are about refusals more than successes.

What they defend:

  1. **The pre/post-load split.** 4c's order puts keys, checks and indexes after
     the data load, because validating a foreign key row by row during a bulk
     insert is the slowest way to do it. An apply that ignored that would work
     and be slow in a way nobody would attribute to this module.

  2. **Unproven DDL is refused.** "It compiled against the Docker container" is
     not "it compiles against this RDS instance", and applying DDL that never
     compiled anywhere is how a target ends up half-built.

  3. **A target that already has the tables is refused with a sentence**, not
     left to fail partway through on PostgreSQL's duplicate-object error.

  4. **Nothing but 4c's own shapes may execute.** Anything else in the plan is a
     defect in the generator, not something to run against a client's target.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "convert"

from . import ddl_apply


def _plan(compiled=True, estate="DBMIG_APP"):
    return {
        "estate": estate,
        "schema": f"CREATE SCHEMA IF NOT EXISTS {estate.lower()};",
        "sequences_needed": ["seq_a", "seq_b"],
        "depends_on_types": ["TY_ADDRESS"],
        "tables": [f"CREATE TABLE {estate.lower()}.customer (id BIGINT)",
                   f"CREATE TABLE {estate.lower()}.loan (id BIGINT)"],
        "primary_unique": [f"ALTER TABLE {estate.lower()}.customer ADD PRIMARY KEY (id)"],
        "foreign": [f"ALTER TABLE {estate.lower()}.loan ADD FOREIGN KEY (id) "
                    f"REFERENCES {estate.lower()}.customer(id)"],
        "check": [f"ALTER TABLE {estate.lower()}.customer ADD CHECK (id > 0)"],
        "indexes": [f"CREATE INDEX ix_loan ON {estate.lower()}.loan (id)"],
        "compile": {"ran": 8, "failed": 0, "ok": compiled, "rolled_back": True},
    }


class _Cur:
    """A cursor that records statements and can be told to fail on one."""

    def __init__(self, fail_on=None, existing=()):
        self.ran, self.fail_on, self.existing = [], fail_on, list(existing)

    def execute(self, sql, params=None):
        self.ran.append(sql)
        if "information_schema.tables" in sql:
            self._rows = [(t,) for t in self.existing]
            return
        if self.fail_on and self.fail_on in sql:
            raise Exception({"S": "ERROR", "C": "42P07",
                             "M": 'relation "customer" already exists'})

    def fetchall(self):
        return getattr(self, "_rows", [])


class _Target:
    def __init__(self, fail_on=None, existing=()):
        self.cur = _Cur(fail_on, existing)
        self.closed = 0

    def connect(self, *, timeout: int = 10):  # noqa: ARG002 -- signature parity with PgTarget
        outer = self

        class _Conn:
            def cursor(self):
                return outer.cur

            def close(self):
                outer.closed += 1
        return _Conn()

    def describe(self):
        return "stub@localhost:5432/stub"


class Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = Check()
    p = _plan()

    print("the pre/post-load split is 4c's own order")
    pre = ddl_apply.statements_for(p, post_load=False)
    post = ddl_apply.statements_for(p, post_load=True)
    c("the schema comes first", "CREATE SCHEMA" in pre[0], pre[0])
    c("sequences precede the tables",
      next(i for i, s in enumerate(pre) if "CREATE SEQUENCE" in s)
      < next(i for i, s in enumerate(pre) if "CREATE TABLE" in s))
    c("types precede the tables",
      next(i for i, s in enumerate(pre) if "CREATE TYPE" in s)
      < next(i for i, s in enumerate(pre) if "CREATE TABLE" in s))
    c("the tables are in the pre-load pass",
      sum(1 for s in pre if "CREATE TABLE" in s) == 2)
    c("no constraint is in the pre-load pass",
      not any("PRIMARY KEY" in s or "FOREIGN KEY" in s or "ADD CHECK" in s for s in pre))
    c("no index is in the pre-load pass", not any("CREATE INDEX" in s for s in pre))
    c("keys, checks and indexes are all post-load", len(post) == 4, str(len(post)))
    c("the post-load pass creates no table",
      not any("CREATE TABLE" in s for s in post))

    print("\nrefusals, each with a sentence")
    for kwargs, target, why, expect in [
        ({"approved_by": ""}, _Target(), "no approver", "no approver named"),
        ({"approved_by": "a@b.c"}, None, "no target", "no PostgreSQL target"),
    ]:
        try:
            ddl_apply.apply(p, target, **kwargs)
            c(f"{why} is refused", False, "it proceeded")
        except ddl_apply.ApplyRefused as exc:
            c(f"{why} is refused", True)
            c("  ...and says why", expect in str(exc), str(exc))

    try:
        ddl_apply.apply(_plan(compiled=False), _Target(), approved_by="a@b.c")
        c("unproven DDL is refused", False, "it proceeded")
    except ddl_apply.ApplyRefused as exc:
        c("unproven DDL is refused", True)
        c("  ...and names the compile", "compiled cleanly" in str(exc))
        c("  ...and says what it risks", "half-built" in str(exc))

    # A target already carrying the tables: refused with a sentence rather than
    # left to fail partway through.
    try:
        ddl_apply.apply(p, _Target(existing=["customer"]), approved_by="a@b.c")
        c("an existing table is refused", False, "it proceeded")
    except ddl_apply.ApplyRefused as exc:
        c("an existing table is refused", True)
        c("  ...and names it", "customer" in str(exc))
        c("  ...and explains the alternative", "allow_existing" in str(exc))

    print("\nonly 4c's own shapes may execute")
    c("a DROP TABLE is caught",
      ddl_apply.check_statements(["DROP TABLE customer"]))
    c("a GRANT is caught", ddl_apply.check_statements(["GRANT ALL ON x TO y"]))
    c("a TRUNCATE is caught", ddl_apply.check_statements(["TRUNCATE customer"]))
    c("a DELETE is caught", ddl_apply.check_statements(["DELETE FROM customer"]))
    c("a function body is caught",
      ddl_apply.check_statements(["CREATE FUNCTION f() RETURNS int AS $$ $$ LANGUAGE sql"]))
    c("4c's own statements pass", not ddl_apply.check_statements(pre + post))

    print("\none transaction, all or nothing")
    t = _Target()
    r = ddl_apply.apply(p, t, approved_by="dba@client.example")
    c("a clean apply succeeds", r["ok"] is True, str(r.get("error")))
    c("it commits once", t.cur.ran.count("COMMIT") == 1)
    c("it begins once", t.cur.ran.count("BEGIN") == 1)
    c("nothing_applied is false when it worked", r["nothing_applied"] is False)
    c("every statement is counted", r["statements_applied"] == len(pre), str(r))
    c("the approver is recorded", r["approved_by"] == "dba@client.example")
    c("the pass is named", r["pass"] == "pre_load")
    c("it says what is left",
      "deferred until after the data load" in r["what_is_left"])
    c("every connection it opened is closed", t.closed == 2, f"closed {t.closed}")

    t2 = _Target(fail_on="CREATE TABLE")
    r2 = ddl_apply.apply(p, t2, approved_by="dba@client.example")
    c("a failure rolls back", r2["ok"] is False)
    c("it rolled back, not committed",
      "ROLLBACK" in t2.cur.ran and "COMMIT" not in t2.cur.ran)
    c("nothing_applied is true when it failed", r2["nothing_applied"] is True)
    c("no statement is claimed applied", r2["statements_applied"] == 0)
    c("the engine's message is readable",
      "already exists" in (r2["error"] or ""), str(r2["error"]))
    c("what was in flight is recorded",
      "CREATE TABLE" in (r2["in_flight_when_it_failed"] or ""))
    c("it says nothing was applied",
      "rolled back" in r2["what_is_left"])

    print("\nthe post-load pass")
    t3 = _Target()
    r3 = ddl_apply.apply(p, t3, approved_by="dba@client.example", post_load=True)
    c("the post-load pass succeeds", r3["ok"] is True)
    c("it is named", r3["pass"] == "post_load")
    c("it applies the deferred statements", r3["statements_applied"] == 4)
    c("it does not re-check for existing tables",
      not any("information_schema" in s for s in t3.cur.ran))
    c("it says the schema is complete", "complete" in r3["what_is_left"])

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
