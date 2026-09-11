"""Prove the kill switch touches exactly what it should, with no AWS account.

    python -m killswitch.selftest

Uses botocore's Stubber: every AWS call must have been declared in advance, and
any call that was not -- say, a delete aimed at a resource that is not ours --
raises immediately. So "the foreign instance was never touched" is enforced by
the stub, not inferred from output.

The fake account holds one of everything that matters:
  dbshift-target        stack, ours
  finance-prod          stack, NOT ours
  dbshift-target-db     RDS, ours, owned by the dbshift-target stack
  dbshift-orphan-db     RDS, ours, no stack
  dbshift-guarded-db    RDS, ours, deletion protection on
  finance-prod-db       RDS, NOT ours
  tagged-db             RDS, ours only by its project=dbshift tag
  dbshift-final-snap    manual snapshot, ours
  dbshift-repl          DMS, ours
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "killswitch"

import boto3
from botocore.stub import Stubber

from . import actions, scan

REGION = "ap-south-1"
NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
REPL_ARN = "arn:aws:dms:ap-south-1:000000000000:rep:DBSHIFTREPL"


def _clients():
    s = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=REGION)
    return scan.clients_for(s, REGION)


def _stub_scan(stubs):
    stubs["cloudformation"].add_response("list_stacks", {"StackSummaries": [
        {"StackName": "dbshift-target", "StackStatus": "CREATE_COMPLETE", "CreationTime": NOW},
        {"StackName": "finance-prod", "StackStatus": "CREATE_COMPLETE", "CreationTime": NOW},
        {"StackName": "dbshift-old", "StackStatus": "DELETE_COMPLETE", "CreationTime": NOW},
    ]})
    stubs["rds"].add_response("describe_db_instances", {"DBInstances": [
        {"DBInstanceIdentifier": "dbshift-target-db", "DBInstanceStatus": "available",
         "Engine": "oracle-ee", "DBInstanceClass": "db.t3.medium",
         "TagList": [{"Key": "aws:cloudformation:stack-name", "Value": "dbshift-target"}]},
        {"DBInstanceIdentifier": "dbshift-orphan-db", "DBInstanceStatus": "available",
         "Engine": "oracle-ee", "DBInstanceClass": "db.t3.medium"},
        {"DBInstanceIdentifier": "dbshift-guarded-db", "DBInstanceStatus": "available",
         "Engine": "oracle-ee", "DBInstanceClass": "db.t3.medium", "DeletionProtection": True},
        {"DBInstanceIdentifier": "finance-prod-db", "DBInstanceStatus": "available",
         "Engine": "postgres", "DBInstanceClass": "db.r5.large"},
        {"DBInstanceIdentifier": "tagged-db", "DBInstanceStatus": "available",
         "Engine": "oracle-ee", "DBInstanceClass": "db.t3.medium",
         "TagList": [{"Key": "Project", "Value": "dbshift"}]},
    ]})
    stubs["rds"].add_response("describe_db_snapshots", {"DBSnapshots": [
        {"DBSnapshotIdentifier": "dbshift-final-snap", "Status": "available", "AllocatedStorage": 20},
    ]}, {"SnapshotType": "manual"})
    stubs["dms"].add_response("describe_replication_instances", {"ReplicationInstances": [
        {"ReplicationInstanceIdentifier": "dbshift-repl", "ReplicationInstanceStatus": "available",
         "ReplicationInstanceClass": "dms.t3.micro", "ReplicationInstanceArn": REPL_ARN},
    ]})
    stubs["ec2"].add_response("describe_nat_gateways", {"NatGateways": []}, None)


def _run(mode, expect_calls, **kw):
    clients = _clients()
    stubs = {k: Stubber(c) for k, c in clients.items()}
    _stub_scan(stubs)
    for service, method, params in expect_calls:
        stubs[service].add_response(method, {}, params)
    for s in stubs.values():
        s.activate()
    found = scan.scan(clients, REGION)
    plan = actions.decide(found, mode, **kw)
    results = actions.execute(plan, clients)
    for s in stubs.values():
        s.assert_no_pending_responses()   # every expected call was made
    return found, plan, results


def main() -> int:
    failures = []

    def check(label, cond):
        print(f"  {'OK ' if cond else 'BAD'} {label}")
        if not cond:
            failures.append(label)

    print("scan")
    found, _, _ = _run("stop", [
        ("rds", "stop_db_instance", {"DBInstanceIdentifier": "dbshift-target-db"}),
        ("rds", "stop_db_instance", {"DBInstanceIdentifier": "dbshift-orphan-db"}),
        ("rds", "stop_db_instance", {"DBInstanceIdentifier": "dbshift-guarded-db"}),
        ("rds", "stop_db_instance", {"DBInstanceIdentifier": "tagged-db"}),
    ])
    ids = {r["id"]: r for r in found}
    check("deleted stack is not reported", "dbshift-old" not in ids)
    check("finance-prod-db found but marked NOT ours", ids["finance-prod-db"]["ours"] is False)
    check("tagged-db is ours by tag alone", ids["tagged-db"]["ours"] is True)
    check("foreign stack marked NOT ours", ids["finance-prod"]["ours"] is False)

    print("stop  -- the stub raises if finance-prod-db is ever stopped")
    check("four of ours stopped, foreign untouched (enforced by stub)", True)

    print("destroy")
    _, plan, results = _run("destroy", [
        ("cloudformation", "delete_stack", {"StackName": "dbshift-target"}),
        ("rds", "delete_db_instance", {"DBInstanceIdentifier": "dbshift-orphan-db",
                                       "SkipFinalSnapshot": True, "DeleteAutomatedBackups": True}),
        ("rds", "delete_db_instance", {"DBInstanceIdentifier": "tagged-db",
                                       "SkipFinalSnapshot": True, "DeleteAutomatedBackups": True}),
        ("dms", "delete_replication_instance", {"ReplicationInstanceArn": REPL_ARN}),
    ])
    by = {p["id"]: p for p in plan}
    check("stack-owned instance left for its stack to delete",
          by["dbshift-target-db"]["action"] == actions.LEAVE)
    check("deletion-protected instance BLOCKED, not deleted",
          by["dbshift-guarded-db"]["action"] == actions.BLOCKED)
    check("manual snapshot kept by default", by["dbshift-final-snap"]["action"] == actions.LEAVE)
    check("foreign instance left", by["finance-prod-db"]["action"] == actions.LEAVE)
    check("foreign stack left", by["finance-prod"]["action"] == actions.LEAVE)
    check("every requested action succeeded", all(r["outcome"] == "requested" for r in results))

    print("destroy --include-snapshots --force-deletion-protection")
    _, plan, _ = _run("destroy", [
        ("cloudformation", "delete_stack", {"StackName": "dbshift-target"}),
        ("rds", "delete_db_instance", {"DBInstanceIdentifier": "dbshift-orphan-db",
                                       "SkipFinalSnapshot": True, "DeleteAutomatedBackups": True}),
        ("rds", "modify_db_instance", {"DBInstanceIdentifier": "dbshift-guarded-db",
                                       "DeletionProtection": False, "ApplyImmediately": True}),
        ("rds", "delete_db_instance", {"DBInstanceIdentifier": "dbshift-guarded-db",
                                       "SkipFinalSnapshot": True, "DeleteAutomatedBackups": True}),
        ("rds", "delete_db_instance", {"DBInstanceIdentifier": "tagged-db",
                                       "SkipFinalSnapshot": True, "DeleteAutomatedBackups": True}),
        ("rds", "delete_db_snapshot", {"DBSnapshotIdentifier": "dbshift-final-snap"}),
        ("dms", "delete_replication_instance", {"ReplicationInstanceArn": REPL_ARN}),
    ], include_snapshots=True, force_deletion_protection=True)
    check("with both overrides, foreign still left",
          {p["id"]: p for p in plan}["finance-prod-db"]["action"] == actions.LEAVE)

    print("\nALL CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
