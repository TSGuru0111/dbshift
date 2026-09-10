from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "collector"

from . import config
from .db import Session, connect

# Ground truth from docs/03-source-estate.md, checked against the live database
# rather than trusted. A mismatch is reported as drift, not silently accepted.
DOCUMENTED_OBJECTS = 90
DOCUMENTED_CONSTRAINTS = 51  # post-defect-seeding; "ORDER" adds an inline PK

# The reference estate this ships against. Override for any other database --
# the counts above are ground truth for that estate only, so they are reported
# as drift rather than treated as universal expectations.
PRIMARY_SCHEMA = os.environ.get("DBSHIFT_PRIMARY_SCHEMA", "DBMIG_APP")


def _load(run_dir: Path, name: str) -> dict:
    path = run_dir / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"missing dataset file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _recent_runs(output_dir: Path, count: int) -> list[Path]:
    dirs = [d for d in output_dir.iterdir() if d.is_dir() and (d / "manifest.json").exists()]
    dirs.sort(key=lambda d: json.loads((d / "manifest.json").read_text(encoding="utf-8"))["started_at_utc"])
    return dirs[-count:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify two collector runs against ground truth")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--run", action="append", dest="runs", default=None)
    args = parser.parse_args(argv)

    cfg = config.load(args.output_dir)
    if args.runs:
        run_dirs = [cfg.output_dir / r for r in args.runs]
    else:
        run_dirs = _recent_runs(cfg.output_dir, 2)
    if len(run_dirs) < 2:
        raise SystemExit(f"need two runs to compare, found {len(run_dirs)} in {cfg.output_dir}")

    first, second = run_dirs[-2], run_dirs[-1]
    manifests = [json.loads((d / "manifest.json").read_text(encoding="utf-8")) for d in (first, second)]
    checks: list[tuple[str, bool, str]] = []

    # 1 -- distinct run ids
    ids = [m["collector_run_id"] for m in manifests]
    checks.append(
        ("two distinct collector_run_id values", ids[0] != ids[1], f"{ids[0]}  /  {ids[1]}")
    )

    # 2 & 3 -- object and constraint counts, reconciled against a live query
    connection = connect(cfg)
    session = Session(connection=connection)
    try:
        live_objects = session.fetch(
            "verify.live_object_count",
            "SELECT COUNT(*) AS n FROM dba_objects WHERE owner = :o",
            {"o": PRIMARY_SCHEMA},
        )[0]["n"]
        live_constraints = session.fetch(
            "verify.live_constraint_count",
            "SELECT COUNT(*) AS n FROM dba_constraints WHERE owner = :o",
            {"o": PRIMARY_SCHEMA},
        )[0]["n"]
        constraint_breakdown = session.fetch(
            "verify.constraint_breakdown",
            """SELECT constraint_type, COUNT(*) AS n FROM dba_constraints
               WHERE owner = :o GROUP BY constraint_type ORDER BY constraint_type""",
            {"o": PRIMARY_SCHEMA},
        )
    finally:
        connection.close()

    collected_objects = [
        sum(1 for r in _load(d, "objects")["rows"] if r["owner"] == PRIMARY_SCHEMA)
        for d in (first, second)
    ]
    collected_constraints = [
        sum(1 for r in _load(d, "constraints")["rows"] if r["owner"] == PRIMARY_SCHEMA)
        for d in (first, second)
    ]

    checks.append(
        (
            f"object count reconciles to live dba_objects ({live_objects})",
            collected_objects[0] == collected_objects[1] == live_objects,
            f"run1={collected_objects[0]} run2={collected_objects[1]} live={live_objects}",
        )
    )
    checks.append(
        (
            f"constraint count reconciles to live dba_constraints ({live_constraints})",
            collected_constraints[0] == collected_constraints[1] == live_constraints,
            f"run1={collected_constraints[0]} run2={collected_constraints[1]} live={live_constraints}",
        )
    )

    # 4 -- PL/SQL hashes stable across runs
    hashes = [
        {
            (r["owner"], r["object_type"], r["object_name"]): r["source_sha256"]
            for r in _load(d, "plsql_source")["rows"]
        }
        for d in (first, second)
    ]
    drifted = [k for k in hashes[0] if hashes[0][k] != hashes[1].get(k)]
    checks.append(
        (
            "PL/SQL SHA-256 identical across both runs",
            not drifted and hashes[0].keys() == hashes[1].keys() and len(hashes[0]) > 0,
            f"{len(hashes[0])} objects hashed, {len(drifted)} drifted",
        )
    )

    # 5 -- feature usage statistics actually queried, evidenced by the manifest
    queried = [
        any(q["label"] == "features.usage_statistics" and q["error"] is None for q in m["query_log"])
        for m in manifests
    ]
    usage_rows = [_load(d, "feature_usage")["row_count"] for d in (first, second)]
    checks.append(
        (
            "DBA_FEATURE_USAGE_STATISTICS queried in both runs",
            all(queried) and all(n > 0 for n in usage_rows),
            f"queried={queried} rows={usage_rows}",
        )
    )

    print(f"\nrun 1 : {first.name}")
    print(f"run 2 : {second.name}\n")
    width = max(len(label) for label, _, _ in checks)
    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label.ljust(width)}   {detail}")

    print("\nconstraint breakdown (live):")
    for row in constraint_breakdown:
        print(f"    {row['constraint_type']}  {row['n']}")

    print("\nagainst docs/03-source-estate.md:")
    for label, documented, actual in (
        ("objects", DOCUMENTED_OBJECTS, live_objects),
        ("constraints", DOCUMENTED_CONSTRAINTS, live_constraints),
    ):
        verdict = "matches" if documented == actual else f"DRIFT -- doc says {documented}"
        print(f"    {label:<12} documented={documented:<4} actual={actual:<4} {verdict}")

    failed = [label for label, ok, _ in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
