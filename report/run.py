from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "report"

from . import build as build_mod, render as render_mod

OUTPUT = Path(__file__).resolve().parent / "output"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBShift Phase 10 -- the migration assessment report "
                                                 "(SCT-style conversion assessment + DMS pre-migration assessment)")
    parser.add_argument("--run-dir", type=Path, default=None, help="collector run directory (default: the assessment's run)")
    parser.add_argument("--records", type=Path, default=None,
                        help="root holding the phase records (default: the repo; e.g. telco-output for the second estate)")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)

    data = build_mod.build_from_disk(args.run_dir, root=args.records)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "migration_report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    html = args.output_dir / "migration_report.html"
    html.write_text(render_mod.render(data), encoding="utf-8")

    sc, dms, cs = data["schema_conversion"], data["dms_assessment"], data["schema_conversion"]["code_summary"]
    print(f"estate           : {data['estate']}  run {data['collector_run_id'][:8]}")
    if cs.get("ran"):
        print(f"stored code      : {cs['automatic']} of {cs['convertible']} converted automatically ({cs['pct_automatic']}%), "
              f"{cs['needs_model']} need the model tier, {cs['manual']} need a person, {cs['excluded']} broken on the source")
    else:
        print("stored code      : Phase 4b not run")
    bc = sc["by_complexity"]
    print(f"action items     : {sum(bc.values())} -- {bc['simple']} simple, {bc['medium']} medium, {bc['complex']} complex, {bc['decision']} decisions")
    t = dms["totals"]
    print(f"DMS checks       : {t.get('pass', 0)} pass, {t.get('warning', 0)} warning, {t.get('fail', 0)} fail, {t.get('info', 0)} info")
    print(f"replication      : full load {dms['path']['full_load']}, CDC {dms['path']['cdc']}")
    print(f"\nwritten: {html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
