"""Phase 4 over AWS SCT's action items.

    python -m remediate.sct_run                      # plan, no model, nothing runs
    python -m remediate.sct_run --model live         # let the model draft fixes
    python -m remediate.sct_run --approve you@x.com  # hold approval for the gates
    python -m remediate.sct_run --target rds-oracle  # a different SCT run

Reads the newest SCT assessment on disk, routes every item through
`sct/route.py`, drafts what may be drafted, and runs each draft through the
gates for **the engine it targets** -- Oracle's narrow allow-list for source
fixes, `remediate/pg_policy.py` for target fixes.

Nothing is applied. `remediate/apply.py` is the only writer in this package and
is not wired to this path.

Environment:
    DBSHIFT_REHEARSAL_DSN / _PASSWORD   the Oracle rehearsal copy, for source fixes
    DBSHIFT_PG_DSN / _USER / _PASSWORD  the PostgreSQL target, for target fixes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "remediate"

from sct import parse as sct_parse
from sct import route as sct_route
from sct import runner as sct_runner

from . import sct_plan

OUTPUT = Path(__file__).resolve().parent / "output"


def _record_engine(record_path: Path) -> str:
    """The source engine an SCT record is about. Records before 2026-09-29 carry
    none and are all Oracle."""
    try:
        rec = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(rec.get("source_engine") or "ORACLE").upper()


def newest_sct_record(target: str = "", source_engine: str = ""
                      ) -> tuple[dict, Path] | tuple[None, None]:
    """The most recent SCT assessment on disk, optionally for one target and one
    source engine. Without the engine filter the newest record wins whatever it
    assessed -- a MySQL report planned as though it were the Oracle estate."""
    if not sct_runner.OUTPUT.is_dir():
        return None, None
    candidates = []
    for d in sct_runner.OUTPUT.iterdir():
        record = d / "sct_assessment.json"
        if not record.exists():
            continue
        if target and not d.name.endswith(target):
            continue
        if source_engine and _record_engine(record) != source_engine.upper():
            continue
        candidates.append(record)
    if not candidates:
        return None, None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(newest.read_text(encoding="utf-8")), newest
    except (OSError, json.JSONDecodeError):
        return None, None


def bind_to_run(record: dict, run_id: str) -> str:
    """Tie an SCT record to a discovery run of the same estate, or refuse.

    SCT reads the source directly, so its record carries whichever discovery
    run was current when it ran; a later discovery of the same schema is the
    same estate. Checked by schema, never assumed -- binding one estate's SCT
    report to another's discovery is exactly the mismatch Phase 6 refuses.
    """
    manifest = Path(__file__).resolve().parent.parent / "collector" / "output" / run_id / "manifest.json"
    try:
        schemas = (json.loads(manifest.read_text(encoding="utf-8")).get("schemas") or {})
    except (OSError, ValueError):
        raise SystemExit(f"collector run {run_id} has no manifest")
    present = {str(s).lower() for s in (schemas.get("present") or schemas.get("configured") or [])}
    wanted = {str(s).lower() for s in ((record.get("source") or {}).get("schemas") or [])}
    if wanted and not wanted <= present:
        raise SystemExit(f"collector run {run_id} discovered {sorted(present)}, but this SCT record "
                         f"assessed {sorted(wanted)}; refusing to bind them")
    return run_id


def _rehearsal_target():
    """The Oracle rehearsal copy, if configured. Source fixes are proven on it."""
    try:
        from . import rehearsal
        return rehearsal.RehearsalTarget.from_env(None)
    except Exception:  # noqa: BLE001 -- absence is reported by the gate, not here
        return None


def _pg_target():
    """The PostgreSQL target, if configured. Target fixes are proven on it."""
    try:
        from convert.target import PgTarget
        return PgTarget.from_env()
    except Exception:  # noqa: BLE001
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", default="", help="SCT target id, e.g. rds-postgresql")
    ap.add_argument("--source-engine", default="",
                    help="ORACLE or MYSQL; only records for that source are considered")
    ap.add_argument("--collector-run", default="",
                    help="bind the SCT record to this discovery run of the same estate")
    ap.add_argument("--model", default="off", choices=["off", "live"],
                    help="whether the model drafts fixes for items with no template")
    ap.add_argument("--approve", default="", metavar="EMAIL",
                    help="hold approval for every gated fix, recorded against this person")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    args = ap.parse_args(argv)

    record, path = newest_sct_record(args.target, args.source_engine)
    if record is None:
        print("No AWS SCT assessment on disk. Run `python -m sct.run` first.",
              file=sys.stderr)
        return 2

    csvs = sct_runner.artefact_paths(record).get("csv") or []
    if not csvs:
        print(f"{path} carries no SCT CSV, so there are no action items to remediate.",
              file=sys.stderr)
        return 2

    parsed = sct_parse.parse_csv_file(csvs[0])
    engine = str(record.get("source_engine") or "ORACLE").upper()
    assessment = {
        "issues": sct_route.annotate(parsed["issues"], engine),
        "target": record.get("target") or {},
        "source_engine": engine,
        "collector_run_id": (bind_to_run(record, args.collector_run) if args.collector_run
                             else record.get("collector_run_id")),
    }

    rehearsal_target, pg_target = _rehearsal_target(), _pg_target()
    approvals = {}
    if args.approve:
        # One approver for every item in this run. Per-item approval is the
        # console's job; a CLI flag that silently approved everything without
        # naming somebody would defeat the gate.
        for issue in assessment["issues"]:
            approvals[str(issue.get("issue_code"))] = args.approve

    plan = sct_plan.build(
        assessment, model_mode=args.model,
        rehearsal_target=rehearsal_target, pg_target=pg_target, approvals=approvals,
    )

    OUTPUT.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT / "sct_remediation_plan.json"
    out_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")

    if args.json:
        print(json.dumps(plan, indent=2, default=str))
        return 0

    t = plan["totals"]
    print(f"AWS SCT remediation plan -- {plan['source_engine']} -> "
          f"{plan['target'] or 'unknown target'}")
    print(f"from {path}")
    print(f"model tier: {args.model}"
          f" | rehearsal: {'configured' if rehearsal_target else 'not configured'}"
          f" | postgres: {'configured' if pg_target else 'not configured'}\n")

    for where in (sct_route.SOURCE, sct_route.TARGET,
                  sct_route.DECISION, sct_route.HUMAN):
        items = [e for e in plan["entries"] if e["where"] == where]
        if not items:
            continue
        print(f"== {sct_route.WHERE_LABEL[where]}  ({len(items)})")
        print(f"   {sct_route.WHERE_MEANING[where]}")
        for e in items:
            print(f"   {e['issue_code']:5} {e['who_label']:18} {e['status']:24} "
                  f"x{e['occurrences']}")
            print(f"         {e['title'][:82]}")
            if e.get("sql"):
                print(f"         SQL      : {e['sql'][:76]}")
                print(f"         rollback : {(e.get('rollback_sql') or '')[:76]}")
            if e.get("reason"):
                print(f"         because  : {e['reason'][:76]}")
        print()

    print(f"{t['items']} action items: {t['ready']} ready, {t['blocked']} blocked, "
          f"{t['rejected']} rejected, {t['needs_a_person']} need a person")
    print(f"{t['with_statement']} carry a statement. Nothing has been applied.")
    print(f"\nRecord: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
