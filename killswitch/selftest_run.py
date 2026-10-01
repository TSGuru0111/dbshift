"""Prove one failing region cannot block killswitch.run.execute() from finding
or acting on resources in a healthy one.

    python -m killswitch.selftest_run

This is the region loop in run.py itself -- selftest.py covers scan.py and
actions.py for one region, but nothing previously exercised what happens when
`regions` (every region this project has ever been pointed at, via
awsregion.used()) includes one that cannot be reached at all. On 2026-10-01 a
stray region a person had once clicked through on the region picker --
af-south-1, an opt-in region the account had never enabled -- answered every
call with `InvalidClientTokenId`, which reads exactly like an expired session.
Because the region loop had no per-region error handling, that one region
failed the *entire* scan, so a real stack billing in a perfectly healthy
region (ap-south-1) could never be found, let alone destroyed -- the kill
switch appeared to simply do nothing.
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

from . import run as kill_run

GOOD_REGION, BAD_REGION = "ap-south-1", "af-south-1"
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _clients_for(region: str) -> dict:
    s = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=region)
    return {n: s.client(n, region_name=region) for n in ("cloudformation", "rds", "dms", "ec2", "s3")}


class FakeSession:
    """One shared `sts` identity, and an independently stubbed client set per
    region, so a call that is stubbed to fail in BAD_REGION is never the same
    client object as the one stubbed to succeed in GOOD_REGION."""

    def __init__(self, sts, by_region: dict):
        self._sts = sts
        self._by_region = by_region

    def client(self, name, region_name=None):
        return self._sts if name == "sts" else self._by_region[region_name][name]


def _stub_healthy_region(clients: dict, *, stacks: list[dict]) -> None:
    """Every call scan.scan() makes against a reachable region, answered once."""
    cfn_stub = Stubber(clients["cloudformation"])
    cfn_stub.add_response("list_stacks", {"StackSummaries": stacks})
    cfn_stub.activate()
    rds_stub = Stubber(clients["rds"])
    rds_stub.add_response("describe_db_instances", {"DBInstances": []})
    rds_stub.add_response("describe_db_snapshots", {"DBSnapshots": []}, {"SnapshotType": "manual"})
    rds_stub.activate()
    dms_stub = Stubber(clients["dms"])
    dms_stub.add_response("describe_replication_instances", {"ReplicationInstances": []})
    dms_stub.activate()
    ec2_stub = Stubber(clients["ec2"])
    ec2_stub.add_response("describe_instances", {"Reservations": []}, None)
    ec2_stub.add_response("describe_nat_gateways", {"NatGateways": []}, None)
    ec2_stub.activate()
    s3_stub = Stubber(clients["s3"])
    s3_stub.add_response("list_buckets", {"Buckets": []})
    s3_stub.activate()


def _stub_unreachable_region(clients: dict) -> None:
    """A disabled opt-in region: the first call any scan makes fails exactly
    the way AWS actually answered -- InvalidClientTokenId, not a timeout, not
    AccessDenied. Nothing else in `clients` is ever stubbed because scan.scan()
    never gets past this call."""
    stub = Stubber(clients["cloudformation"])
    stub.add_client_error("list_stacks", "InvalidClientTokenId",
                          "The security token included in the request is invalid")
    stub.activate()


def _identity(account="111122223333"):
    sts = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test",
                        region_name=GOOD_REGION).client("sts")
    stub = Stubber(sts)
    stub.add_response("get_caller_identity", {"Account": account, "Arn": f"arn:aws:sts::{account}:x",
                                               "UserId": "x"})
    stub.activate()
    return sts


def main() -> int:
    failures = []

    def check(label, cond):
        print(f"  {'OK ' if cond else 'BAD'} {label}")
        if not cond:
            failures.append(label)

    print("inventory: a bad region does not block a good one listed after it")
    good, bad = _clients_for(GOOD_REGION), _clients_for(BAD_REGION)
    _stub_healthy_region(good, stacks=[
        {"StackName": "dbshift-target", "StackStatus": "CREATE_COMPLETE", "CreationTime": NOW}])
    _stub_unreachable_region(bad)
    sess = FakeSession(_identity(), {GOOD_REGION: good, BAD_REGION: bad})
    report = kill_run.execute(sess, mode=None, confirm=None, regions=[GOOD_REGION, BAD_REGION])
    check("the healthy region's stack is still found",
          any(f["id"] == "dbshift-target" for f in report["found"]))
    check("the broken region is recorded by name, not silently dropped",
          report["region_errors"] == [{"region": BAD_REGION,
                                       "error": ("An error occurred (InvalidClientTokenId) when calling "
                                                "the ListStacks operation: The security token included "
                                                "in the request is invalid")}])

    print("inventory: a bad region listed FIRST does not block the good one after it")
    good2, bad2 = _clients_for(GOOD_REGION), _clients_for(BAD_REGION)
    _stub_healthy_region(good2, stacks=[
        {"StackName": "dbshift-target", "StackStatus": "CREATE_COMPLETE", "CreationTime": NOW}])
    _stub_unreachable_region(bad2)
    sess2 = FakeSession(_identity(), {GOOD_REGION: good2, BAD_REGION: bad2})
    report2 = kill_run.execute(sess2, mode=None, confirm=None, regions=[BAD_REGION, GOOD_REGION])
    check("order does not matter -- the good region after a bad one is still found",
          any(f["id"] == "dbshift-target" for f in report2["found"]))
    check("exactly one region error either way", len(report2["region_errors"]) == 1)

    print("destroy: the bad region is skipped, the good region's stack is still acted on")
    good3, bad3 = _clients_for(GOOD_REGION), _clients_for(BAD_REGION)
    _stub_healthy_region(good3, stacks=[
        {"StackName": "dbshift-target", "StackStatus": "CREATE_COMPLETE", "CreationTime": NOW}])
    _stub_unreachable_region(bad3)
    # Nothing in the stack's resources carries a project=dbshift tag or the
    # dbshift name prefix here except the stack itself, so the only action is
    # the delete -- no S3 empty, no RDS/DMS deletes to additionally stub.
    cfn_delete = Stubber(good3["cloudformation"])
    # _revoke_dms_grants asks the stack for its own outputs first (best effort:
    # no DmsSecurityGroupId output here, so it returns having touched no EC2
    # call at all), then the actual delete.
    cfn_delete.add_response("describe_stacks", {"Stacks": [{"StackName": "dbshift-target", "Outputs": [],
                                        "CreationTime": NOW, "StackStatus": "CREATE_COMPLETE"}]},
                            {"StackName": "dbshift-target"})
    cfn_delete.add_response("delete_stack", {}, {"StackName": "dbshift-target"})
    cfn_delete.activate()
    account = "111122223333"
    sess3 = FakeSession(_identity(account), {GOOD_REGION: good3, BAD_REGION: bad3})
    report3 = kill_run.execute(sess3, mode="destroy", confirm=account, regions=[GOOD_REGION, BAD_REGION])
    check("the stack in the healthy region was actually deleted (stub would raise otherwise)",
          any(r["id"] == "dbshift-target" and r["action"] == "delete" for r in report3["results"]))
    check("the destroy call still reports the region it could not reach",
          report3["region_errors"] and report3["region_errors"][0]["region"] == BAD_REGION)

    print("only sts.get_caller_identity -- the one call before the region loop -- is still fatal")
    bad_sts = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test",
                            region_name=GOOD_REGION).client("sts")
    Stubber(bad_sts).activate()   # no response queued: any call raises UnStubbedResponseError
    try:
        kill_run.execute(FakeSession(bad_sts, {}), mode=None, confirm=None, regions=[GOOD_REGION])
        check("an unreachable identity check still raises, rather than reporting an empty scan", False)
    except Exception:  # noqa: BLE001 -- any exception proves it was not swallowed
        check("an unreachable identity check still raises, rather than reporting an empty scan", True)

    print(f"\n{'ALL CHECKS PASSED' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
