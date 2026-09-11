"""Find everything in the account that can bill, and decide what is ours.

"Ours" has one definition, used everywhere: a name starting `dbshift` or a
`project=dbshift` tag. The IAM role here has rds:*, dms:* and more, so the
permission set does not protect anything else in the account -- this filter
does. Resources that are not ours are still reported, because a kill switch
that hides a running instance is lying about the bill, but nothing in this
package ever acts on them.
"""

from __future__ import annotations

PREFIX = "dbshift"

KINDS = ("cloudformation_stack", "rds_instance", "rds_snapshot",
         "dms_replication_instance", "nat_gateway")


def _tags(tag_list) -> dict:
    return {t["Key"].lower(): t["Value"] for t in (tag_list or [])}


def is_ours(name: str | None, tags: dict | None = None) -> bool:
    if (name or "").lower().startswith(PREFIX):
        return True
    return (tags or {}).get("project", "").lower() == PREFIX


def _item(kind, rid, status, region, *, ours, billable, stack=None, detail="", **extra) -> dict:
    return {"kind": kind, "id": rid, "status": status, "region": region, "ours": ours,
            "billable": billable, "stack": stack, "detail": detail, **extra}


def clients_for(session, region: str) -> dict:
    return {name: session.client(name, region_name=region)
            for name in ("cloudformation", "rds", "dms", "ec2")}


def scan(clients: dict, region: str) -> list[dict]:
    """Every live resource of the billable kinds, in one region. Read-only."""
    found: list[dict] = []

    # Stacks cost nothing themselves, but deleting one is how its resources go.
    for page in clients["cloudformation"].get_paginator("list_stacks").paginate():
        for s in page["StackSummaries"]:
            if s["StackStatus"] == "DELETE_COMPLETE":
                continue
            found.append(_item("cloudformation_stack", s["StackName"], s["StackStatus"], region,
                               ours=is_ours(s["StackName"]), billable=False))

    for page in clients["rds"].get_paginator("describe_db_instances").paginate():
        for db in page["DBInstances"]:
            tags = _tags(db.get("TagList"))
            found.append(_item(
                "rds_instance", db["DBInstanceIdentifier"], db.get("DBInstanceStatus"), region,
                ours=is_ours(db["DBInstanceIdentifier"], tags),
                # A stopped instance still bills for storage, so it is still billable.
                billable=True,
                stack=tags.get("aws:cloudformation:stack-name"),
                detail=f"{db.get('Engine', '?')} {db.get('DBInstanceClass', '?')}",
                deletion_protection=bool(db.get("DeletionProtection")),
            ))

    for page in clients["rds"].get_paginator("describe_db_snapshots").paginate(SnapshotType="manual"):
        for sn in page["DBSnapshots"]:
            tags = _tags(sn.get("TagList"))
            found.append(_item(
                "rds_snapshot", sn["DBSnapshotIdentifier"], sn.get("Status"), region,
                ours=is_ours(sn["DBSnapshotIdentifier"], tags), billable=True,
                detail=f"{sn.get('AllocatedStorage', '?')} GB",
            ))

    for page in clients["dms"].get_paginator("describe_replication_instances").paginate():
        for ri in page["ReplicationInstances"]:
            found.append(_item(
                "dms_replication_instance", ri["ReplicationInstanceIdentifier"],
                ri.get("ReplicationInstanceStatus"), region,
                ours=is_ours(ri["ReplicationInstanceIdentifier"]), billable=True,
                detail=ri.get("ReplicationInstanceClass", ""),
                arn=ri.get("ReplicationInstanceArn"),
            ))

    nat = clients["ec2"].describe_nat_gateways(
        Filter=[{"Name": "state", "Values": ["pending", "available"]}])
    for gw in nat.get("NatGateways", []):
        tags = _tags(gw.get("Tags"))
        found.append(_item(
            "nat_gateway", gw["NatGatewayId"], gw.get("State"), region,
            ours=is_ours(tags.get("name"), tags), billable=True,
            stack=tags.get("aws:cloudformation:stack-name"),
        ))

    return found
