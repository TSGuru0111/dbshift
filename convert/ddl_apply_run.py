"""Create Phase 4c's schema on a real PostgreSQL target.

    python -m convert.ddl_apply_run                                   # preflight only, free
    python -m convert.ddl_apply_run --apply --confirm host:5432/db \
        --approved-by you@example.com                                 # pre-load: schema, types, tables
    python -m convert.ddl_apply_run --apply --post-load --confirm host:5432/db \
        --approved-by you@example.com                                 # after Phase 7: keys, checks, indexes

**This writes to a database**, like `convert.apply_run` and unlike everything
else in `convert/`, so it asks for the same two things: the target DSN typed
back, and a named person.

It exists because `ddl_apply.py` had no command. Phase 4c's apply was driven by
an inline `python -c` script written fresh each time, which is exactly the kind
of step that is done slightly differently on the day it matters. The module was
always the careful part; this is only the door to it.

**The order is not cosmetic.** The pre-load pass stops after the tables, because
validating a foreign key row by row during a bulk load is the slowest possible
way to do it; `--post-load` applies the keys, checks and indexes once Phase 7's
rows are in. Running post-load first will fail, and says so rather than
half-applying.

The password comes from the environment and is held in memory only:
    DBSHIFT_PG_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "convert"

from . import ddl_apply as ddl_apply_mod
from . import target as target_mod

OUTPUT = Path(__file__).resolve().parent / "output"
PLAN = OUTPUT / "schema_ddl.json"


def record_path(post_load: bool) -> Path:
    """Each pass keeps its own record. One file would mean the post-load apply
    overwrote the evidence that the pre-load one ever happened.

    These two names are the ones the inline script that preceded this CLI
    already wrote, and `convert/output/` still holds its 19- and 41-statement
    records from the real RDS apply. Writing to new names would have orphaned
    them and made it look as though 4c had never been applied to a target.
    """
    return OUTPUT / ("ddl_apply_post_record.json" if post_load else "ddl_apply_record.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Apply Phase 4c's schema DDL to a PostgreSQL target")
    ap.add_argument("--plan", type=Path, default=PLAN)
    ap.add_argument("--pg-dsn", default=os.environ.get("DBSHIFT_PG_DSN", "localhost:5432/dbshift"))
    ap.add_argument("--pg-user", default=os.environ.get("DBSHIFT_PG_USER", "dbshift"))
    ap.add_argument("--post-load", action="store_true",
                    help="apply the keys, checks and indexes deferred until after Phase 7")
    ap.add_argument("--apply", action="store_true",
                    help="actually create the objects. THIS WRITES TO THE DATABASE.")
    ap.add_argument("--confirm", default=None,
                    help="the target DSN, typed back, so a wrong database is a refusal")
    ap.add_argument("--approved-by", default=None, help="who is running this")
    ap.add_argument("--allow-existing", action="store_true",
                    help="proceed when the target already carries some of these tables "
                         "(only when they are known-empty and you intend to add the rest)")
    args = ap.parse_args(argv)

    if not args.plan.exists():
        raise SystemExit(f"no schema DDL plan at {args.plan}; run `python -m convert.ddl_run` first")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))

    which = "post-load" if args.post_load else "pre-load"
    statements = ddl_apply_mod.statements_for(plan, post_load=args.post_load)
    compiled = bool((plan.get("compile") or {}).get("ok"))

    print(f"\nestate    : {plan.get('estate')}   run {plan.get('collector_run_id')}")
    print(f"target    : {args.pg_dsn}")
    print(f"pass      : {which}")
    print(f"compiled  : {compiled}")
    print(f"\nwould apply {len(statements)} statement(s):")
    for s in statements:
        print(f"  {s.strip().splitlines()[0][:96]}")

    # The same violations the module refuses on, surfaced before anything opens
    # a connection -- a generator defect should not read as a database error.
    violations = ddl_apply_mod.check_statements(statements)
    if violations:
        print(f"\n{len(violations)} statement(s) are not shapes 4c produces:")
        for v in violations[:5]:
            print(f"  {v}")
        print("\nThis is a defect in the DDL generator, not something to force through.")
        return 2

    if not compiled:
        print("\nThe DDL has not compiled cleanly on a PostgreSQL. Run "
              "`python -m convert.ddl_run --compile` first. Nothing was applied.")
        return 2

    if not args.apply:
        print(f"\nnothing was applied")
        cmd = ("python -m convert.ddl_apply_run --apply"
               + (" --post-load" if args.post_load else "")
               + f" --confirm {args.pg_dsn} --approved-by <you>")
        print(f"to apply: {cmd}")
        if not args.post_load:
            print("then, once Phase 7's rows are in, the same command with --post-load")
        return 0

    password = os.environ.get("DBSHIFT_PG_PASSWORD")
    if not password:
        print("\nset DBSHIFT_PG_PASSWORD. Nothing was applied.")
        return 2
    if not args.approved_by:
        print("\n--apply needs --approved-by: it is recorded against a person. "
              "Nothing was applied.")
        return 2
    if args.confirm != args.pg_dsn:
        print(f"\n--apply needs --confirm {args.pg_dsn}. Nothing was applied.")
        return 2

    pg = target_mod.PgTarget.parse(args.pg_dsn, args.pg_user, password)
    try:
        record = ddl_apply_mod.apply(plan, pg, approved_by=args.approved_by,
                                     post_load=args.post_load,
                                     allow_existing=args.allow_existing)
    except ddl_apply_mod.ApplyRefused as exc:
        print(f"\nrefused: {exc}")
        print("Nothing was applied.")
        return 2

    OUTPUT.mkdir(parents=True, exist_ok=True)
    out = record_path(args.post_load)
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nstatus: {'applied' if record['ok'] else 'failed'}")
    if record["ok"]:
        print(f"applied {record['statements_applied']} statement(s) "
              f"as {record['approved_by']}")
    else:
        print(f"{record.get('error', 'failed')}")
        if record.get("in_flight_when_it_failed"):
            print(f"in flight: {record['in_flight_when_it_failed']}")
        print("Every statement was rolled back; the target is unchanged.")
    print(f"what is left: {record['what_is_left']}")
    print(f"record: {out}")
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
