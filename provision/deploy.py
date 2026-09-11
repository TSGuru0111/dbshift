"""Phase 6 deploy -- create the stack the render produced. The one step here that bills.

    python -m provision.deploy --confirm <account-id> --accept-hourly <rate> \
        --price-file <path> [--acknowledge-halt "<reason>"]

Nothing is trusted from an earlier run. Render and every preflight check run
again here, immediately before the create: a plan written this morning describes
this morning's account.

It refuses, before anything is created, when:

  * preflight is not clean
  * --confirm is not the account these credentials resolve to
  * --accept-hourly is not the rate just computed from AWS's price list. The
    operator must have seen the price and typed it; a deploy is never offered
    without one.
  * the gate verdict is HALT and there is no acknowledgement. It is scoped to
    provisioning only, needs a reason, and records who gave it from the
    credentials themselves, never from a typed name.

A failed create is rolled back by CloudFormation (OnFailure=DELETE), so a broken
deploy does not leave half a stack billing.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "provision"

from botocore.exceptions import ClientError

from . import policy
from . import run as run_mod

OUTPUT = run_mod.OUTPUT
MIN_REASON = 15
DEFAULT_PROFILE = run_mod.DEFAULT_PROFILE


class DeployRefused(RuntimeError):
    pass


def identity_name(arn: str) -> str:
    """The person behind an assumed-role ARN -- the session name, e.g. an email."""
    return arn.rsplit("/", 1)[-1]


def _password(length: int = 24) -> str:
    # RDS Oracle master password: 8-30 printable characters, none of / " @ or
    # space. Start with a letter and use only letters, digits, _ and # so it never
    # needs quoting in any client.
    alphabet = string.ascii_letters + string.digits + "_#"
    while True:
        pw = secrets.choice(string.ascii_letters) + "".join(
            secrets.choice(alphabet) for _ in range(length - 1))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


def ensure_password(ssm, name: str, tags: list[dict]) -> str:
    """Create the master password in SSM once. Returns 'created' or 'reused' --
    never the value, which exists only in SSM and in CloudFormation's resolution."""
    try:
        ssm.get_parameter(Name=name)          # existence only; not decrypted
        return "reused"
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ParameterNotFound":
            raise
    ssm.put_parameter(Name=name, Type="SecureString", Value=_password(), Overwrite=False,
                      Description="DBShift RDS master password; resolved by CloudFormation",
                      Tags=tags)
    return "created"


def acknowledge(gate: dict, identity: dict, reason: str | None, now: datetime) -> dict | None:
    """Provision past a HALT only on the record. Returns None when the gate did not halt."""
    if gate["verdict"] != "HALT":
        return None
    if gate["by_phase"]["provision"]["status"] != "clear":
        raise DeployRefused("provision itself is blocked; an acknowledgement cannot cover that -- "
                            "resolve or waive those findings in Phase 5")
    if not reason or len(reason.strip()) < MIN_REASON:
        raise DeployRefused(
            f"the gate verdict is HALT ({len(gate['blockers'])} blocker(s) open). Provisioning past it "
            f"needs --acknowledge-halt with a reason of at least {MIN_REASON} characters. It is recorded "
            "with the identity on these credentials and covers provisioning only.")
    return {
        "scope": "provision only",
        "approved_by": identity_name(identity["Arn"]),
        "principal_arn": identity["Arn"],
        "reason": reason.strip(),
        "at_utc": now.isoformat(),
        "collector_run_id": gate["collector_run_id"],
        "blockers_left_open": [b["rule_id"] for b in gate["blockers"]],
        "does_not": "waive any blocker -- migrate, CDC, validate and cutover remain blocked",
    }


