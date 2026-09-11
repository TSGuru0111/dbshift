"""Phase 7 -- run the migration steps in order, stopping at the first that cannot pass.

    python -m migrate.run                              # uses a VERSION=19 dump if one exists
    python -m migrate.run --resolve-external-via-s3    # choose the RDS-004 route (recorded)
    python -m migrate.run --fresh-export               # export again first

Passwords come from the environment and are held in memory only:
    DBSHIFT_SOURCE_OWNER_PASSWORD   the schema owner, only for a fresh export
    DBSHIFT_COLLECTOR_PASSWORD      read-only: source row counts, external file lookup

Every step's start, log lines and result are written to
migrate/output/migration_run.json -- the same stream the console shows.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "migrate"

from provision import records
from provision import run as provision_run

from . import steps as S

OUTPUT = Path(__file__).resolve().parent / "output"
RECORD = OUTPUT / "migration_run.json"


def plan() -> list[dict]:
    return [{"id": s.id, "title": s.title, "why": s.why, "decided_by": s.decided_by,
             "touches": s.touches, "reversible": s.reversible, "conditional": s.when is not None}
            for s in S.STEPS]


def last_run() -> dict | None:
    return json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.exists() else None


def execute(session, opts: S.Options, on_event=None) -> dict:
    events: list[dict] = []

    def emit(e):
        events.append(e)
        if on_event:
            on_event(e)

    prov_plan_path = provision_run.OUTPUT / "provision_plan.json"
    record = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "steps": [], "status": "running",
              "options": {"resolve_external_via_s3": opts.resolve_external_via_s3,
                          "fresh_export": opts.fresh_export}}
    if not prov_plan_path.exists():
        record.update(status="stopped", stopped_at="records", reason="Phase 6 has not rendered a target")
        return _save(record, events)

    ctx = S.Ctx(session=session, opts=opts, records=records.load(),
                plan=json.loads(prov_plan_path.read_text(encoding="utf-8")), emit=emit)
    record["collector_run_id"] = ctx.run_id
    # Resuming skips the work already done, never the checks: records, target and
    # gate run every time, because what they check can have changed since.
    always = {"records", "target", "gate"}
    resuming = opts.resume_from
    record["options"]["resume_from"] = opts.resume_from
    try:
        for step in S.STEPS:
            meta = {"id": step.id, "title": step.title, "decided_by": step.decided_by}
            if resuming and step.id not in always:
                if step.id == resuming:
                    resuming = None
                else:
                    done = {**meta, "status": S.SKIP, "elapsed_ms": 0,
                            "detail": f"skipped -- this run resumes from '{opts.resume_from}'"}
                    emit({"event": "step_done", **done})
                    record["steps"].append(done)
                    continue
            if step.when is not None and not step.when(ctx):
                done = {**meta, "status": S.SKIP, "detail": "not chosen for this run", "elapsed_ms": 0}
                emit({"event": "step_done", **done})
                record["steps"].append(done)
                continue
            ctx.step_id = step.id
            emit({"event": "step_start", **meta})
            started = time.monotonic()
            try:
                res = step.fn(ctx)
            except Exception as exc:  # noqa: BLE001 -- a step failing is a result, not a crash
                res = S.result(S.FAIL, str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
            done = {**meta, **res, "elapsed_ms": int((time.monotonic() - started) * 1000)}
            emit({"event": "step_done", **done})
            record["steps"].append(done)
            if res["status"] in (S.FAIL, S.STOP):
                record.update(status="stopped", stopped_at=step.id, reason=res["detail"])
                break
        else:
            record["status"] = ("completed_with_warnings"
                                if any(s["status"] == S.WARN for s in record["steps"]) else "completed")
    finally:
        ctx.close()
    record["approvals"] = ctx.approvals
    return _save(record, events)


def _save(record: dict, events: list[dict]) -> dict:
    record["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    record["log"] = [e for e in events if e["event"] == "step_log"][-2000:]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DBShift Phase 7 -- migrate")
    ap.add_argument("--resolve-external-via-s3", action="store_true")
    ap.add_argument("--fresh-export", action="store_true")
    ap.add_argument("--from", dest="resume_from", default=None,
                    help="resume from this step id; records, target and gate still run first")
    ap.add_argument("--profile", default=provision_run.DEFAULT_PROFILE)
    args = ap.parse_args(argv)

    import boto3
    opts = S.Options(source_owner_password=os.environ.get("DBSHIFT_SOURCE_OWNER_PASSWORD"),
                     collector_password=os.environ.get("DBSHIFT_COLLECTOR_PASSWORD"),
                     collector_user=os.environ.get("DBSHIFT_COLLECTOR_USER", "dbmig_collector"),
                     source_dsn=os.environ.get("DBSHIFT_DSN", "localhost:1521/XEPDB1"),
                     resolve_external_via_s3=args.resolve_external_via_s3, fresh_export=args.fresh_export,
                     resume_from=args.resume_from)

    def show(e):
        if e["event"] == "step_start":
            print(f"\n=== {e['title']}  [decided by {e['decided_by']}]")
        elif e["event"] == "step_log":
            print(f"    {e['line']}")
        elif e["event"] == "step_done":
            print(f"--- {e['status'].upper()}  {e['detail']}  ({e['elapsed_ms']} ms)")

    rec = execute(boto3.Session(profile_name=args.profile), opts, on_event=show)
    print(f"\nmigration: {rec['status']}" + (f" at '{rec.get('stopped_at')}': {rec.get('reason')}"
                                              if rec["status"] == "stopped" else ""))
    return 0 if rec["status"].startswith("completed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
