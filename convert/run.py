from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "convert"

from . import inventory, plan as plan_mod, target as target_mod


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DBShift Phase 4b -- converts PL/SQL to PL/pgSQL, compiles it on a rolled-back "
                    "PostgreSQL transaction, applies nothing"
    )
    parser.add_argument("--run", type=str, default=None, help="collector_run_id (default: latest)")
    parser.add_argument("--collector-output", type=Path, default=inventory.COLLECTOR_OUTPUT)
    parser.add_argument("--output-dir", type=Path, default=plan_mod.OUTPUT)
    parser.add_argument(
        "--model-mode", choices=["off", "static", "live"], default="static",
        help="where a conversion comes from when the rules decline: off (route to a person), static "
             "(hand-written stand-ins in bedrock/static/convert/, labelled as such), live (Bedrock -- "
             "invoke is blocked on this account)",
    )
    parser.add_argument("--pg-dsn", type=str, default=None, help="host:5432/dbname (or DBSHIFT_PG_DSN)")
    parser.add_argument("--no-compile", action="store_true", help="skip the PostgreSQL compile gate")
    parser.add_argument("--approved-by", type=str, default=None, help="record a named approver on every conversion")
    args = parser.parse_args(argv)

    run_dir = args.collector_output / args.run if args.run else inventory.latest_run(args.collector_output)
    if not (run_dir / "manifest.json").exists():
        raise SystemExit(f"not a collector run directory: {run_dir}")
    inv = inventory.load(run_dir)

    pg = None if args.no_compile else target_mod.PgTarget.from_env(args.pg_dsn)
    if pg is None and not args.no_compile:
        print("postgres target  : not configured -- the compile gate will report blocked")
    result = plan_mod.build(inv, model_mode=args.model_mode, pg_target=pg, approved_by=args.approved_by,
                            compile=not args.no_compile, output_dir=args.output_dir)
    out = plan_mod.write(result, args.output_dir)
    _report(result)
    print(f"\nwritten: {out}")
    print("Nothing was applied. Converted DDL is in convert/output/plpgsql/, one file per object, for review.")
    return 0


def _report(r: dict) -> None:
    print(f"\ncollector_run_id : {r['collector_run_id']}")
    print(f"estate           : {r['estate']}  ->  {r['target_engine']}")
    print(f"objects          : {sum(r['totals'].values())}")
    mode_note = {
        "off": "no model, no stand-ins",
        "static": f"hand-written stand-ins, NOT model output ({r['static_outputs_used']} used)",
        "live": "Bedrock",
    }[r["model_mode"]]
    print(f"model mode       : {r['model_mode']} -- {mode_note}")
    pg = r.get("pg_target")
    print(f"postgres target  : {pg['detail'] if pg else 'not configured'}")

    print("\nOUTCOMES")
    for status in plan_mod.STATUS_ORDER:
        if r["totals"].get(status):
            print(f"  {status:<24} {r['totals'][status]}")

    print("\nOBJECTS")
    for e in r["entries"]:
        tag = f"[{e['status']}]"
        src = f", {e['source']}" if e["source"] else ""
        print(f"\n  {tag:<26} {e['object_type']} {e['owner']}.{e['object_name']}{src}")
        found = ", ".join(c["id"] for c in e["constructs_found"]) or "none"
        print(f"    constructs : {found}")
        if e["conversion"]:
            print(f"    creates    : {', '.join(e['conversion']['creates'])}")
            for g in e["gates"]:
                mark = {"pass": "ok  ", "fail": "FAIL", "blocked": "wait"}[g["status"]]
                print(f"    {g['gate']:<9}: [{mark}] {g['detail'][:110]}")
            not_tr = [c for c in e["conversion"]["constructs"] if c.get("handling") == "not_translated"]
            for c in not_tr:
                print(f"    not translated: {c['oracle']} -- {(c.get('note') or '')[:100]}")
        elif e["reason"]:
            print(f"    reason     : {e['reason'][:140]}")

    if r["shadow_notes"]:
        print("\nSHADOW SCHEMA NOTES")
        for n in r["shadow_notes"]:
            print(f"  - {n}")
    if r["what_is_in_the_way"]:
        print("\nWHAT IS IN THE WAY")
        for item in r["what_is_in_the_way"]:
            print(f"  - {item}")


if __name__ == "__main__":
    raise SystemExit(main())
