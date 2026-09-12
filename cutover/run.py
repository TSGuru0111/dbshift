"""Phase 9 -- the cutover certificate, its approval, and the steps it authorises.

    python -m cutover.run                                   # build the certificate (read-only)
    python -m cutover.run --approve "<why this is acceptable>"
    python -m cutover.run --execute --confirm <account-id>   # run the cutover steps on the target

Building a certificate changes nothing and can be done at any time. Approving
records a named person against exactly what they accepted. Executing runs only
the target-side steps Phase 4 set aside -- it does not repoint applications,
which stays the owner's step and is said so on the certificate.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "cutover"

from provision import policy as prov_policy
from provision import records
from provision import run as provision_run
from provision.deploy import identity_name

from . import requirements as R

OUTPUT = Path(__file__).resolve().parent / "output"
CERTIFICATE = OUTPUT / "certificate.json"
APPROVALS = OUTPUT / "approvals.jsonl"
MIN_REASON = 15
# Only these may run from the Phase 4 plan, as in Phase 7. A cutover step is a
# scheduler or refresh call, never arbitrary SQL arriving from a file.
ALLOWED_SQL = ("BEGIN DBMS_SCHEDULER.", "BEGIN DBMS_MVIEW.")


def _approvals() -> list[dict]:
    if not APPROVALS.exists():
        return []
    return [json.loads(line) for line in APPROVALS.read_text(encoding="utf-8").splitlines() if line.strip()]


def approval_for(run_id: str | None) -> dict | None:
    """The most recent approval for this exact estate. An approval is not a
    standing permission: it names one collector run."""
    for entry in reversed(_approvals()):
        if entry.get("collector_run_id") == run_id:
            return entry
    return None


def certificate(session, *, on_event=None) -> dict:
    emit = on_event or (lambda e: None)
    recs = records.load()
    plan = json.loads((provision_run.OUTPUT / "provision_plan.json").read_text(encoding="utf-8"))
    run = R.target_run(session, plan)
    emit({"event": "estate", **run})
    ack = approval_for(run.get("run_id"))
    reqs = R.build(recs, plan, run, ack)
    for req in reqs:
        emit({"event": "requirement", **req})
    cert = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "stack_name": plan.get("stack_name"),
        "estate": run.get("estate") or R.records.estate_of(recs["assessment"]),
        "collector_run_id": run.get("run_id"),
        "run_id_source": run.get("source"),
        "target_status": run.get("status"),
        "requirements": reqs,
        "approval": ack,
        "ready": R.is_ready(reqs),
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    CERTIFICATE.write_text(json.dumps(cert, indent=2, default=str), encoding="utf-8")
    (OUTPUT / "runs").mkdir(exist_ok=True)
    stamp = cert["built_at_utc"].replace(":", "").replace("-", "")[:15]
    (OUTPUT / "runs" / f"{stamp}-{'ready' if cert['ready'] else 'not-ready'}.json").write_text(
        json.dumps(cert, indent=2, default=str), encoding="utf-8")
    return cert


def approve(session, reason: str) -> dict:
    """Record who accepts the cutover, and exactly what they are accepting."""
    if not reason or len(reason.strip()) < MIN_REASON:
        raise ValueError(f"an approval needs a reason of at least {MIN_REASON} characters; it is "
                         "recorded against your identity and read by whoever asks later why this "
                         "was signed off")
    cert = certificate(session)
    identity = session.client("sts").get_caller_identity()
    unmet = [r for r in cert["requirements"] if r["status"] == R.UNMET and r["id"] != "gate"]
    if unmet:
        raise PermissionError(
            "approving cannot paper over an unmet requirement: "
            + "; ".join(f"{r['id']} -- {r['detail']}" for r in unmet)
            + ". The gate's blockers are the only thing an approval may accept.")
    gate = cert["requirements"][2]
    entry = {
        "approved_by": identity_name(identity["Arn"]),
        "principal_arn": identity["Arn"],
        "account": identity["Account"],
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "collector_run_id": cert["collector_run_id"],
        "stack_name": cert["stack_name"],
        "reason": reason.strip(),
        "accepting": gate["evidence"].get("blocked_by", []),
        "scope": "cutover of this estate onto this target, accepting the blockers listed",
        "does_not": "repoint any application; the source stays authoritative until someone does",
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with APPROVALS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def execute(session, *, confirm_account: str, on_event=None) -> dict:
    """Run the target-side steps a ready, approved certificate authorises."""
    emit = on_event or (lambda e: None)
    cert = certificate(session, on_event=on_event)
    identity = session.client("sts").get_caller_identity()
    if str(confirm_account) != identity["Account"]:
        raise PermissionError(f"--confirm must be {identity['Account']}, the account these credentials "
                              f"resolve to; it was {confirm_account!r}")
    if not cert["ready"]:
        unmet = [f"{r['id']}: {r['detail']}" for r in cert["requirements"] if r["status"] == R.UNMET]
        raise PermissionError("the certificate is not ready -- " + "; ".join(unmet))
    if not cert["approval"]:
        raise PermissionError("no approval on record for this estate. Run --approve first; it names "
                              "who accepted the cutover and what they accepted.")

    steps = next(r for r in cert["requirements"] if r["id"] == "steps")["evidence"].get("steps", [])
    import oracledb
    plan = json.loads((provision_run.OUTPUT / "provision_plan.json").read_text(encoding="utf-8"))
    pw = session.client("ssm", region_name=prov_policy.REGION).get_parameter(
        Name=plan["rendered"]["password_parameter"], WithDecryption=True)["Parameter"]["Value"]
    cfn = session.client("cloudformation", region_name=prov_policy.REGION)
    out = {o["OutputKey"]: o["OutputValue"] for o in
           cfn.describe_stacks(StackName=cert["stack_name"])["Stacks"][0].get("Outputs", [])}
    conn = oracledb.connect(user=prov_policy.MASTER_USERNAME, password=pw,
                            dsn=f"{out['Endpoint']}:{out['Port']}/{prov_policy.DB_NAME}")
    done, failed = [], []
    try:
        for step in steps:
            sql = (step.get("sql_on_target") or "").strip()
            label = f"{step['rule_id']} {step.get('object')}"
            if not sql.upper().startswith(ALLOWED_SQL):
                failed.append(f"{label}: refused -- only scheduler and refresh calls may run from the plan")
                continue
            emit({"event": "step", "detail": f"{label}: {' '.join(sql.split())[:160]}"})
            try:
                conn.cursor().execute(sql)
                done.append(label)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{label}: {str(exc).splitlines()[0]}")
    finally:
        conn.close()

    record = {"at_utc": datetime.now(timezone.utc).isoformat(), "stack_name": cert["stack_name"],
              "collector_run_id": cert["collector_run_id"], "executed_by": identity_name(identity["Arn"]),
              "approval": cert["approval"], "applied": done, "failed": failed,
              "status": "cut_over" if not failed else "cut_over_with_failures"}
    with (OUTPUT / "cutovers.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return record


def _print(cert: dict) -> None:
    mark = {R.MET: "OK  ", R.WAIVED: "WAIV", R.NOT_APPLICABLE: "N/A ", R.UNMET: "NO  "}
    print(f"\nstack   : {cert['stack_name']}   estate {cert['estate']}   target {cert['target_status']}")
    print(f"estate  : collector run {str(cert['collector_run_id'])[:8]} (from {cert['run_id_source']})")
    print("\nREADINESS")
    for r in cert["requirements"]:
        print(f"  [{mark[r['status']]}] {r['title']}")
        print(f"         {r['detail']}")
        if r["remedy"] and r["status"] == R.UNMET:
            print(f"         -> {r['remedy']}")
        for gap in r["evidence"].get("gaps", []):
            print(f"         - {gap}")
        for step in r["evidence"].get("steps", []):
            if isinstance(step, dict):
                print(f"         - {step['rule_id']} {step.get('object')}: {step.get('when')}")
            else:
                print(f"         - {step}")
    if cert["approval"]:
        a = cert["approval"]
        print(f"\napproved by {a['approved_by']} at {a['at_utc'][:16]}Z, accepting "
              f"{', '.join(a['accepting']) or 'nothing outstanding'}")
        print(f"  \"{a['reason']}\"")
    print(f"\nready to cut over: {cert['ready']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DBShift Phase 9 -- cutover certificate")
    ap.add_argument("--approve", metavar="REASON", default=None,
                    help="record approval for this estate, against your identity")
    ap.add_argument("--execute", action="store_true", help="run the cutover steps on the target")
    ap.add_argument("--confirm", default=None, help="account id, required with --execute")
    ap.add_argument("--profile", default=provision_run.DEFAULT_PROFILE)
    ap.add_argument("--no-aws", action="store_true", help="build the certificate without AWS")
    args = ap.parse_args(argv)

    session = None
    if not args.no_aws:
        import boto3
        session = boto3.Session(profile_name=args.profile)

    try:
        if args.approve:
            entry = approve(session, args.approve)
            print(f"approval recorded for {entry['approved_by']}, accepting "
                  f"{', '.join(entry['accepting']) or 'nothing outstanding'}")
        if args.execute:
            record = execute(session, confirm_account=args.confirm,
                             on_event=lambda e: print(f"  {e.get('detail', e)}"))
            print(f"\ncutover: {record['status']}; applied {len(record['applied'])}, "
                  f"failed {len(record['failed'])}")
            for f in record["failed"]:
                print(f"  FAILED {f}")
            return 0 if record["status"] == "cut_over" else 2
    except (PermissionError, ValueError) as exc:
        print(f"REFUSED -- nothing was changed.\n  {exc}", file=sys.stderr)
        return 1

    cert = certificate(session)
    _print(cert)
    print(f"\nwritten: {CERTIFICATE}")
    if not cert["ready"]:
        print("Cutover is not authorised. Each unmet requirement above says what would clear it.")
    return 0 if cert["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