def wait(cfn, stack_id: str, *, emit, poll_seconds: int, timeout_minutes: int,
         sleep=time.sleep, clock=time.monotonic) -> tuple[str, dict, list[dict]]:
    """Follow the stack until it stops moving. Streams every new event, and keeps
    each *_FAILED reason, because that reason is the whole diagnosis."""
    seen: set[str] = set()
    failures: list[dict] = []
    deadline = clock() + timeout_minutes * 60
    while True:
        events = cfn.describe_stack_events(StackName=stack_id)["StackEvents"]   # newest first
        for ev in reversed(events):
            if ev["EventId"] in seen:
                continue
            seen.add(ev["EventId"])
            status, reason = ev["ResourceStatus"], ev.get("ResourceStatusReason") or ""
            if status.endswith("_FAILED"):
                failures.append({"resource": ev["LogicalResourceId"], "status": status, "reason": reason})
            emit({"event": "stack_event", "resource": ev["LogicalResourceId"],
                  "type": ev.get("ResourceType"), "status": status, "reason": reason})
        stack = cfn.describe_stacks(StackName=stack_id)["Stacks"][0]
        if not stack["StackStatus"].endswith("_IN_PROGRESS") or clock() > deadline:
            return stack["StackStatus"], stack, failures
        sleep(poll_seconds)


