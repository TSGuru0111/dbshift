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
    __package__ = "sizing"

from assess import loader

from . import facts as facts_mod
from . import propose as propose_mod
from . import validate as validate_mod

log = logging.getLogger("sizing")

DEFAULT_COLLECTOR_OUTPUT = Path(__file__).resolve().parent.parent / "collector" / "output"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "output"


def _latest_run(output_dir: Path) -> Path:
    runs = [d for d in output_dir.iterdir() if d.is_dir() and (d / "manifest.json").exists()]
    if not runs:
        raise SystemExit(f"no collector runs found in {output_dir}")
    runs.sort(
        key=lambda d: json.loads((d / "manifest.json").read_text(encoding="utf-8"))["started_at_utc"]
    )
    return runs[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBShift size and edition decision")
    parser.add_argument("--run", type=str, default=None, help="collector_run_id")
    parser.add_argument("--collector-output", type=Path, default=DEFAULT_COLLECTOR_OUTPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--bedrock", action="store_true", help="use the Bedrock proposer")
    parser.add_argument("--model-id", type=str, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    run_dir = args.collector_output / args.run if args.run else _latest_run(args.collector_output)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    db_path = args.output_dir / "sizing.sqlite"

    loaded = loader.load_run(run_dir, db_path)
    conn = sqlite3.connect(db_path)
    try:
        facts = facts_mod.extract(conn)
    finally:
        conn.close()

    proposal = propose_mod.propose(facts, use_bedrock=args.bedrock, model_id=args.model_id)
    decision = validate_mod.validate(proposal, facts)

    out = {
        "collector_run_id": loaded["collector_run_id"],
        "decided_at_utc": datetime.now(timezone.utc).isoformat(),
        "facts": facts,
        "proposal": proposal,
        "decision": decision,
    }
    path = args.output_dir / "sizing.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    _report(out)
    print(f"\nwritten: {path}")
    return 0


def _report(out: dict) -> None:
    p, d, f = out["proposal"], out["decision"], out["facts"]

    print(f"\ncollector_run_id : {out['collector_run_id']}")
    print(f"estate           : {f['segment_gb']} GB, {f['table_count']} user tables "
          f"({f['total_table_count']} incl. internals), {f['object_count']} objects, "
          f"{f['character_set']}")

    print(f"\nPROPOSAL  (source: {p['source']}{', model ' + p['model_id'] if p['model_id'] else ''})")
    print(f"  edition        : {p['edition']}")
    print(f"  instance       : {p['instance_class']}")
    print(f"  storage        : {p['storage_gb']} GB")
    print(f"  rationale      : {p['rationale']}")

    print("\nVALIDATION  (rules engine -- overrides the proposal where they disagree)")
    for c in d["checks"]:
        mark = {"PASS": "ok  ", "OVERRIDE": "OVER", "WARN": "warn"}[c["verdict"]]
        print(f"  [{mark}] {c['check']}")
        print(f"         {c['detail']}")

    print(f"\nDECISION")
    print(f"  edition        : {d['edition']} ({d['licence_model']})")
    print(f"  instance       : {d['instance_class']}  {d['vcpu']} vCPU / {d['memory_gib']} GiB")
    print(f"  storage        : {d['storage_gb']} GB {d['storage_type']}")
    print(f"  character set  : {d['character_set']}  (target must be created with this)")
    if d["licence_model"] == "BYOL":
        print(f"  licences       : {d['processor_licences']} Oracle processor licence(s) "
              f"({d['vcpu']} vCPU / 2)")

    if d["forced_by"]:
        print("\n  Edition forced by:")
        for x in d["forced_by"]:
            print(f"    - {x['feature']}  [{x['evidence']}]  {x['why']}")
    if d["dismissed"]:
        print("\n  Detected but NOT edition-forcing:")
        for x in d["dismissed"]:
            print(f"    - {x['feature']}: {x['why_not_forcing']}")

    print(f"\n  overrides: {d['override_count']}   warnings: {d['warning_count']}   "
          f"proposal accepted as-is: {d['agreed_with_proposal']}")


if __name__ == "__main__":
    raise SystemExit(main())
