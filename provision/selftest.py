"""Prove the deploy refuses what it should and creates what it should -- no AWS.

    python -m provision.selftest

The plan is canned, and every AWS call goes through botocore's Stubber, which
raises on any call that was not declared. So "a refusal creates nothing" is
enforced by the stub: a refused deploy that reached put_parameter or
create_stack would fail the test, not just print something wrong.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "provision"

import boto3
from botocore.stub import ANY, Stubber

from . import deploy as d

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
ARN = "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_DBA_x/approver@example.com"
STACK = "dbshift-target-dbmig-app"
STACK_ID = f"arn:aws:cloudformation:ap-south-1:111122223333:stack/{STACK}/abc"
PW_PARAM = f"/dbshift/{STACK}/master-password"


def _plan(verdict="HALT", ready=True):
    return {
        "ready": ready,
        "checks": [] if ready else [{"name": "engine_version", "status": "fail", "detail": "none orderable"}],
        "gate": {"verdict": verdict, "collector_run_id": "run-1",
                 "by_phase": {"provision": {"status": "clear", "blocked_by": []}},
                 "blockers": [{"rule_id": "OPS-001"}, {"rule_id": "RDS-004"}] if verdict == "HALT" else []},
        "cost": {"estimate": {"instance_per_hour": 0.098, "per_8h_day": 0.87,
                              "if_left_running_30_days": 74.16}},
        "parameters": {"VpcId": "vpc-1", "SubnetIds": ["subnet-a", "subnet-b"], "OperatorCidr": "203.0.113.7/32"},
        "rendered": {"stack_name": STACK, "estate": "DBMIG_APP", "collector_run_id": "run-1",
                     "password_parameter": PW_PARAM, "template": {"Resources": {}}},
    }


class Session:
    """Real boto3 clients with stubs attached; any undeclared call raises."""
    def __init__(self):
        s = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="ap-south-1")
        self.clients = {n: s.client(n, region_name="ap-south-1") for n in ("sts", "ssm", "cloudformation")}
        self.stubs = {n: Stubber(c) for n, c in self.clients.items()}
        self.stubs["sts"].add_response("get_caller_identity",
                                       {"Account": "111122223333", "Arn": ARN, "UserId": "x"})

    def client(self, name, region_name=None):
        return self.clients[name]

    def __enter__(self):
        for s in self.stubs.values():
            s.activate()
        return self

    def __exit__(self, *exc):
        for s in self.stubs.values():
            s.deactivate()


def _deploy(sess, plan, **kw):
    args = dict(confirm_account="111122223333", accept_hourly=0.098,
                halt_reason="provision only; blockers affect CDC, cutover and one external table",
                execute=lambda **_: plan, sleep=lambda s: None, now=NOW, poll_seconds=0)
    args.update(kw)
    return d.deploy(sess, **args)


def _refused(label, plan, **kw):
    sess = Session()
    with sess:
        try:
            _deploy(sess, plan, **kw)
            return label, False, "was NOT refused"
        except d.DeployRefused as exc:
            return label, True, str(exc)[:100]


def _event(eid, resource, status, reason=""):
    return {"StackId": STACK_ID, "EventId": eid, "StackName": STACK, "LogicalResourceId": resource,
            "ResourceStatus": status, "ResourceStatusReason": reason, "Timestamp": NOW}


def _stack(status, outputs=None):
    return {"Stacks": [{"StackName": STACK, "StackId": STACK_ID, "CreationTime": NOW,
                        "StackStatus": status, "Outputs": outputs or []}]}


def _expect_create(sess, halt_tag=True):
    tags = [{"Key": "dbshift-requested-by", "Value": "approver@example.com"}]
    if halt_tag:
        tags.append({"Key": "dbshift-halt-acknowledged-by", "Value": "approver@example.com"})
    sess.stubs["cloudformation"].add_response("create_stack", {"StackId": STACK_ID}, {
        "StackName": STACK, "TemplateBody": ANY,
        "Parameters": [{"ParameterKey": "VpcId", "ParameterValue": "vpc-1"},
                       {"ParameterKey": "SubnetIds", "ParameterValue": "subnet-a,subnet-b"},
                       {"ParameterKey": "OperatorCidr", "ParameterValue": "203.0.113.7/32"}],
        "Capabilities": ["CAPABILITY_NAMED_IAM"], "OnFailure": "DELETE",
        "TimeoutInMinutes": 90, "Tags": tags})


def main() -> int:
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'OK ' if ok else 'BAD'} {label}" + (f"  -- {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    print("refusals -- the stub raises if any of these reaches SSM or CloudFormation")
    for label, ok, why in [
        _refused("preflight not clean", _plan(ready=False)),
        _refused("wrong account in --confirm", _plan(), confirm_account="999999999999"),
        _refused("--accept-hourly not the computed rate", _plan(), accept_hourly=0.05),
        _refused("HALT with no acknowledgement", _plan(), halt_reason=None),
        _refused("HALT with a throwaway reason", _plan(), halt_reason="ok go"),
    ]:
        check(label, ok, why)

    print("happy path under HALT, new password")
    sess = Session()
    sess.stubs["ssm"].add_client_error("get_parameter", "ParameterNotFound", expected_params={"Name": PW_PARAM})
    sess.stubs["ssm"].add_response("put_parameter", {"Version": 1}, {
        "Name": PW_PARAM, "Type": "SecureString", "Value": ANY, "Overwrite": False,
        "Description": ANY, "Tags": [{"Key": "project", "Value": "dbshift"}, {"Key": "estate", "Value": "DBMIG_APP"}]})
    _expect_create(sess)
    cfn = sess.stubs["cloudformation"]
    cfn.add_response("describe_stack_events", {"StackEvents": [_event("e1", STACK, "CREATE_IN_PROGRESS")]},
                     {"StackName": STACK_ID})
    cfn.add_response("describe_stacks", _stack("CREATE_IN_PROGRESS"), {"StackName": STACK_ID})
    cfn.add_response("describe_stack_events", {"StackEvents": [
        _event("e3", STACK, "CREATE_COMPLETE"), _event("e2", "DbInstance", "CREATE_COMPLETE"),
        _event("e1", STACK, "CREATE_IN_PROGRESS")]}, {"StackName": STACK_ID})
    cfn.add_response("describe_stacks", _stack("CREATE_COMPLETE", [
        {"OutputKey": "Endpoint", "OutputValue": "db.example.rds.amazonaws.com"},
        {"OutputKey": "Port", "OutputValue": "1521"}]), {"StackName": STACK_ID})
    seen = []
    buf = io.StringIO()
    with sess, redirect_stdout(buf):
        rec = _deploy(sess, _plan(), on_event=seen.append)
    for s in sess.stubs.values():
        s.assert_no_pending_responses()
    check("stack created", rec["result"] == "created", rec["status"])
    check("acknowledgement names the credential holder, provision scope only",
          rec["acknowledgement"]["approved_by"] == "approver@example.com"
          and rec["acknowledgement"]["scope"] == "provision only")
    check("blockers stay open -- nothing waived",
          rec["acknowledgement"]["blockers_left_open"] == ["OPS-001", "RDS-004"])
    check("password created, and its value appears nowhere in the record or events",
          rec["password"] == "created" and "Value" not in str(rec) + str(seen))
    check("each event streamed once", [e["resource"] for e in seen if e["event"] == "stack_event"]
          == [STACK, "DbInstance", STACK])
    check("endpoint read from outputs", rec["outputs"].get("Endpoint") == "db.example.rds.amazonaws.com")

    print("failure path: CloudFormation rolls back, the reason is kept")
    sess = Session()
    sess.stubs["ssm"].add_response("get_parameter", {"Parameter": {"Name": PW_PARAM, "Type": "SecureString"}},
                                   {"Name": PW_PARAM})
    _expect_create(sess)
    cfn = sess.stubs["cloudformation"]
    cfn.add_response("describe_stack_events", {"StackEvents": [
        _event("f3", STACK, "DELETE_COMPLETE"),
        _event("f2", "DbInstance", "CREATE_FAILED", "Cannot find version 19.0.0.0.x for oracle-ee"),
        _event("f1", STACK, "CREATE_IN_PROGRESS")]}, {"StackName": STACK_ID})
    cfn.add_response("describe_stacks", _stack("DELETE_COMPLETE"), {"StackName": STACK_ID})
    with sess, redirect_stdout(buf):
        rec = _deploy(sess, _plan())
    check("existing password reused, not overwritten", rec["password"] == "reused")
    check("result says it failed AND was removed", rec["result"] == "failed_and_removed")
    check("the failure reason is kept", rec["failures"] and "Cannot find version" in rec["failures"][0]["reason"])

    print("\nALL CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