def _append(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def deploy(session, *, confirm_account: str, accept_hourly: float | None, halt_reason: str | None = None,
           price_file: Path | None = None, operator_cidr: str | None = None, on_event=None,
           poll_seconds: int = 20, timeout_minutes: int = 90, sleep=time.sleep,
           execute=run_mod.execute, now: datetime | None = None) -> dict:
    emit = on_event or (lambda e: None)
    now = now or datetime.now(timezone.utc)

    # ---- every refusal happens before anything is created -------------------------
    plan = execute(session=session, price_file=price_file, operator_cidr=operator_cidr,
                   now=now, on_event=on_event)
    if not plan["ready"]:
        bad = [f"{c['name']}: {c['detail']}" for c in plan["checks"] if c["status"] in ("fail", "blocked")]
        raise DeployRefused("preflight is not clean -- " + ("; ".join(bad) or "nothing rendered"))

    identity = session.client("sts").get_caller_identity()
    if str(confirm_account) != identity["Account"]:
        raise DeployRefused(f"--confirm must be the account these credentials resolve to "
                            f"({identity['Account']}); it was {confirm_account!r}")

    est = (plan.get("cost") or {}).get("estimate")
    if not est:
        raise DeployRefused("no stated cost -- pass --price-file. A deploy is never offered without a price.")
    if accept_hourly is None or abs(float(accept_hourly) - est["instance_per_hour"]) > 1e-9:
        raise DeployRefused(
            f"--accept-hourly must be the rate just computed: {est['instance_per_hour']} USD/hour "
            f"(${est['per_8h_day']} for an 8-hour day, ${est['if_left_running_30_days']} if left "
            f"running 30 days)")

    ack = acknowledge(plan["gate"], identity, halt_reason, now)

    # ---- from here on, things are created ------------------------------------------
    r, p = plan["rendered"], plan["parameters"]
    stack = r["stack_name"]
    who = identity_name(identity["Arn"])
    record = {"stack_name": stack, "requested_at_utc": now.isoformat(), "requested_by": who,
              "account": identity["Account"], "collector_run_id": r["collector_run_id"],
              "cost": est, "acknowledgement": ack}
    # Written before the first billable call, so an interrupted deploy still has a record.
    _append(OUTPUT / "deployments.jsonl", {**record, "event": "requested"})

    tags = [{"Key": "project", "Value": "dbshift"}, {"Key": "estate", "Value": r["estate"]}]
    ssm = session.client("ssm", region_name=policy.REGION)
    emit({"event": "stage", "stage": "password", "detail": r["password_parameter"]})
    record["password"] = ensure_password(ssm, r["password_parameter"], tags)

    # Stack-level tag keys are distinct from the resource tags in the template, so
    # nothing collides when CloudFormation propagates them.
    stack_tags = [{"Key": "dbshift-requested-by", "Value": who}]
    if ack:
        stack_tags.append({"Key": "dbshift-halt-acknowledged-by", "Value": ack["approved_by"]})

    cfn = session.client("cloudformation", region_name=policy.REGION)
    emit({"event": "stage", "stage": "create", "detail": f"{stack} -- billing starts when the instance is up"})
    resp = cfn.create_stack(
        StackName=stack,
        TemplateBody=json.dumps(r["template"]),
        Parameters=[
            {"ParameterKey": "VpcId", "ParameterValue": p["VpcId"]},
            {"ParameterKey": "SubnetIds", "ParameterValue": ",".join(p["SubnetIds"])},
            {"ParameterKey": "OperatorCidr", "ParameterValue": p["OperatorCidr"]},
        ],
        Capabilities=["CAPABILITY_NAMED_IAM"],
        OnFailure="DELETE",
        TimeoutInMinutes=timeout_minutes,
        Tags=stack_tags,
    )
    record["stack_id"] = resp["StackId"]

    status, desc, failures = wait(cfn, resp["StackId"], emit=emit, poll_seconds=poll_seconds,
                                  timeout_minutes=timeout_minutes, sleep=sleep)
    record.update(status=status, failures=failures,
                  outputs={o["OutputKey"]: o["OutputValue"] for o in desc.get("Outputs", [])})
    record["result"] = ("created" if status == "CREATE_COMPLETE"
                        else "still_creating" if status.endswith("_IN_PROGRESS")
                        else "failed_and_removed" if status == "DELETE_COMPLETE"
                        else "failed")
    _append(OUTPUT / "deployments.jsonl", {**record, "event": record["result"]})
    if record["result"] == "created":
        (OUTPUT / "deployed.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DBShift Phase 6 deploy -- this one bills")
    ap.add_argument("--confirm", required=True, help="the account id; must match the credentials")
    ap.add_argument("--accept-hourly", required=True, type=float,
                    help="the instance rate shown by provision.run, typed back")
    ap.add_argument("--acknowledge-halt", default=None,
                    help="reason for provisioning while the gate says HALT (provision only)")
    ap.add_argument("--price-file", type=Path, default=os.environ.get("DBSHIFT_PRICE_FILE"))
    ap.add_argument("--operator-cidr", default=None)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--timeout-minutes", type=int, default=90)
    args = ap.parse_args(argv)

    import boto3
    session = boto3.Session(profile_name=args.profile)

    def show(e):
        if e["event"] == "stack_event":
            reason = f"  {e['reason']}" if e["reason"] else ""
            print(f"  {e['status']:<22} {e['resource']:<20}{reason}")
        elif e["event"] == "stage":
            print(f"[{e['stage']}] {e['detail']}")

    try:
        rec = deploy(session, confirm_account=args.confirm, accept_hourly=args.accept_hourly,
                     halt_reason=args.acknowledge_halt, price_file=args.price_file,
                     operator_cidr=args.operator_cidr, on_event=show,
                     timeout_minutes=args.timeout_minutes)
    except DeployRefused as exc:
        print(f"\nREFUSED -- nothing was created.\n  {exc}")
        return 1

    print(f"\nresult: {rec['result']}   ({rec['status']})")
    if rec["failures"]:
        print("why it failed:")
        for f in rec["failures"]:
            print(f"  {f['resource']}: {f['reason']}")
    if rec["outputs"]:
        print(f"endpoint: {rec['outputs'].get('Endpoint')}:{rec['outputs'].get('Port')}")
    if rec["result"] in ("created", "still_creating"):
        print(f"\nbilling now: ${rec['cost']['instance_per_hour']}/hour plus storage.")
        print(f"verify:     python -m provision.verify")
        print(f"tear down:  python -m killswitch --destroy --confirm {rec['account']}")
    return 0 if rec["result"] == "created" else 2


if __name__ == "__main__":
    raise SystemExit(main())
