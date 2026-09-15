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

def create_instance(dms, ec2, *, estate: str, run_id: str, emit) -> dict:
    """Create the replication instance. **This starts billing.**

    Returns when the instance is `available`, because an endpoint test against a
    creating instance fails in a way that looks like a connectivity problem.
    """
    name = policy.instance_name(estate)
    existing = [i for i in dms.describe_replication_instances()["ReplicationInstances"]
                if i["ReplicationInstanceIdentifier"] == name]
    if existing:
        emit({"event": "step", "detail": f"{name} already exists; reusing it"})
        return existing[0]

    emit({"event": "step",
          "detail": f"creating {name} ({policy.INSTANCE_CLASS}, {policy.STORAGE_GB} GB) -- "
                    "this starts billing by the hour"})
    resp = dms.create_replication_instance(
        ReplicationInstanceIdentifier=name,
        ReplicationInstanceClass=policy.INSTANCE_CLASS,
        AllocatedStorage=policy.STORAGE_GB,
        EngineVersion=policy.ENGINE_VERSION,
        MultiAZ=policy.MULTI_AZ,
        PubliclyAccessible=policy.PUBLICLY_ACCESSIBLE,
        AutoMinorVersionUpgrade=policy.AUTO_MINOR_UPGRADE,
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
            emit({"event": "step", "detail": f"{role} endpoint {name} already exists; reusing it"})
            out[role] = existing[0]
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
    dms.test_connection(ReplicationInstanceArn=instance_arn, EndpointArn=endpoint_arn)
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        conns = dms.describe_connections(
            Filters=[{"Name": "endpoint-arn", "Values": [endpoint_arn]}])["Connections"]
        this = [c for c in conns if c.get("ReplicationInstanceArn") == instance_arn]
        if this:
            status = this[0]["Status"]
            if status == "successful":
                emit({"event": "step", "detail": f"endpoint connection successful"})
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
    return dms.create_replication_task(
        ReplicationTaskIdentifier=name,
        SourceEndpointArn=source_arn,
        TargetEndpointArn=target_arn,
        ReplicationInstanceArn=instance_arn,
        MigrationType=migration_type,
        TableMappings=table_mappings,
        ReplicationTaskSettings=task_settings,
        Tags=_tags(estate, run_id, expiry()),
    )["ReplicationTask"]


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
