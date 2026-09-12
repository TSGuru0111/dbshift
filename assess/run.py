from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "assess"

from . import engine, loader, scoring

log = logging.getLogger("assess")

DEFAULT_COLLECTOR_OUTPUT = Path(__file__).resolve().parent.parent / "collector" / "output"
DEFAULT_ASSESS_OUTPUT = Path(__file__).resolve().parent / "output"


def _latest_run(output_dir: Path) -> Path:
    runs = [d for d in output_dir.iterdir() if d.is_dir() and (d / "manifest.json").exists()]
    if not runs:
        raise SystemExit(f"no collector runs found in {output_dir}")
    runs.sort(
        key=lambda d: json.loads((d / "manifest.json").read_text(encoding="utf-8"))["started_at_utc"]
    )
    return runs[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBShift assessment engine")
    parser.add_argument("--run", type=str, default=None, help="collector_run_id")
    parser.add_argument("--collector-output", type=Path, default=DEFAULT_COLLECTOR_OUTPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_ASSESS_OUTPUT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    run_dir = args.collector_output / args.run if args.run else _latest_run(args.collector_output)
    if not (run_dir / "manifest.json").exists():
        raise SystemExit(f"not a collector run directory: {run_dir}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    db_path = args.output_dir / "assessment.sqlite"

    loaded = loader.load_run(run_dir, db_path)
    log.info("loaded %d rows across %d tables", loaded["total_rows"], len(loaded["tables"]))

    conn = sqlite3.connect(db_path)
    try:
        rules = engine.load_rules()
        engine.install_rules(conn, rules)
        findings, rule_errors = engine.evaluate(conn)
    finally:
        conn.close()

    scores = scoring.score_findings(findings)
    owners = {f["owner"] for f in findings if f["owner"]}
    recall = scoring.score_against_answer_key(findings, owners=owners)
    groups = engine.group_findings(findings)

    assessment = {
        "collector_run_id": loaded["collector_run_id"],
        "assessed_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": loaded["source"],
        "rules_evaluated": len(rules),
        "rule_errors": rule_errors,
        "scores": scores,
        "answer_key": recall,
        "issues": groups,
        "findings": findings,
    }
    out = args.output_dir / "assessment.json"
    out.write_text(json.dumps(assessment, indent=2, ensure_ascii=False), encoding="utf-8")

    _report(assessment)
    print(f"\nwritten: {out}")
    if rule_errors:
        print(f"RULE ERRORS: {len(rule_errors)}")
        for e in rule_errors:
            print(f"  {e['rule_id']}: {e['error']}")
        return 1
    return 0


def _report(a: dict) -> None:
    s, k = a["scores"], a["answer_key"]
    print(f"\ncollector_run_id : {a['collector_run_id']}")
    print(f"rules evaluated  : {a['rules_evaluated']}")
    print(f"issues           : {len(a['issues'])}  ({s['total_findings']} occurrences)")

    print("\nSCORES")
    for category, c in s["by_category"].items():
        bar = "#" * (c["score"] // 5)
        print(f"  {category:<20} {c['score']:>3}  {bar:<20} {c['findings']} findings")
    cap = "  (capped by critical finding)" if s["capped_by_critical"] else ""
    print(f"  {'OVERALL':<20} {s['overall_score']:>3}{cap}")

    print("\nSEVERITY")
    for sev, n in s["by_severity"].items():
        if n:
            print(f"  {sev:<10} {n}")

    print("\nREMEDIATION LEVEL")
    for lvl, n in s["by_remediation_level"].items():
        if n:
            print(f"  {lvl} {n}")

    print("\nISSUES  (one row per rule, not per object)")
    for g in a["issues"]:
        count = f"x{g['occurrences']}" if g["occurrences"] > 1 else "  "
        print(
            f"  {g['severity']:<8} {g['rule_id']:<9} {count:>4}  {g['title'][:52]:<52} "
            f"{g['remediation_level']}"
        )

    # score_against_answer_key returns applicable=False when the key's reference
    # estate is not in this database. Everything below assumes a real recall
    # figure, and k["recall"] is None in that case, so print the reason and stop.
    if not k.get("applicable", True):
        print("\nANSWER KEY")
        print(f"  not applicable: {k['reason']}")
        print(f"  Findings:        {k['additional_findings']}  (all triage pending)")
        return

    print(f"\nANSWER KEY  (docs/04-defects.md)")
    for d in k["defects"]:
        if d["detected"]:
            mark = "OK  "
        elif d["not_present_in_source"]:
            mark = "N/A "
        else:
            mark = "MISS"
        sev = ",".join(d["reported_severities"]) or "-"
        note = "" if not d["detected"] else ("" if d["severity_matches"] else f"  severity {sev}, expected {d['expected_severity']}")
        print(f"  [{mark}] {d['defect']} {d['title']:<34} {d['object'][:34]:<34}{note}")

    for d in k["defects"]:
        if d["not_present_in_source"] and not d["detected"]:
            print(f"\n  N/A defect {d['defect']}: not present in the source data.")
            print(f"    {d['note']}")

    print(f"\n  Detected:        {k['detected']} of {k['detectable']} detectable   (recall {k['recall']:.0%})")
    print(f"  Severity exact:  {k['severity_exact']} of {k['detected']}")
    print(f"  Missed:          {k['missed']}")
    if k["not_present_in_source"]:
        print(f"  Not in source:   {k['not_present_in_source']}  (seeding failed; excluded from recall)")
    print(f"  Additional:      {k['additional_findings']}  (not seeded defects; triage pending)")


if __name__ == "__main__":
    raise SystemExit(main())
