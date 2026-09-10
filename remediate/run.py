from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "remediate"

from . import plan as plan_mod

DEFAULT_ASSESS_OUTPUT = Path(__file__).resolve().parent.parent / "assess" / "output"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "output"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DBShift remediation planner -- plans fixes, applies nothing"
    )
    parser.add_argument("--assessment", type=Path, default=DEFAULT_ASSESS_OUTPUT / "assessment.json")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--allow-model",
        action="store_true",
        help="permit Bedrock generation for findings with no template (not yet wired)",
    )
    parser.add_argument(
        "--rehearsal-dsn",
        type=str,
        default=os.environ.get("DBSHIFT_REHEARSAL_DSN"),
        help="a restored copy of the estate to dry-run fixes against",
    )
    args = parser.parse_args(argv)

    if not args.assessment.exists():
        raise SystemExit(f"no assessment at {args.assessment} -- run 'python -m assess.run' first")
    assessment = json.loads(args.assessment.read_text(encoding="utf-8"))

    from . import rehearsal as rehearsal_mod
    target = rehearsal_mod.RehearsalTarget.from_env(args.rehearsal_dsn)
    if target:
        check = rehearsal_mod.check_target(target)
        print(f"rehearsal target : {check['detail']}")
        if not check['ok']:
            print('  the dry-run gate will report blocked')
            target = None

    result = plan_mod.build(
        assessment, allow_model=args.allow_model, rehearsal_target=target
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / "remediation_plan.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    _report(result)
    print(f"\nwritten: {out}")
    print("Nothing was applied. This phase plans; applying is a separate step that needs the "
          "rehearsal database.")
    return 0


def _report(r: dict) -> None:
    print(f"\ncollector_run_id : {r['collector_run_id']}")
    print(f"findings planned : {sum(r['totals'].values())}")
    print(f"fixes with SQL   : {r['fixes_with_sql']}")
    print(f"model generation : {'enabled' if r['model_generation_enabled'] else 'disabled'}")
    print(f"rehearsal        : {'in use' if r['rehearsal_configured'] else 'not configured'}")

    print("\nOUTCOMES")
    order = ["AUTO_APPLY", "READY_TO_APPLY", "BLOCKED", "REJECTED",
             "MANUAL_ACTION_REQUIRED", "NOT_A_FIX"]
    for status in order:
        if r["totals"].get(status):
            print(f"  {status:<24} {r['totals'][status]}")

    with_sql = [e for e in r["entries"] if e["sql"]]
    if with_sql:
        print("\nPROPOSED FIXES")
        for e in with_sql:
            print(f"\n  [{e['status']}] {e['rule_id']} - {e['owner']}.{e['object_name']} "
                  f"({e['remediation_level']}, {e['source']})")
            print(f"    fix      : {e['sql'][:104]}")
            print(f"    rollback : {e['rollback_sql'][:104]}")
            failed = [g for g in e["gates"] if g["status"] != "pass"]
            for g in failed:
                print(f"    {g['gate']:<9}: {g['status'].upper()} -- {g['detail'][:90]}")

    if r["what_is_in_the_way"]:
        print("\nWHAT IS IN THE WAY")
        for item in r["what_is_in_the_way"]:
            print(f"  - {item}")


if __name__ == "__main__":
    raise SystemExit(main())
