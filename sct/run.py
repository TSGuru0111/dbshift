"""Phase 2, SCT path -- run AWS SCT and keep its own PDF and CSV.

    python -m sct.run --check                    # are the prerequisites there?
    python -m sct.run --plan                     # what would run. Free, no password
    python -m sct.run --target rds-postgresql    # run SCT for that target
    python -m sct.run --target rds-oracle        # ...and the homogeneous one
    python -m sct.run --list-targets             # what the dropdown offers

The password comes from the environment, never a flag:
    DBSHIFT_COLLECTOR_PASSWORD

`--check` and `--plan` need no password and touch nothing, which is the point:
the prerequisites are three manual installs, and a run that fails deep inside a
Java stack trace because one is missing tells a client nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sct"

from . import parse, runner, targets, toolchain

PASSWORD_ENV = "DBSHIFT_COLLECTOR_PASSWORD"
DEFAULT_DSN = "localhost:1521/XEPDB1"
DEFAULT_USER = "dbmig_collector"
DEFAULT_SCHEMAS = ("DBMIG_APP",)


def _schemas() -> list[str]:
    raw = os.environ.get("DBSHIFT_SCHEMAS")
    if raw:
        names = [n.strip().upper() for n in raw.split(",") if n.strip()]
        if names:
            return names
    return list(DEFAULT_SCHEMAS)


def _latest_collector_run() -> str:
    """The newest collector run id, so a report is tied to an inventory."""
    out = Path(__file__).resolve().parent.parent / "collector" / "output"
    if not out.is_dir():
        return ""
    runs = sorted((p for p in out.iterdir() if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0].name if runs else ""


def cmd_list_targets() -> int:
    print("Targets AWS SCT can assess:\n")
    for t in targets.for_console():
        scope = "in scope" if t["in_scope"] else "OUT OF SCOPE"
        star = " (default)" if t["default"] else ""
        print(f"  {t['id']:20} {t['label']:34} [{scope}]{star}")
        print(f"  {'':20} {t['scope_note']}\n")
    return 0


def cmd_check() -> int:
    tc = toolchain.discover()
    print("AWS SCT prerequisites:\n")
    for line in toolchain.summary_lines(tc):
        print(line)
    print()
    if tc.ready:
        print("Ready. `python -m sct.run --target rds-postgresql` will run SCT.")
        return 0
    print("Not ready. Install what is marked above, then re-run --check.")
    return 1


def cmd_plan(args) -> int:
    p = runner.plan(
        target_id=args.target, dsn=args.dsn, user=args.user,
        schemas=_schemas(), collector_run_id=args.collector_run or _latest_collector_run(),
    )
    print(json.dumps(p, indent=2))
    return 0 if p["ready"] else 1


def cmd_assess(args) -> int:
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        print(f"{PASSWORD_ENV} is not set. Export the read-only collector password.",
              file=sys.stderr)
        return 2

    target = targets.get(args.target)
    if not target["in_scope"]:
        # Allowed, because a client asks for the comparison -- but never silently.
        print(f"NOTE: {target['label']} is out of DBShift's migration scope.")
        print(f"      {target['scope_note']}")
        print("      SCT will assess it for comparison; it is not a migration path.\n")

    schemas = _schemas()
    print(f"AWS SCT -> {target['label']}  ({target['sct_platform']})")
    print(f"source  :  {args.user}@{args.dsn}  schemas: {', '.join(schemas)}\n")

    rec = runner.assess(
        target_id=args.target, dsn=args.dsn, user=args.user, password=password,
        schemas=schemas, collector_run_id=args.collector_run or _latest_collector_run(),
        on_line=lambda line: print(f"  {line}"),
        reuse_cached=not args.force,
        timeout=args.timeout,
    )

    if rec.get("from_cache"):
        print("  (cached result for this run and target; --force to re-run)")

    if not rec.get("ok"):
        print(f"\nSCT did not complete: "
              f"{rec.get('reason') or rec.get('host', {}).get('detail')}", file=sys.stderr)
        # SCT's own errors are the actionable part. Its exit code is not: it
        # returns 0 with a failed AddSource and an empty report.
        for err in rec.get("sct_errors") or []:
            print(f"  SCT error: {err}", file=sys.stderr)
        if rec.get("exit_code_said_ok") and not rec.get("artefacts"):
            print("\n  (SCT exited 0 but produced no report -- an empty assessment is "
                  "not a clean estate.)", file=sys.stderr)
        for line in toolchain.summary_lines(toolchain.discover()):
            print(line, file=sys.stderr)
        return 1

    arte = runner.artefact_paths(rec)
    print(f"\nSCT produced {len(rec['artefacts'])} file(s) in {rec['report_dir']}")
    for kind in ("pdf", "csv"):
        for path in arte[kind]:
            print(f"  {kind.upper():4} {Path(path).name}")

    for csv_path in arte["csv"]:
        parsed = parse.parse_csv_file(csv_path)
        print(f"\nAction items from {Path(csv_path).name}: {parsed['action_item_count']} "
              f"({parsed['occurrence_count']} occurrences)")
        for bucket in parse.COMPLEXITY_ORDER:
            n = parsed["by_complexity"][bucket]
            occ = parsed["occurrences_by_complexity"][bucket]
            print(f"  {bucket:9} {n:4} items, {occ:6} occurrences"
                  f"   -- {parse.COMPLEXITY_MEANING[bucket]}")
        if parsed["unmapped_columns"]:
            print(f"  columns this parser did not understand: {parsed['unmapped_columns']}")
        break

    print(f"\nRecord: {Path(rec['output_dir']) / 'sct_assessment.json'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", default=targets.DEFAULT_TARGET_ID,
                    help="target platform id (see --list-targets)")
    ap.add_argument("--dsn", default=os.environ.get("DBSHIFT_DSN", DEFAULT_DSN))
    ap.add_argument("--user", default=os.environ.get("DBSHIFT_COLLECTOR_USER", DEFAULT_USER))
    ap.add_argument("--collector-run", default="", help="tie the report to a collector run id")
    ap.add_argument("--timeout", type=int, default=None, help="seconds before SCT is killed")
    ap.add_argument("--force", action="store_true", help="re-run even if a result is cached")
    ap.add_argument("--check", action="store_true", help="report the prerequisites and exit")
    ap.add_argument("--plan", action="store_true", help="what would run; free, no password")
    ap.add_argument("--list-targets", action="store_true")
    args = ap.parse_args(argv)

    if args.list_targets:
        return cmd_list_targets()
    if args.check:
        return cmd_check()

    try:
        targets.get(args.target)
    except targets.UnknownTarget as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.plan:
        return cmd_plan(args)
    return cmd_assess(args)


if __name__ == "__main__":
    raise SystemExit(main())
