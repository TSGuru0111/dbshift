from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "blocker"

from . import gate as gate_mod

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ASSESSMENT = ROOT / "assess" / "output" / "assessment.json"
DEFAULT_REMEDIATION = ROOT / "remediate" / "output" / "remediation_plan.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "output"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBShift blocker gate")
    parser.add_argument("--assessment", type=Path, default=DEFAULT_ASSESSMENT)
    parser.add_argument("--remediation", type=Path, default=DEFAULT_REMEDIATION)
    parser.add_argument(
        "--waivers", type=Path, default=None,
        help="JSON list of {rule_id, approved_by, reason} accepting specific blockers",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    if not args.assessment.exists():
        raise SystemExit(f"no assessment at {args.assessment} -- run 'python -m assess.run' first")
    assessment = json.loads(args.assessment.read_text(encoding="utf-8"))
    remediation = (
        json.loads(args.remediation.read_text(encoding="utf-8"))
        if args.remediation.exists() else None
    )
    waivers = json.loads(args.waivers.read_text(encoding="utf-8")) if args.waivers else []

    decision = gate_mod.evaluate(assessment, remediation, waivers)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / "gate_decision.json"
    out.write_text(json.dumps(decision, indent=2, ensure_ascii=False), encoding="utf-8")

    _report(decision)
    print(f"\nwritten: {out}")
    # Non-zero on HALT so a pipeline stops here without needing to parse the JSON.
    return 1 if decision["verdict"] == gate_mod.HALT else 0


def _report(d: dict) -> None:
    print(f"\ncollector_run_id : {d['collector_run_id']}")
    print(f"critical findings: {d['critical_findings']}")
    print(f"\nVERDICT: {d['verdict']}")
    print(f"  {d['summary']}")

    if d["blockers"]:
        print("\nBLOCKERS")
        for b in d["blockers"]:
            mark = "" if b["known_blast_radius"] else "  [blast radius unknown -- blocks everything]"
            print(f"\n  {b['rule_id']} x{b['occurrences']} -- {b['title']}{mark}")
            print(f"    blocks      : {', '.join(b['blocks'])}")
            print(f"    why         : {b['why'][:150]}")
            print(f"    clears when : {b['clears_when'][:130]}")
            if b["objects"]:
                print(f"    objects     : {', '.join(str(o) for o in b['objects'][:5])}")
            if b["fix_drafted"]:
                print("    a fix is already drafted for this rule in the remediation plan")

    if d["waived"]:
        print("\nWAIVED")
        for w in d["waived"]:
            print(f"  {w['rule_id']} -- accepted by {w['waiver']['approved_by']}")
            print(f"    reason: {w['waiver']['reason'][:120]}")

    if d["rejected_waivers"]:
        print("\nREJECTED WAIVERS")
        for w in d["rejected_waivers"]:
            print(f"  {w.get('rule_id','?')}: {'; '.join(w['rejected_because'])}")

    print("\nDOWNSTREAM PHASES")
    for phase, v in d["by_phase"].items():
        detail = f"blocked by {', '.join(v['blocked_by'])}" if v["blocked_by"] else "clear"
        print(f"  {phase:<20} {v['status']:<8} {detail}")


if __name__ == "__main__":
    raise SystemExit(main())
