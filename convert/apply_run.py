"""Apply approved converted code to a PostgreSQL target.

    python -m convert.apply_run                                  # preflight only, free
    python -m convert.apply_run --apply --confirm host:5432/db --applied-by you@example.com

**This is the only command in `convert/` that writes to a database.** Everything
else compiles into a transaction and rolls it back. So the target's DSN is typed
back, the same way a deploy asks for the account id, and the apply is recorded
against a named person.

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

from . import apply as apply_mod
from . import target as target_mod

OUTPUT = Path(__file__).resolve().parent / "output"
PLAN = OUTPUT / "conversion_plan.json"
RECORD = OUTPUT / "apply_record.json"


def last_record() -> dict | None:
    return json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.exists() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply approved converted code to PostgreSQL")
    ap.add_argument("--plan", type=Path, default=PLAN)
    ap.add_argument("--pg-dsn", default=os.environ.get("DBSHIFT_PG_DSN", "localhost:5432/dbshift"))
    ap.add_argument("--pg-user", default=os.environ.get("DBSHIFT_PG_USER", "dbshift"))
    ap.add_argument("--apply", action="store_true",
                    help="actually create the objects. THIS WRITES TO THE DATABASE.")
    ap.add_argument("--confirm", default=None,
                    help="the target DSN, typed back, so a wrong database is a refusal")
    ap.add_argument("--applied-by", default=None, help="who is running this")
    args = ap.parse_args(argv)

    if not args.plan.exists():
        raise SystemExit(f"no conversion plan at {args.plan}; run `python -m convert.run` first")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))

    password = os.environ.get("DBSHIFT_PG_PASSWORD")
    pg = None
    if password:
        pg = target_mod.PgTarget.parse(args.pg_dsn, args.pg_user, password)

    pre = apply_mod.preflight(plan, pg, approved_by=args.applied_by)

    print(f"\nestate    : {plan.get('estate')}   run {plan.get('collector_run_id')}")
    print(f"target    : {args.pg_dsn}")
    print(f"approved  : {plan.get('approved_by') or 'nobody'}")
    print(f"\nwould apply {len(pre['appliable'])} object(s), "
          f"{pre['statement_count']} statement(s):")
    for e in pre["appliable"]:
        print(f"  {e['object_type']:14} {e['object_name']}")

    if pre["held_back"]:
        print(f"\nheld back ({len(pre['held_back'])}):")
        for h in pre["held_back"]:
            print(f"  {h['object_name']:26} {h['status']}")
            print(f"      {h['why_not']}")

    print("\nPREFLIGHT")
    for c in pre["checks"]:
        mark = "ok  " if c["ok"] else ("note" if c.get("advisory") else "FAIL")
        print(f"  [{mark}] {c['name']:28} {c['detail']}")
        # A remedy belongs to a check that failed. Printing one under a PASS
        # reads as an instruction to fix something that is already right.
        if c.get("remedy") and not c["ok"]:
            print(f"          -> {c['remedy']}")

    if not args.apply:
        print(f"\nready: {pre['ready']}   nothing was applied")
        if pre["ready"]:
            print(f"to apply: python -m convert.apply_run --apply "
                  f"--confirm {args.pg_dsn} --applied-by <you>")
        return 0 if pre["ready"] else 2

    if not password:
        print("\nset DBSHIFT_PG_PASSWORD. Nothing was applied.")
        return 2
    if not args.applied_by:
        print("\n--apply needs --applied-by: it is recorded against a person. "
              "Nothing was applied.")
        return 2
    if not args.confirm:
        print(f"\n--apply needs --confirm {args.pg_dsn}. Nothing was applied.")
        return 2

    try:
        record = apply_mod.execute(plan, pg, approved_by=args.applied_by,
                                   confirm_target=args.confirm,
                                   on_event=lambda e: print(
                                       f"  {e['event']:12} {e.get('object') or e.get('message', '')}"))
    except apply_mod.ApplyRefused as exc:
        print(f"\nrefused: {exc}")
        print("Nothing was applied.")
        return 2

    OUTPUT.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nstatus: {record['status']}")
    if record["status"] == "applied":
        print(f"applied {len(record['applied'])} object(s) as {record['applied_by']}")
    else:
        print(f"{record.get('error', 'failed')}")
        print("Every object was rolled back; the target is unchanged.")
    print(f"record: {RECORD}")
    return 0 if record["status"] == "applied" else 1


if __name__ == "__main__":
    raise SystemExit(main())
