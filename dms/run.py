"""Phase 7, heterogeneous path -- move the data with AWS DMS.

    python -m dms.run                          # plan and preflight only, free
    python -m dms.run --execute --confirm <account-id>
    python -m dms.run --migration-type full-load-and-cdc --execute --confirm <account-id>
    python -m dms.run --status                 # where a running task stands
    python -m dms.run --stop                   # stop the task (the instance keeps billing)

Passwords come from the environment and are held in memory only:
    DBSHIFT_SOURCE_OWNER_PASSWORD   the source schema owner, read by DMS
    DBSHIFT_PG_PASSWORD             the target master password

**A replication instance bills by the hour for as long as it exists**, whether
or not a task is running. Planning is free; `--execute` is not, which is why it
needs the account id typed back the same way a deploy does.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "dms"

from provision import records as prov_records
from provision import run as provision_run

from provision import policy as prov_policy
from provision import run as prov_run

from . import actions, mappings, policy, preflight, residue


def target_from_deployment(session=None) -> dict | None:
    """Where the provisioned target actually is, from Phase 6's own record.

    The host cannot come from a flag or an environment variable: it is assigned
    by AWS at create time and written to `provision/output/deployed.json`. The
    master password is in SSM, where the deploy put it, and is read into memory
    only when an endpoint is being created.
    """
    path = prov_run.OUTPUT / "deployed.json"
    if not path.exists():
        return None
    deployed = json.loads(path.read_text(encoding="utf-8"))
    outputs = deployed.get("outputs") or {}
    host = outputs.get("Endpoint")
    if not host:
        return None

    plan_path = prov_run.OUTPUT / "provision_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
    rendered = plan.get("rendered") or {}
    pg = rendered.get("target") == "POSTGRESQL"

    password = ""
    if session is not None and rendered.get("password_parameter"):
        try:
            password = session.client("ssm", region_name=policy.REGION).get_parameter(
                Name=rendered["password_parameter"], WithDecryption=True)["Parameter"]["Value"]
        except Exception:  # noqa: BLE001 -- fall back to the environment below
            password = ""

    return {
        "engine": "postgres" if pg else "oracle",
        "host": host,
        "port": int(outputs.get("Port") or (5432 if pg else 1521)),
        "user": prov_policy.PG_MASTER_USERNAME if pg else prov_policy.MASTER_USERNAME,
        "password": password or os.environ.get("DBSHIFT_PG_PASSWORD", ""),
        "database": prov_policy.PG_DB_NAME if pg else prov_policy.DB_NAME,
        "stack": deployed.get("stack_name"),
    }

OUTPUT = Path(__file__).resolve().parent / "output"
RECORD = OUTPUT / "dms_run.json"

DEFAULT_PROFILE = os.environ.get("DBSHIFT_AWS_PROFILE", "dbshift-static")


def _session(profile: str):
    import boto3
    return boto3.Session(profile_name=profile, region_name=policy.REGION)


def last_run() -> dict | None:
    return json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.exists() else None


def _save(record: dict) -> dict:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


def _model_live() -> bool:
    """Whether the model tier can actually be invoked right now.

    Asked rather than assumed: a model that is configured but unreachable would
    otherwise make the residue claim answers it never produced. DBSHIFT_MODEL_MODE
    forces it off for a deterministic run.
    """
    import os
    if os.environ.get("DBSHIFT_MODEL_MODE") == "off":
        return False
    try:
        from bedrock.client import BedrockClient
        BedrockClient().complete("reasoning", "Reply with exactly: OK", max_tokens=8)
        return True
    except Exception:  # noqa: BLE001 -- unreachable is an ordinary state here
        return False


def declared_migration_type(recs: dict) -> str:
    """What Phase 1 said this migration is, in DMS's vocabulary.

    The mode is chosen in Discovery and carried on the gate record. Phase 7
    following it means the task DMS is given matches the migration the gate
    actually judged -- before this, the mode was a flag here with its own
    default, so a run could be gated for full load and then executed with CDC,
    or the reverse, with nothing in the record contradicting it.
    """
    record = (recs.get("gate") or {}).get("migration_mode")
    if not record:
        return policy.FULL_LOAD
    # collector.mode names its values as DMS does, so this passes straight
    # through. Anything unrecognised falls back to the safer full load rather
    # than starting a replication nobody asked for.
    mode = record.get("mode")
    return mode if mode in policy.MIGRATION_TYPES else policy.FULL_LOAD


def plan(session=None, *, migration_type: str | None = None,
         target_counts: dict | None = None) -> dict:
    """Everything that can be known without creating anything. Free.

    Produces the two JSON documents DMS will be given, the preflight verdict,
    and the list of objects DMS will *not* move -- which is as important as
    what it will, because "DMS migrated the database" is never true.
    """
    recs = prov_records.load()
    # None means "whatever Phase 1 declared". An explicit argument still wins,
    # and the plan records which of the two it was.
    declared = declared_migration_type(recs)
    overridden = migration_type is not None and migration_type != declared
    migration_type = migration_type or declared
    prov_plan_path = provision_run.OUTPUT / "provision_plan.json"
    prov_plan = json.loads(prov_plan_path.read_text(encoding="utf-8")) \
        if prov_plan_path.exists() else None

    estate = prov_records.estate_of(recs["assessment"])
    # Log mode and supplemental logging decide whether CDC is possible at all,
    # and provision.records.source_facts does not carry them -- it answers a
    # different question (version, character set, CDB). The sizing record's
    # facts do, because Phase 3 reads them for the same reason.
    facts = {**prov_records.source_facts(recs["sizing"]["collector_run_id"]),
             **{k: v for k, v in (recs["sizing"].get("facts") or {}).items()
                if k in ("log_mode", "supplemental_logging")}}
    d = recs["sizing"]["decision"]
    heterogeneous = d.get("engine") == "POSTGRESQL"

    checks = [preflight.records_consistent(recs)]
    checks.append(preflight.gate_allows(recs["gate"], migration_type))
    checks += preflight.cdc_possible(facts, recs["assessment"]["findings"], migration_type)
    if target_counts is not None:
        checks.append(preflight.target_is_empty(target_counts))
    if session is not None:
        # One AWS call, and it answers one question: is a replication instance
        # already running and billing? Everything else in this phase is computed
        # from records on disk.
        #
        # So a credentials problem must not take the phase down. Expired SSO
        # tokens are the normal state of an idle laptop, and planning is exactly
        # what someone does before they go and refresh them. Report the check as
        # blocked, with the reason, and carry on -- the alternative is a 500 that
        # looks like the planner is broken when only the login is.
        try:
            dms = session.client("dms")
            checks.append(preflight.no_instance_running(
                dms.describe_replication_instances()["ReplicationInstances"],
                policy.instance_name(estate)))
        except Exception as exc:  # noqa: BLE001 -- botocore raises several shapes here
            detail = str(exc).splitlines()[0]
            expired = "ExpiredToken" in detail or "security token" in detail.lower()
            checks.append({
                "name": "no_instance_running",
                "status": preflight.BLOCKED,
                "detail": ("AWS credentials have expired, so this could not be checked"
                           if expired else f"AWS could not be reached: {detail}"),
                "remedy": ("Refresh the login (aws sso login) and plan again. Until then "
                           "this cannot confirm whether a replication instance is already "
                           "running and billing."),
            })

    # Tables come from discovery, named rather than wildcarded: a table added to
    # the source after planning should not join the migration unnoticed.
    run_id = recs["sizing"]["collector_run_id"]
    objects = prov_records._dataset(run_id, "objects")
    selection = mappings.select_tables(
        prov_records._dataset(run_id, "tables"), objects, estate,
        external_tables=prov_records._dataset(run_id, "external_tables"),
        queues=prov_records._dataset(run_id, "queues"))
    tables = selection["include"]

    tm = mappings.table_mappings(schema=estate, tables=tables, lowercase=heterogeneous)
    ts = mappings.task_settings(migration_type=migration_type)

    return {
        "planned_at_utc": datetime.now(timezone.utc).isoformat(),
        "estate": estate,
        "collector_run_id": recs["sizing"]["collector_run_id"],
        "migration_type": migration_type,
        "migration_type_declared_in_phase_1": declared,
        # A Phase 7 flag disagreeing with the Phase 1 decision is allowed --
        # someone may deliberately rehearse a full load before a CDC run -- but
        # it is never silent, because the gate judged the other one.
        "migration_type_overridden": overridden,
        "heterogeneous": heterogeneous,
        "target_engine": d.get("engine") or "ORACLE",
        "instance": {"name": policy.instance_name(estate), "class": policy.INSTANCE_CLASS,
                     "storage_gb": policy.STORAGE_GB, "engine_version": policy.ENGINE_VERSION,
                     "multi_az": policy.MULTI_AZ},
        "task": {"name": policy.task_name(estate, migration_type)},
        "tables": tables,
        "tables_excluded": selection["exclude"],
        # What DMS will not finish, routed to rule / model / person. The model
        # tier cannot run while Bedrock is blocked, so those items are answered
        # from labelled stand-ins or reported as MODEL_REQUIRED.
        "residue": residue.build(
            # The model tier answers the residue items that need judgement --
            # rewriting a view's SQL. Hardcoding this False dated from when
            # Bedrock was blocked; it now follows whether the tier actually
            # invokes, so a live run does not report MODEL_REQUIRED for work
            # the model could have done.
            schema=estate, lowercase=heterogeneous, model_available=_model_live(),
            datasets={n: prov_records._dataset(run_id, n)
                      for n in ("sequences", "views", "materialized_views", "external_tables")}),
        "not_moved_by_dms": mappings.excluded_objects(objects, estate),
        "table_mappings": tm,
        "task_settings": ts,
        "lowercase_names": heterogeneous,
        "provision_stack": (prov_plan or {}).get("stack_name"),
        **preflight.summarise(checks),
    }


def execute(session, *, confirm_account: str, migration_type: str | None = None,
            source: dict, target: dict, on_event=None, target_counts: dict | None = None) -> dict:
    """Create the instance, endpoints and task, then run it. **This bills.**"""
    events: list[dict] = []

    def emit(e):
        events.append(e)
        if on_event:
            on_event(e)

    account = session.client("sts").get_caller_identity()["Account"]
    if confirm_account != account:
        raise PermissionError(
            f"refusing to create DMS resources: --confirm must be the account these credentials "
            f"resolve to ({account}), and it was {confirm_account!r}")

    p = plan(session, migration_type=migration_type, target_counts=target_counts)
    if not p["ready"]:
        raise ValueError("preflight refused: " + ", ".join(p["refused_because"]))

    record = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
              "plan": {k: v for k, v in p.items() if k not in ("table_mappings", "task_settings")},
              "events": events}
    _save(record)

    dms = session.client("dms")
    try:
        ri = actions.create_instance(dms, session.client("ec2"), estate=p["estate"],
                                     run_id=p["collector_run_id"], emit=emit)
        record["instance_arn"] = ri["ReplicationInstanceArn"]
        _save(record)

        eps = actions.create_endpoints(dms, estate=p["estate"], run_id=p["collector_run_id"],
                                       source=source, target=target, emit=emit)
        for role in ("source", "target"):
            emit({"event": "step", "detail": f"testing the {role} endpoint"})
            actions.test_endpoint(dms, endpoint_arn=eps[role]["EndpointArn"],
                                  instance_arn=ri["ReplicationInstanceArn"], emit=emit)
        record["endpoints"] = {r: eps[r]["EndpointArn"] for r in eps}
        _save(record)

        task = actions.create_task(
            dms, estate=p["estate"], run_id=p["collector_run_id"], migration_type=migration_type,
            instance_arn=ri["ReplicationInstanceArn"],
            source_arn=eps["source"]["EndpointArn"], target_arn=eps["target"]["EndpointArn"],
            table_mappings=json.dumps(p["table_mappings"]),
            task_settings=json.dumps(p["task_settings"]), emit=emit)
        record["task_arn"] = task["ReplicationTaskArn"]
        _save(record)

        actions.start_task(dms, task_arn=task["ReplicationTaskArn"], emit=emit)
        progress = actions.wait_for_full_load(dms, task["ReplicationTaskArn"], emit=emit)
        record["full_load"] = progress
        record["tables"] = actions.table_statistics(dms, task["ReplicationTaskArn"])

        errored = [t for t in record["tables"] if t["state"] in ("Table error", "Error")]
        record["status"] = "failed" if errored else (
            "replicating" if migration_type != policy.FULL_LOAD else "loaded")
        record["tables_errored"] = len(errored)
        if migration_type != policy.FULL_LOAD:
            record["cdc"] = actions.cdc_latency(
                session.client("cloudwatch"), instance_name=p["instance"]["name"],
                task_id=task["ReplicationTaskIdentifier"])
        emit({"event": "complete", "status": record["status"],
              "tables": len(record["tables"]), "errored": len(errored)})
    except Exception as exc:  # noqa: BLE001
        record["status"] = "error"
        record["error"] = str(exc)
        emit({"event": "error", "message": str(exc)})
    finally:
        record["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        _save(record)
    return record


def status(session) -> dict | None:
    """Where a previously started task stands now. Read-only."""
    rec = last_run()
    if not rec or not rec.get("task_arn"):
        return None
    dms = session.client("dms")
    out = {"task_arn": rec["task_arn"], "estate": rec["plan"]["estate"],
           "migration_type": rec["plan"]["migration_type"],
           **actions.task_progress(dms, rec["task_arn"])}
    out["tables"] = actions.table_statistics(dms, rec["task_arn"])
    if rec["plan"]["migration_type"] != policy.FULL_LOAD:
        out["cdc"] = actions.cdc_latency(
            session.client("cloudwatch"), instance_name=rec["plan"]["instance"]["name"],
            task_id=rec["task_arn"].rsplit(":", 1)[-1])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DBShift Phase 7 -- migrate with AWS DMS")
    ap.add_argument("--migration-type", default=None, choices=list(policy.MIGRATION_TYPES),
                    help="defaults to the mode declared in Phase 1 discovery. Passing it "
                         "here overrides that, and the plan records the disagreement.")
    ap.add_argument("--execute", action="store_true", help="create and run. THIS BILLS.")
    ap.add_argument("--confirm", type=str, default=None, help="the AWS account id, typed back")
    ap.add_argument("--status", action="store_true", help="where the running task stands")
    ap.add_argument("--stop", action="store_true", help="stop the task")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    # Not "localhost": a replication instance runs inside AWS, and localhost
    # there is the replication instance itself. The source address must be one
    # AWS can route to -- a public address, a VPN, or Direct Connect -- which is
    # exactly what the Phase 1 network requirements tell a client they need.
    ap.add_argument("--source-host", default=None,
                    help="an address AWS can reach. NOT localhost: DMS runs inside AWS.")
    ap.add_argument("--source-port", type=int, default=1521)
    ap.add_argument("--source-user", default=None, help="defaults to the estate owner")
    ap.add_argument("--source-database", default="XEPDB1")
    args = ap.parse_args(argv)

    session = _session(args.profile)

    if args.status:
        s = status(session)
        if not s:
            print("no DMS task has been started from this machine")
            return 1
        print(json.dumps(s, indent=2, default=str))
        return 0

    if args.stop:
        rec = last_run()
        if not rec or not rec.get("task_arn"):
            print("no task to stop")
            return 1
        actions.stop_task(session.client("dms"), task_arn=rec["task_arn"],
                          emit=lambda e: print(e.get("detail", "")))
        print("stopped. The replication instance is still billing -- "
              "delete it with `python -m killswitch --destroy --confirm <account>`.")
        return 0

    p = plan(session if args.execute else None, migration_type=args.migration_type)
    print(f"\nestate           : {p['estate']}   run {p['collector_run_id']}")
    print(f"target           : {p['target_engine']}"
          + ("  (heterogeneous -- names lowercased)" if p["heterogeneous"] else ""))
    print(f"migration type   : {p['migration_type']}"
          + ("  (OVERRIDES the Phase 1 declaration of "
             f"{p['migration_type_declared_in_phase_1']})"
             if p["migration_type_overridden"] else "  (declared in Phase 1)"))
    print(f"instance         : {p['instance']['name']} {p['instance']['class']} "
          f"{p['instance']['storage_gb']} GB")
    print(f"tables           : {len(p['tables'])} moved, "
          f"{len([e for e in p['tables_excluded'] if not e.get('included')])} held back")
    print(f"not moved by DMS : {len(p['not_moved_by_dms'])} object(s)")
    r = p["residue"]
    print(f"left to finish   : {r['total']} item(s) -- "
          + ", ".join(f"{k.lower().replace('_', ' ')} {v}" for k, v in sorted(r["by_status"].items())))

    print("\nPREFLIGHT (read-only)")
    for c in p["checks"]:
        print(f"  [{c['status'].upper():7}] {c['name']:28} {c['detail']}")
        if c.get("remedy"):
            print(f"{'':41}-> {c['remedy']}")

    if not args.execute:
        print(f"\nready: {p['ready']}")
        if p["ready"]:
            print(f"to run: python -m dms.run --migration-type {p['migration_type']} "
                  f"--execute --confirm <account-id>")
        return 0 if p["ready"] else 2

    if not args.confirm:
        print("\n--execute needs --confirm <account-id>. Nothing was created.")
        return 2

    if not args.source_host:
        print("\n--source-host is required and must be an address AWS can reach.\n"
              "  A replication instance runs inside AWS: 'localhost' there is the\n"
              "  replication instance, not your database. Supply a public address, or\n"
              "  the private address behind a VPN or Direct Connect.\n"
              "Nothing was created.")
        return 2
    if args.source_host in ("localhost", "127.0.0.1", "::1"):
        print(f"\nrefusing --source-host {args.source_host!r}: that address means the\n"
              "  replication instance itself, not your database. Nothing was created.")
        return 2
    source = {"engine": "oracle", "host": args.source_host, "port": args.source_port,
              "user": args.source_user or p["estate"],
              "password": os.environ.get("DBSHIFT_SOURCE_OWNER_PASSWORD", ""),
              "database": args.source_database}
    target = target_from_deployment(session)
    if not target:
        print("\nno provisioned target: run Phase 6 first. Nothing was created.")
        return 2
    if target["stack"] != p.get("provision_stack"):
        print(f"\nthe deployed stack is {target['stack']!r} but this plan expects "
              f"{p.get('provision_stack')!r}. Re-render Phase 6 for this target. "
              "Nothing was created.")
        return 2
    if not source["password"] or not target["password"]:
        print("\nset DBSHIFT_SOURCE_OWNER_PASSWORD and DBSHIFT_PG_PASSWORD. Nothing was created.")
        return 2

    rec = execute(session, confirm_account=args.confirm, migration_type=args.migration_type,
                  source=source, target=target,
                  on_event=lambda e: print("  " + (e.get("detail") or e.get("message") or "")))
    print(f"\nstatus: {rec['status']}")
    print(f"record: {RECORD}")
    return 0 if rec["status"] in ("loaded", "replicating") else 1


if __name__ == "__main__":
    raise SystemExit(main())
