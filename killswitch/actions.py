"""Decide, then act. Deciding is a pure function so it can be tested without AWS.

Two modes:

  stop     pause what can be paused. Cheap to undo, but an RDS instance that is
           stopped **restarts itself after 7 days** and starts billing again, so
           stop is a pause, not an off switch. DMS instances and NAT gateways
           cannot be stopped at all.
  destroy  delete what is ours. A resource owned by a dbshift- stack is deleted
           by deleting the stack, never directly -- deleting it underneath the
           stack leaves the stack broken and the next deploy confused.

Never acted on, in either mode:

  * anything that is not ours (see scan.is_ours)
  * a manual snapshot, unless include_snapshots -- a snapshot is a backup, and
    deleting it is permanent
  * an instance with deletion protection on, unless force_deletion_protection
"""

from __future__ import annotations

LEAVE, STOP, DELETE, BLOCKED, EMPTY = "leave", "stop", "delete", "blocked", "empty"

# Buckets are emptied before anything is deleted: CloudFormation cannot delete a
# stack whose bucket still holds objects, and would leave it DELETE_FAILED.
ORDER = {EMPTY: 0, STOP: 1, DELETE: 2}


def _a(res, action, why):
    return {**res, "action": action, "why": why}


def decide(found: list[dict], mode: str, *, include_snapshots: bool = False,
           force_deletion_protection: bool = False) -> list[dict]:
    if mode not in ("stop", "destroy"):
        raise ValueError("mode must be 'stop' or 'destroy'")

    live_stacks = {r["id"] for r in found
                   if r["kind"] == "cloudformation_stack" and r["ours"]
                   and r["status"] != "DELETE_IN_PROGRESS"}
    plan = []
    for r in found:
        if not r["ours"]:
            plan.append(_a(r, LEAVE, "not created by this project -- the kill switch never touches it"))
            continue

        kind, status = r["kind"], (r["status"] or "").lower()

        if mode == "stop":
            if kind == "rds_instance" and status == "available":
                plan.append(_a(r, STOP, "stops compute billing; storage still bills, and AWS "
                                        "restarts it automatically after 7 days"))
            elif kind == "rds_instance" and status == "stopped":
                plan.append(_a(r, LEAVE, "already stopped -- but it restarts itself 7 days after "
                                         "it was stopped"))
            elif kind in ("dms_replication_instance", "nat_gateway", "s3_bucket"):
                plan.append(_a(r, LEAVE, "cannot be stopped, only deleted -- use destroy"))
            else:
                plan.append(_a(r, LEAVE, "nothing to stop"))
            continue

        # destroy
        if kind == "cloudformation_stack":
            if status == "delete_in_progress":
                plan.append(_a(r, LEAVE, "already deleting"))
            else:
                plan.append(_a(r, DELETE, "deleting the stack deletes everything it created"))
        elif kind == "s3_bucket" and r.get("stack") in live_stacks:
            plan.append(_a(r, EMPTY, f"emptied first so its stack {r['stack']} can delete it"))
        elif r.get("stack") in live_stacks:
            plan.append(_a(r, LEAVE, f"deleted with its stack {r['stack']}"))
        elif kind == "rds_snapshot" and not include_snapshots:
            plan.append(_a(r, LEAVE, "kept -- a snapshot is a backup and deleting it is permanent; "
                                     "storage bills while it exists (include_snapshots to delete)"))
        elif kind == "rds_instance" and r.get("deletion_protection") and not force_deletion_protection:
            plan.append(_a(r, BLOCKED, "deletion protection is on; it exists to stop exactly this "
                                       "(force_deletion_protection to override)"))
        elif kind == "rds_instance" and status == "deleting":
            plan.append(_a(r, LEAVE, "already deleting"))
        else:
            plan.append(_a(r, DELETE, "ours, not owned by a stack, so deleted directly"))
    return plan


def execute(plan: list[dict], clients: dict) -> list[dict]:
    """Carry out stop/delete actions. Returns each attempt with its outcome.

    One failure does not stop the rest: a kill switch that aborts halfway leaves
    the bill running on everything after the failure.
    """
    results = []
    for step in sorted((s for s in plan if s["action"] in ORDER), key=lambda s: ORDER[s["action"]]):
        try:
            _do(step, clients)
            results.append({**step, "outcome": "requested"})
        except Exception as exc:  # noqa: BLE001 -- reported per resource, never swallowed
            results.append({**step, "outcome": f"FAILED: {exc}"})
    return results


def _do(step: dict, clients: dict) -> None:
    kind, rid = step["kind"], step["id"]
    if kind == "s3_bucket":
        _empty_bucket(clients["s3"], rid)
        if step["action"] == DELETE:
            clients["s3"].delete_bucket(Bucket=rid)
        return
    if step["action"] == STOP:
        clients["rds"].stop_db_instance(DBInstanceIdentifier=rid)
        return
    if kind == "cloudformation_stack":
        clients["cloudformation"].delete_stack(StackName=rid)
    elif kind == "rds_instance":
        if step.get("deletion_protection"):
            clients["rds"].modify_db_instance(DBInstanceIdentifier=rid, DeletionProtection=False,
                                              ApplyImmediately=True)
        # No final snapshot: this is a migration target, the source is the system
        # of record. A snapshot would be a new billable thing the kill switch made.
        clients["rds"].delete_db_instance(DBInstanceIdentifier=rid, SkipFinalSnapshot=True,
                                          DeleteAutomatedBackups=True)
    elif kind == "rds_snapshot":
        clients["rds"].delete_db_snapshot(DBSnapshotIdentifier=rid)
    elif kind == "dms_replication_instance":
        clients["dms"].delete_replication_instance(ReplicationInstanceArn=step["arn"])
    elif kind == "nat_gateway":
        clients["ec2"].delete_nat_gateway(NatGatewayId=rid)
    else:
        raise ValueError(f"no delete action for {kind}")


def _empty_bucket(s3, bucket: str) -> None:
    """Delete every object and every version. Versions are included so a bucket
    that ever had versioning on is really empty, not just holding delete markers."""
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        objects = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                   for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
