"""The AWS calls that create, run and remove a DMS migration.

Everything here bills or changes state, so nothing here is called without a
passing preflight and, for anything that starts billing, an explicit account
confirmation from the caller.

Naming is load-bearing twice over: the kill switch acts on `dbshift*`, and the
IAM policy is scoped the same way. `dms/policy.py` owns the names.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from . import policy


class DmsError(RuntimeError):
    pass


def _tags(estate: str, run_id: str, expires: str) -> list[dict]:
    # Shared with Phase 6 so a DMS resource and an RDS resource carry the same
    # required tags -- Purpose=DMA among them.
    from provision import policy as prov_policy
    return prov_policy.as_tag_list({
        "project": "dbshift",
        "estate": estate,
        "collector_run_id": run_id,
        "expires-at": expires,
    })


def expiry(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(hours=policy.TTL_HOURS)).strftime("%Y-%m-%dT%H:%MZ")


# --- replication instance ----------------------------------------------------

def ensure_security_group(ec2, *, estate: str, run_id: str, emit,
                          from_stack: str | None = None) -> str | None:
    """The replication instance's own security group, created if absent.

    Returned so `create_instance` can attach it. Egress is open by default,
    which is all DMS needs -- it is the client to both databases. What matters
    is that the *source* and *target* can name this group in their own ingress
    rules, and that the group they name is the one the instance actually holds.

    Returns None if the group cannot be resolved: a replication instance on the
    VPC default group still works once the databases admit it, so this never
    blocks the migration. It only stops the operator from writing rules that
    point at nothing.
    """
    # Phase 6's stack now creates this group and grants it on the target in
    # the same template, so prefer that one: attaching the instance to a group
    # the target already trusts removes the "grant it afterwards" step that
    # used to fail the target endpoint test on every rebuild.
    if from_stack:
        emit({"event": "step",
              "detail": f"using the replication security group Phase 6 created ({from_stack}); "
                        "the target already admits it"})
        return from_stack

    name = policy.security_group_name(estate)
    try:
        found = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [name]}])["SecurityGroups"]
        if found:
            emit({"event": "step", "detail": f"security group {name} already exists; reusing it"})
            return found[0]["GroupId"]
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            return None
        gid = ec2.create_security_group(
            GroupName=name, VpcId=vpcs[0]["VpcId"],
            Description="DBShift DMS replication instance -- egress only",
            TagSpecifications=[{"ResourceType": "security-group",
                                "Tags": _tags(estate, run_id, expiry())}])["GroupId"]
        emit({"event": "step",
              "detail": f"created security group {name} ({gid}) -- grant it on the source's "
                        "1521 and the target's 5432"})
        return gid
    except Exception as exc:  # noqa: BLE001 -- reported, never fatal
        emit({"event": "step", "detail": f"security group could not be prepared: "
                                         f"{str(exc).splitlines()[0]}"})
        return None


def grant_ingress(ec2, *, dms_group_id: str, target_group_id: str, port: int,
                  what: str, emit) -> bool:
    """Let the DMS replication instance reach one database. Idempotent.

    `ensure_security_group` created the group and then printed "grant it on the
    source's 1521 and the target's 5432" -- a to-do, not an action. Nothing
    automated it, so every rebuild needed two rules added by hand in the AWS
    console, and Phase 7 failed twice before anyone thought to add them: once
    on the source with ORA-12170, once on the target with an ODBC timeout,
    both of which read as "the database is unreachable" rather than "nobody
    opened the door". On 2026-09-22 that cost most of a day.

    It cannot be done in Phase 6's CloudFormation either, because the DMS
    group does not exist until Phase 7 -- the template can only pin the
    operator's own /32. So it belongs here, at the one moment both group ids
    are known.

    Security-group-to-security-group, never a CIDR: the rule admits *this*
    replication instance and nothing else, and it dies with the group.
    """
    if not dms_group_id or not target_group_id:
        emit({"event": "step",
              "detail": f"cannot grant {what} -- security group not resolved "
                        f"(dms={dms_group_id or 'unknown'}, db={target_group_id or 'unknown'})"})
        return False
    try:
        ec2.authorize_security_group_ingress(
            GroupId=target_group_id,
            IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                "UserIdGroupPairs": [{
                    "GroupId": dms_group_id,
                    "Description": "DBShift DMS replication instance",
                }],
            }])
        emit({"event": "step",
              "detail": f"granted {what}: {target_group_id} now admits {dms_group_id} on {port}"})
        return True
    except Exception as exc:  # noqa: BLE001 -- botocore's ClientError
        # Already there is the normal case on a re-run, and is success.
        if "InvalidPermission.Duplicate" in str(exc):
            emit({"event": "step",
                  "detail": f"{what} already granted on {target_group_id}:{port}; nothing to do"})
            return True
        emit({"event": "step",
              "detail": f"could not grant {what} on {target_group_id}:{port} -- "
                        f"{str(exc).splitlines()[0]}"})
        return False


def source_security_group(ec2, host: str, emit) -> str | None:
    """The security group of the EC2 instance serving the source, by its IP.

    Matches on private then public address, because the console connects to
    one and DMS to the other. Returns None for a source that is not an EC2
    instance in this account -- an on-premises source has no security group
    to grant, and saying so is better than failing.
    """
    if not host:
        return None
    for key in ("private-ip-address", "ip-address"):
        try:
            res = ec2.describe_instances(
                Filters=[{"Name": key, "Values": [host]}])["Reservations"]
        except Exception:  # noqa: BLE001 -- permissions vary; fall through to None
            return None
        for r in res:
            for inst in r.get("Instances", []):
                groups = inst.get("SecurityGroups") or []
                if groups:
                    return groups[0]["GroupId"]
    emit({"event": "step",
          "detail": f"no EC2 instance in this account has address {host}; "
                    "its firewall is not ours to open"})
    return None


def create_instance(dms, ec2, *, estate: str, run_id: str, emit,
                    instance_class: str | None = None,
                    dms_group_id: str | None = None) -> dict:
    """Create the replication instance. **This starts billing.**

    Returns when the instance is `available`, because an endpoint test against a
    creating instance fails in a way that looks like a connectivity problem.
    """
    name = policy.instance_name(estate)
    klass = instance_class or policy.INSTANCE_CLASS
    existing = [i for i in dms.describe_replication_instances()["ReplicationInstances"]
                if i["ReplicationInstanceIdentifier"] == name]
    if existing:
        emit({"event": "step", "detail": f"{name} already exists; reusing it"})
        return existing[0]

    emit({"event": "step",
          "detail": f"creating {name} ({klass}, {policy.STORAGE_GB} GB) -- "
                    "this starts billing by the hour"})
    sg_id = ensure_security_group(ec2, estate=estate, run_id=run_id, emit=emit,
                                  from_stack=dms_group_id)
    resp = dms.create_replication_instance(
        ReplicationInstanceIdentifier=name,
        ReplicationInstanceClass=klass,
        AllocatedStorage=policy.STORAGE_GB,
        EngineVersion=policy.ENGINE_VERSION,
        MultiAZ=policy.MULTI_AZ,
        PubliclyAccessible=policy.PUBLICLY_ACCESSIBLE,
        AutoMinorVersionUpgrade=policy.AUTO_MINOR_UPGRADE,
        **({"VpcSecurityGroupIds": [sg_id]} if sg_id else {}),
        Tags=_tags(estate, run_id, expiry()),
    )
    arn = resp["ReplicationInstance"]["ReplicationInstanceArn"]
    return wait_for_instance(dms, arn, emit=emit)


def wait_for_instance(dms, arn: str, *, emit, timeout_minutes: int = 40) -> dict:
    deadline = time.time() + timeout_minutes * 60
    last = None
    while time.time() < deadline:
        ri = dms.describe_replication_instances(
            Filters=[{"Name": "replication-instance-arn", "Values": [arn]}]
        )["ReplicationInstances"][0]
        status = ri["ReplicationInstanceStatus"]
        if status != last:
            emit({"event": "step", "detail": f"replication instance {status}"})
            last = status
        if status == "available":
            return ri
        if status in ("failed", "deleting", "incompatible-network"):
            raise DmsError(f"replication instance is {status}")
        time.sleep(20)
    raise DmsError(f"replication instance did not become available within {timeout_minutes} minutes")


# --- endpoints ---------------------------------------------------------------

def create_endpoints(dms, *, estate: str, run_id: str, source: dict, target: dict, emit) -> dict:
    """Source and target endpoints.

    Passwords are passed straight to the AWS API and never written to a record,
    a file or a log line -- the same rule the rest of the project follows.
    """
    out = {}
    for role, spec in (("source", source), ("target", target)):
        name = policy.endpoint_name(estate, role)
        existing = [e for e in dms.describe_endpoints()["Endpoints"]
                    if e["EndpointIdentifier"] == name]
        if existing:
            # **Reuse the name, not the address.** An endpoint left over from a
            # previous estate keeps whatever host it was built with: on
            # 2026-09-21 the source endpoint still pointed at a laptop's public
            # IP from a week earlier, was "reused" without comment, and failed
            # its test with ORA-12170 -- which reads as a network problem
            # rather than as the wrong server. The plan is the authority on
            # where an endpoint points, so a stale one is corrected and the
            # change is said out loud.
            e = existing[0]
            # ExtraConnectionAttributes is in here for the same reason the
            # address is. An endpoint created before this project set
            # useLogMinerReader=N has the right host, port, user and database,
            # so without this row `drift` is empty, the endpoint is "reused"
            # unchanged, and the test fails again with "Log Miner is not
            # supported in Oracle PDB environment" -- a fix that silently does
            # not apply is worse than no fix, because the message does not
            # change.
            drift = {k: (e.get(a), v) for k, a, v in (
                ("host", "ServerName", spec["host"]),
                ("port", "Port", int(spec["port"])),
                ("database", "DatabaseName", spec["database"]),
                ("user", "Username", spec["user"]),
                ("settings", "ExtraConnectionAttributes", spec.get("extra_settings") or None),
            ) if (e.get(a) or None) != v}
            if drift:
                emit({"event": "step",
                      "detail": f"{role} endpoint {name} points elsewhere; updating "
                                + ", ".join(f"{k} {was} -> {now}" for k, (was, now) in drift.items())})
                e = dms.modify_endpoint(
                    EndpointArn=e["EndpointArn"], ServerName=spec["host"], Port=int(spec["port"]),
                    DatabaseName=spec["database"], Username=spec["user"],
                    Password=spec["password"],
                    **({"ExtraConnectionAttributes": spec["extra_settings"]}
                       if spec.get("extra_settings") else {}))["Endpoint"]
            else:
                emit({"event": "step",
                      "detail": f"{role} endpoint {name} already points at {spec['host']}; reusing it"})
            out[role] = e
            continue

        emit({"event": "step", "detail": f"creating {role} endpoint {name} ({spec['engine']})"})
        # RDS for PostgreSQL refuses an unencrypted connection, and DMS defaults
        # to none -- so without this the target endpoint fails its own
        # connection test with "no pg_hba.conf entry ... no encryption".
        ssl_mode = spec.get("ssl_mode") or (
            policy.TARGET_SSL_MODE if role == "target" else policy.SOURCE_SSL_MODE)
        kwargs = dict(
            EndpointIdentifier=name,
            EndpointType=role,
            EngineName=spec["engine"],
            ServerName=spec["host"],
            Port=int(spec["port"]),
            Username=spec["user"],
            Password=spec["password"],
            DatabaseName=spec["database"],
            SslMode=ssl_mode,
            Tags=_tags(estate, run_id, expiry()),
        )
        if spec.get("extra_settings"):
            kwargs["ExtraConnectionAttributes"] = spec["extra_settings"]
        out[role] = dms.create_endpoint(**kwargs)["Endpoint"]
    return out


def test_endpoint(dms, *, endpoint_arn: str, instance_arn: str, emit,
                  timeout_minutes: int = 10) -> dict:
    """Prove the endpoint connects before a task depends on it.

    Worth the wait: a task that starts against an unreachable endpoint reports
    a generic failure minutes later, and the cause is much harder to see there.
    """
    def existing_connection():
        conns = dms.describe_connections(
            Filters=[{"Name": "endpoint-arn", "Values": [endpoint_arn]}])["Connections"]
        this = [c for c in conns if c.get("ReplicationInstanceArn") == instance_arn]
        return this[0] if this else None

    # A prior attempt against this same endpoint (a retry after an earlier
    # step failed, or this console rerun after a fix) can leave AWS still
    # mid-test. Calling test_connection again then raises
    # InvalidResourceStateFault: "Connection is already being tested" -- not
    # a real failure, just AWS refusing to start a second test on the same
    # pair. Check what is already there first, and only start a fresh test
    # when nothing is in flight.
    prior = existing_connection()
    if prior and prior.get("Status") == "testing":
        emit({"event": "step", "detail": "a connection test is already in progress; waiting on it"})
    else:
        try:
            dms.test_connection(ReplicationInstanceArn=instance_arn, EndpointArn=endpoint_arn)
        except Exception as exc:                        # noqa: BLE001 -- botocore's ClientError
            if "already being tested" not in str(exc):
                raise
            emit({"event": "step", "detail": "a connection test was already starting; waiting on it"})
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        conns = dms.describe_connections(
            Filters=[{"Name": "endpoint-arn", "Values": [endpoint_arn]}])["Connections"]
        this = [c for c in conns if c.get("ReplicationInstanceArn") == instance_arn]
        if this:
            status = this[0]["Status"]
            if status == "successful":
                emit({"event": "step", "detail": "endpoint connection successful"})
                return this[0]
            if status == "failed":
                raise DmsError("endpoint connection failed: "
                               + (this[0].get("LastFailureMessage") or "no reason given"))
        time.sleep(10)
    raise DmsError("endpoint connection test did not finish in time")


# --- task --------------------------------------------------------------------

def create_task(dms, *, estate: str, run_id: str, migration_type: str, instance_arn: str,
                source_arn: str, target_arn: str, table_mappings: str, task_settings: str,
                emit) -> dict:
    name = policy.task_name(estate, migration_type)
    existing = [t for t in dms.describe_replication_tasks(WithoutSettings=True)["ReplicationTasks"]
                if t["ReplicationTaskIdentifier"] == name]
    if existing:
        emit({"event": "step", "detail": f"task {name} already exists; reusing it"})
        return existing[0]

    emit({"event": "step", "detail": f"creating task {name} ({migration_type})"})
    task = dms.create_replication_task(
        ReplicationTaskIdentifier=name,
        SourceEndpointArn=source_arn,
        TargetEndpointArn=target_arn,
        ReplicationInstanceArn=instance_arn,
        MigrationType=migration_type,
        TableMappings=table_mappings,
        ReplicationTaskSettings=task_settings,
        Tags=_tags(estate, run_id, expiry()),
    )["ReplicationTask"]
    # A freshly created task sits in "creating" for a few seconds before AWS
    # settles it into "ready". start_task, called right after this returns,
    # reads whatever status is there *right now* -- calling
    # start_replication_task against "creating" gets InvalidResourceStateFault:
    # "Replication Task cannot be started, invalid state", which reads like a
    # real failure but is really just this function returning too early. Same
    # shape of gap wait_for_instance already closes for the instance itself.
    return wait_for_task_ready(dms, task["ReplicationTaskArn"], emit=emit)


def wait_for_task_ready(dms, arn: str, *, emit, timeout_minutes: int = 5) -> dict:
    deadline = time.time() + timeout_minutes * 60
    last = None
    while time.time() < deadline:
        t = dms.describe_replication_tasks(
            Filters=[{"Name": "replication-task-arn", "Values": [arn]}],
            WithoutSettings=True)["ReplicationTasks"][0]
        status = t["Status"]
        if status != last:
            emit({"event": "step", "detail": f"task {status}"})
            last = status
        if status in ("ready", "stopped", "failed"):
            return t
        time.sleep(5)
    raise DmsError(f"task did not settle out of 'creating' within {timeout_minutes} minutes")


def start_task(dms, *, task_arn: str, emit) -> None:
    task = dms.describe_replication_tasks(
        Filters=[{"Name": "replication-task-arn", "Values": [task_arn]}],
        WithoutSettings=True)["ReplicationTasks"][0]
    # A task that has run before must be resumed or reloaded rather than started,
    # and asking for the wrong one is an error rather than a no-op.
    start_type = "reload-target" if task["Status"] in ("stopped", "failed") else "start-replication"
    emit({"event": "step", "detail": f"starting task ({start_type})"})
    dms.start_replication_task(ReplicationTaskArn=task_arn, StartReplicationTaskType=start_type)


def task_progress(dms, task_arn: str) -> dict:
    """One snapshot of where the task is, in terms a person can read."""
    t = dms.describe_replication_tasks(
        Filters=[{"Name": "replication-task-arn", "Values": [task_arn]}],
        WithoutSettings=True)["ReplicationTasks"][0]
    stats = t.get("ReplicationTaskStats", {}) or {}
    return {
        "status": t.get("Status"),
        "stop_reason": t.get("StopReason"),
        "last_failure": t.get("LastFailureMessage"),
        "tables_loaded": stats.get("TablesLoaded", 0),
        "tables_loading": stats.get("TablesLoading", 0),
        "tables_queued": stats.get("TablesQueued", 0),
        "tables_errored": stats.get("TablesErrored", 0),
        "full_load_progress": stats.get("FullLoadProgressPercent", 0),
        "elapsed_ms": stats.get("ElapsedTimeMillis", 0),
    }


def table_statistics(dms, task_arn: str) -> list[dict]:
    """Per-table outcome: what loaded, what was suspended, what validation says."""
    rows: list[dict] = []
    paginator = dms.get_paginator("describe_table_statistics")
    for page in paginator.paginate(ReplicationTaskArn=task_arn):
        for s in page["TableStatistics"]:
            rows.append({
                "schema": s.get("SchemaName"),
                "table": s.get("TableName"),
                "state": s.get("TableState"),
                "inserts": s.get("Inserts", 0),
                "updates": s.get("Updates", 0),
                "deletes": s.get("Deletes", 0),
                "full_load_rows": s.get("FullLoadRows", 0),
                "full_load_errors": s.get("FullLoadErrorRows", 0),
                "validation_state": s.get("ValidationState"),
                "validation_failed": s.get("ValidationFailedRecords", 0),
                "validation_pending": s.get("ValidationPendingRecords", 0),
            })
    return sorted(rows, key=lambda r: (r["schema"] or "", r["table"] or ""))


def wait_for_full_load(dms, task_arn: str, *, emit,
                       timeout_minutes: int = policy.FULL_LOAD_TIMEOUT_MINUTES) -> dict:
    """Watch until the full load finishes, or the task stops, or time runs out."""
    deadline = time.time() + timeout_minutes * 60
    last = None
    while time.time() < deadline:
        p = task_progress(dms, task_arn)
        line = (f"{p['status']} -- {p['full_load_progress']}%, "
                f"{p['tables_loaded']} loaded, {p['tables_loading']} loading, "
                f"{p['tables_errored']} errored")
        if line != last:
            emit({"event": "progress", "detail": line, **p})
            last = line

        if p["status"] == "failed":
            raise DmsError(p.get("last_failure") or "task failed with no message")
        # `stopped` after a full-load-only task is success; the stop reason says so.
        if p["status"] == "stopped":
            return p
        if p["status"] == "running" and p["full_load_progress"] == 100 \
                and p["tables_loading"] == 0 and p["tables_queued"] == 0:
            # full-load-and-cdc keeps running after the load; that is the point.
            return p
        time.sleep(15)
    raise DmsError(f"full load did not finish within {timeout_minutes} minutes")


def cdc_latency(cloudwatch, *, instance_name: str, task_id: str) -> dict | None:
    """How far behind the target is, from the task's own CloudWatch metrics.

    This is the number a cutover decision rests on: with replication running,
    the outage window is roughly this latency plus the time to switch over,
    rather than the time to copy the whole estate.
    """
    from datetime import datetime, timedelta, timezone as tz
    end = datetime.now(tz.utc)
    start = end - timedelta(minutes=15)
    out = {}
    for metric, key in (("CDCLatencySource", "source_seconds"),
                        ("CDCLatencyTarget", "target_seconds")):
        r = cloudwatch.get_metric_statistics(
            Namespace="AWS/DMS", MetricName=metric,
            Dimensions=[{"Name": "ReplicationInstanceIdentifier", "Value": instance_name},
                        {"Name": "ReplicationTaskIdentifier", "Value": task_id}],
            StartTime=start, EndTime=end, Period=60, Statistics=["Average"])
        points = sorted(r.get("Datapoints", []), key=lambda d: d["Timestamp"])
        out[key] = round(points[-1]["Average"], 1) if points else None
    if out.get("source_seconds") is None and out.get("target_seconds") is None:
        return None
    worst = max(v for v in out.values() if v is not None)
    out["worst_seconds"] = worst
    out["within_cutover_threshold"] = worst <= policy.CDC_CUTOVER_LATENCY_SECONDS
    out["threshold_seconds"] = policy.CDC_CUTOVER_LATENCY_SECONDS
    return out


# --- teardown ----------------------------------------------------------------

def stop_task(dms, *, task_arn: str, emit) -> None:
    """Stop replication. The instance keeps billing until it is deleted."""
    emit({"event": "step", "detail": "stopping the task"})
    try:
        dms.stop_replication_task(ReplicationTaskArn=task_arn)
    except dms.exceptions.InvalidResourceStateFault:
        emit({"event": "step", "detail": "task was not running"})
