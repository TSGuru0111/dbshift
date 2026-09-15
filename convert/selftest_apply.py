"""Checks for the apply path -- the one place in convert/ that writes to a database.

Every check here is about a way applying converted code could go wrong: applying
something nobody approved, applying to the wrong database, applying a statement
that drops or grants, or leaving a half-built schema behind when one object
fails.

The proof half runs a real apply against the local PostgreSQL, including a
deliberate failure, and checks the rollback left nothing behind.

    python -m convert.selftest_apply
    python -m convert.selftest_apply --offline
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "convert"

from . import apply as apply_mod
from . import target as target_mod

SCHEMA = "dbshift_apply_selftest"


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _entry(name, status, statements, otype="FUNCTION"):
    return {"owner": SCHEMA.upper(), "object_type": otype, "object_name": name,
            "status": status, "conversion": {"statements": statements}}


def _plan(entries, **kw):
    return {"estate": SCHEMA.upper(), "collector_run_id": "selftest",
            "approved_by": kw.pop("approved_by", "someone@example.com"),
            "entries": entries, **kw}


def main(argv=None) -> int:
    offline = "--offline" in (argv if argv is not None else sys.argv[1:])
    c = _Check()

    print("only approved code is applied")
    entries = [
        _entry("F_OK", "APPROVED", ["SELECT 1"]),
        _entry("F_READY", "READY_FOR_APPROVAL", ["SELECT 1"]),
        _entry("F_BLOCKED", "BLOCKED", ["SELECT 1"]),
        _entry("F_REJECTED", "REJECTED", ["SELECT 1"]),
        _entry("T_MANUAL", "MANUAL", [], otype="TYPE"),
    ]
    split = apply_mod.selectable(entries)
    names = {e["object_name"] for e in split["appliable"]}
    c("an approved object is appliable", names == {"F_OK"}, str(names))
    c("ready-for-approval is held back", len(split["held_back"]) == 4)
    held = {h["object_name"]: h["why_not"] for h in split["held_back"]}
    c("and the reason names the missing approval", "Approve it first" in held["F_READY"])
    c("blocked explains what a blocked gate means", "proven to compile" in held["F_BLOCKED"])
    c("rejected says it would fail on the target too", "fail on the target" in held["F_REJECTED"])
    c("manual says a person must write it", "a person must write" in held["T_MANUAL"])
    c("every held-back object has a reason", all(h["why_not"] for h in split["held_back"]))
    c("APPLIABLE is deliberately one status", apply_mod.APPLIABLE == {"APPROVED"})

    print("destructive statements are refused, whatever a gate said")
    for sql, label in [
        ("DROP TABLE customer", "DROP TABLE"),
        ("TRUNCATE customer", "TRUNCATE"),
        ("DELETE FROM customer", "DELETE"),
        ("GRANT SELECT ON customer TO app", "GRANT"),
        ("CREATE FUNCTION f() RETURNS void SECURITY DEFINER AS $$ $$", "SECURITY DEFINER"),
    ]:
        c(f"{label} is caught", len(apply_mod.screen([sql])) == 1)
    c("ordinary converted code passes the screen",
      apply_mod.screen(["CREATE OR REPLACE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END $$ "
                        "LANGUAGE plpgsql"]) == [])
    c("the refusal explains itself",
      all(len(p["why"]) > 15 for p in apply_mod.screen(["DROP TABLE x"])))

    print("preflight refuses before anything runs")
    pre = apply_mod.preflight(_plan([_entry("F", "READY_FOR_APPROVAL", ["SELECT 1"])]), None,
                              approved_by="a@b")
    c("nothing approved means not ready", not pre["ready"])
    c("and it says which check refused", "something to apply" in pre["refused_because"])
    pre = apply_mod.preflight(_plan([_entry("F", "APPROVED", ["SELECT 1"])], approved_by=None),
                              None, approved_by="a@b")
    c("a plan with no approver is refused", "approval on record" in pre["refused_because"])
    pre = apply_mod.preflight(_plan([_entry("F", "APPROVED", ["DROP TABLE x"])]), None,
                              approved_by="a@b")
    c("a destructive statement is refused", "nothing destructive" in pre["refused_because"])
    c("no target is refused", "target configured" in pre["refused_because"])
    c("preflight itself changes nothing", pre["statement_count"] == 1)

    if offline:
        print(f"\n{c.ok}/{c.n} checks passed (offline; the proof was skipped)")
        return 0 if c.ok == c.n else 1

    print("proof: a real apply, a real failure, and a real rollback")
    dsn = os.environ.get("DBSHIFT_PG_DSN", "localhost:5432/dbshift")
    try:
        pg = target_mod.PgTarget.parse(
            dsn, os.environ.get("DBSHIFT_PG_USER", "dbshift"),
            os.environ.get("DBSHIFT_PG_PASSWORD", "dbshift-local-only"))
        conn = pg.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"  [skip] no PostgreSQL at {dsn}: {str(exc).splitlines()[0]}")
        print(f"\n{c.ok}/{c.n} checks passed (the proof was skipped)")
        return 0 if c.ok == c.n else 1

    cur = conn.cursor()
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    cur.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.commit()
    conn.close()

    def functions() -> set[str]:
        cn = pg.connect()
        cu = cn.cursor()
        cu.execute("""SELECT p.proname FROM pg_proc p JOIN pg_namespace n
                      ON n.oid = p.pronamespace WHERE n.nspname = %s""", (SCHEMA,))
        out = {r[0] for r in cu.fetchall()}
        cn.close()
        return out

    good = [f"CREATE FUNCTION {SCHEMA}.f_one() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;"]
    also = [f"CREATE FUNCTION {SCHEMA}.f_two() RETURNS int LANGUAGE sql AS $$ SELECT 2 $$;"]
    # A *syntax* error, not a call to a missing function. check_function_bodies
    # validates syntax and declarations but deliberately does NOT resolve calls,
    # so a body calling something that does not exist is created happily -- the
    # same behaviour Phase 4b's compile gate documents. What must roll back here
    # is a statement PostgreSQL genuinely refuses.
    bad = [f"CREATE FUNCTION {SCHEMA}.f_bad() RETURNS int LANGUAGE plpgsql AS "
           "$$ BEGIN RETURN ( END $$;"]

    try:
        # A wrong target typed back is refused before anything runs.
        try:
            apply_mod.execute(_plan([_entry("F_ONE", "APPROVED", good)]), pg,
                              approved_by="a@b", confirm_target="wrong:5432/nope")
            c("typing the wrong target is refused", False)
        except apply_mod.ApplyRefused as exc:
            c("typing the wrong target is refused", "typed target must be" in str(exc))
        c("and nothing was created", not functions())

        # No approver, likewise.
        try:
            apply_mod.execute(_plan([_entry("F_ONE", "APPROVED", good)]), pg,
                              approved_by="", confirm_target=pg.dsn)
            c("an apply with no approver is refused", False)
        except apply_mod.ApplyRefused as exc:
            c("an apply with no approver is refused", "recorded against a person" in str(exc))

        # A real apply.
        rec = apply_mod.execute(_plan([_entry("F_ONE", "APPROVED", good)]), pg,
                                approved_by="a@b", confirm_target=pg.dsn)
        c("a real apply succeeds", rec["status"] == "applied", rec.get("error", ""))
        c("the function exists afterwards", "f_one" in functions())
        c("nothing_applied is false once something was", rec["nothing_applied"] is False)
        c("the record names who applied it", rec["applied_by"] == "a@b")
        c("and who approved it", rec["approved_by"] == "someone@example.com")
        c("the record names the target", rec["target"] == pg.dsn)

        # One failure rolls back everything, including the objects that worked.
        before = functions()
        rec = apply_mod.execute(
            _plan([_entry("F_TWO", "APPROVED", also), _entry("F_BAD", "APPROVED", bad)]),
            pg, approved_by="a@b", confirm_target=pg.dsn)
        c("a failing object rolls the apply back", rec["status"] == "rolled_back")
        c("the good object is gone too", functions() == before,
          str(functions() - before))
        c("nothing_applied is true again after a rollback", rec["nothing_applied"] is True)
        c("the record says which object failed",
          any(o["status"] == "failed" for o in rec["objects"]))
        c("and what had been attempted before it",
          any(o["status"] == "applied" for o in rec["objects"]))
    finally:
        cn = pg.connect()
        cu = cn.cursor()
        cu.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cn.commit()
        cn.close()

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
