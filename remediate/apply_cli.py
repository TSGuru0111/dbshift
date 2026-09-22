"""Apply proven fixes to the rehearsal copy.

Deliberately a separate command from `remediate.run`. Planning is free and can be
run any number of times; this writes and keeps what it writes, and the two should
not share an entry point where a stray flag turns one into the other.

    python -m remediate.apply_cli --check
    python -m remediate.apply_cli --applied-by you@example.com --confirm DBMIG_REHEARSAL
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "remediate"

from . import apply as apply_mod
from .rehearsal import RehearsalTarget

DEFAULT_PLAN = Path(__file__).resolve().parent / "output" / "remediation_plan.json"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="remediate.apply_cli",
        description="Apply proven remediation fixes to the rehearsal copy. Never the source.")
    p.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    p.add_argument("--applied-by", default="",
                   help="the person this is recorded against")
    p.add_argument("--confirm", default="",
                   help="the rehearsal schema name, typed back")
    p.add_argument("--check", action="store_true",
                   help="preflight only -- changes nothing")
    a = p.parse_args(argv)

    if not a.plan.exists():
        print(f"no plan at {a.plan}. Run `python -m remediate.run` first.")
        return 2
    plan = json.loads(a.plan.read_text(encoding="utf-8"))

    target = RehearsalTarget.from_env()
    pre = apply_mod.preflight(plan, target, applied_by=a.applied_by or None)

    print("PREFLIGHT")
    for c in pre["checks"]:
        mark = "ok " if c["ok"] else ("-- " if c.get("advisory") else "XX ")
        print(f"  [{mark}] {c['name']:34} {c['detail']}")
        if c.get("remedy") and not c["ok"]:
            print(f"         -> {c['remedy']}")

    print(f"\n{len(pre['appliable'])} fix(es) would be applied:")
    for e in pre["appliable"]:
        print(f"  + {e['rule_id']:9} {e.get('owner','')}.{e.get('object_name','')}")
    if pre["held_back"]:
        print(f"\n{len(pre['held_back'])} held back. The first few:")
        for h in pre["held_back"][:5]:
            print(f"  - {h['entry']:44} {h['why'][:60]}")

    if a.check:
        print("\n--check: nothing was applied.")
        return 0 if pre["ready"] else 1

    if not pre["ready"]:
        print("\nRefused: " + ", ".join(pre["refused_because"]))
        return 1

    try:
        rec = apply_mod.execute(plan, target, applied_by=a.applied_by,
                                confirm_schema=a.confirm)
    except apply_mod.ApplyRefused as exc:
        print(f"\nRefused: {exc}")
        return 1

    out = a.plan.parent / "apply_record.json"
    out.write_text(json.dumps(rec, indent=2), encoding="utf-8")

    print(f"\nAPPLIED to {rec['target']['schema']}")
    for r in rec["results"]:
        print(f"  [{'ok' if r['applied'] else 'XX'}] {r['entry']}")
        if r.get("error"):
            print(f"       {r['error']}")
    print(f"\napplied {rec['applied_count']}, failed {rec['failed_count']}, "
          f"recorded against {rec['applied_by']}")
    print(rec["where"])
    print(f"written: {out}")
    return 1 if rec["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
