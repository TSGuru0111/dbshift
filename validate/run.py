"""Phase 8 -- validate the target against the estate it was built from. Read-only.

    python -m validate.run                 # all five levels
    python -m validate.run --no-checksum   # skip level 4, which reads every row twice

Needs the read-only collector login for the source side:
    DBSHIFT_COLLECTOR_PASSWORD

The target must be running. It is left exactly as found: every statement Phase 8
issues is a SELECT.
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
    __package__ = "validate"

from provision import policy as prov_policy
from provision import run as provision_run

from . import context as C
from .levels import LEVELS

OUTPUT = Path(__file__).resolve().parent / "output"
REPORT = OUTPUT / "validation_report.json"


def execute(session, opts: C.Options, on_event=None) -> dict:
    events: list[dict] = []

    def emit(e):
        events.append(e)
        if on_event:
            on_event(e)

    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "levels": [], "findings": []}
    ctx = C.Ctx(session=session, opts=opts, plan=C.load_plan(), emit=emit)
    try:
        estate = ctx.resolve_estate()
        report.update(estate)
        emit({"event": "estate", **estate})
        if estate["status"] != "available":
            report.update(status="stopped", reason=f"the target is {estate['status']}; start it to validate")
            return _save(report, events)

        stack = ctx.plan["stack_name"]
        s = session.client("cloudformation", region_name=prov_policy.REGION).describe_stacks(
            StackName=stack)["Stacks"][0]
        ctx.state["outputs"] = {o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])}

        # Reach the target before claiming to validate anything against it.
        reach = C.reachability(ctx)
        emit({"event": "log", "level": 0, "line": reach["detail"]})
        if not reach["ok"]:
            if reach.get("remedy"):
                emit({"event": "log", "level": 0, "line": reach["remedy"]})
            report.update(status="unreachable", reason=reach["detail"], remedy=reach.get("remedy"),
                          allowed=reach.get("allowed"), this_machine=reach.get("this_machine"))
            return _save(report, events)

        # What Phase 4 set aside for this phase, from the plan of the same run.
        plan_path = provision_run.OUTPUT.parent / "remediate" / "output" / "remediation_plan.json"
        artefacts = []
        if plan_path.exists():
            rem = json.loads(plan_path.read_text(encoding="utf-8"))
            if rem.get("collector_run_id") == ctx.run_id:
                artefacts = [e for e in rem.get("entries", [])
                             if (e.get("artefact") or {}).get("applies_to_phase") == "validate"]
            else:
                emit({"event": "log", "level": 0,
                      "line": f"Phase 4's plan describes collector run {rem.get('collector_run_id')}, not "
                              f"{ctx.run_id}; its checks for this phase are not applied"})
        ctx.state["validate_artefacts"] = artefacts

        for number, title, why, fn in LEVELS:
            ctx.level = number
            emit({"event": "level_start", "level": number, "title": title, "why": why})
            started = time.monotonic()
            try:
                found = fn(ctx)
            except Exception as exc:  # noqa: BLE001 -- a level failing is a result
                found = [C.finding(number, title.lower(), C.NOT_COMPARABLE,
                                   str(exc).splitlines()[0] or type(exc).__name__)]
            elapsed = int((time.monotonic() - started) * 1000)
            counts = {v: sum(1 for f in found if f["verdict"] == v)
                      for v in (C.MATCH, C.EXPECTED, C.MISMATCH, C.NOT_COMPARABLE)}
            report["levels"].append({"level": number, "title": title, "elapsed_ms": elapsed, **counts})
            report["findings"] += found
            emit({"event": "level_done", "level": number, "title": title, "elapsed_ms": elapsed,
                  "findings": found, **counts})

        mismatches = [f for f in report["findings"] if f["verdict"] == C.MISMATCH]
        unreadable = [f for f in report["findings"] if f["verdict"] == C.NOT_COMPARABLE]
        report["mismatches"], report["not_comparable"] = len(mismatches), len(unreadable)
        # "Validated" means every level compared both sides and agreed. A run that
        # could not read one side has validated nothing, and the first version of
        # this said "validated" after failing to connect to the target eleven
        # times -- a check that cannot fail is not a check.
        report["status"] = ("mismatch" if mismatches
                            else "incomplete" if unreadable
                            else "validated")
    except Exception as exc:  # noqa: BLE001
        report.update(status="error", reason=str(exc).splitlines()[0] or type(exc).__name__)
    finally:
        ctx.close()
    return _save(report, events)


def _save(report: dict, events: list[dict]) -> dict:
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["log"] = [e for e in events if e["event"] == "log"][-2000:]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # Every run keeps its own report. Phase 7 overwrote one file and lost the
    # record of a successful run when a later one stopped early.
    (OUTPUT / "runs").mkdir(exist_ok=True)
    stamp = report["started_at_utc"].replace(":", "").replace("-", "")[:15]
    (OUTPUT / "runs" / f"{stamp}-{report.get('status', 'unknown')}.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")
    REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def last_report() -> dict | None:
    return json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DBShift Phase 8 -- validate. Read-only.")
    ap.add_argument("--no-checksum", action="store_true", help="skip level 4")
    ap.add_argument("--profile", default=provision_run.DEFAULT_PROFILE)
    args = ap.parse_args(argv)

    import boto3
    opts = C.Options(collector_password=os.environ.get("DBSHIFT_COLLECTOR_PASSWORD"),
                     collector_user=os.environ.get("DBSHIFT_COLLECTOR_USER", "dbmig_collector"),
                     source_dsn=os.environ.get("DBSHIFT_DSN", "localhost:1521/XEPDB1"),
                     checksum=not args.no_checksum)

    def show(e):
        if e["event"] == "estate":
            print(f"comparing collector run {e['run_id']} ({e['estate']}), target {e['status']}")
        elif e["event"] == "level_start":
            print(f"\n=== Level {e['level']}: {e['title']} -- {e['why']}")
        elif e["event"] == "log":
            print(f"    {e['line']}")
        elif e["event"] == "level_done":
            for f in e["findings"]:
                mark = {"match": "OK  ", "expected_difference": "EXP ", "mismatch": "DIFF",
                        "not_comparable": "??  "}[f["verdict"]]
                print(f"  [{mark}] {f['check']}: {f['detail']}")
                if f["why"]:
                    print(f"           why: {f['why'][:150]}")

    report = execute(boto3.Session(profile_name=args.profile), opts, on_event=show)
    print(f"\nvalidation: {report.get('status')}"
          + (f" -- {report.get('reason')}" if report.get("reason") else "")
          + (f" · {report.get('mismatches')} mismatch(es)" if report.get("mismatches") else "")
          + (f" · {report.get('not_comparable')} check(s) could not be made"
             if report.get("not_comparable") else ""))
    return 0 if report.get("status") == "validated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
