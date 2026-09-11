"""Everything that must be true before a deploy is even offered. Read-only.

Each check reports pass / warn / fail / blocked with a remedy, the same shape
as the connection preflight in Phase 1. A check that cannot run -- expired
credentials, say -- is BLOCKED, never quietly passed.

Nothing here creates, modifies or deletes anything. Every AWS call is a
Describe, List, Get or Validate.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.request

from . import policy

PASS, WARN, FAIL, BLOCKED = "pass", "warn", "fail", "blocked"


def _c(name, status, detail, remedy=None, **extra):
    return {"name": name, "status": status, "detail": detail, "remedy": remedy, **extra}


def gate_allows(gate: dict) -> dict:
    prov = gate["by_phase"]["provision"]
    if prov["status"] != "clear":
        return _c("gate_allows_provision", FAIL,
                  f"provision is blocked by {', '.join(prov['blocked_by'])}",
                  "Resolve or waive those findings in Phase 5.")
    if gate["verdict"] == "HALT":
        return _c("gate_allows_provision", WARN,
                  "nothing blocks provision itself, but the gate's overall verdict is HALT "
                  f"({gate['critical_findings']} critical findings open)",
                  "Rendering is free and proceeds. A DEPLOY under HALT needs either waivers in "
                  "Phase 5 or a named acknowledgement -- it is not taken silently.")
    return _c("gate_allows_provision", PASS, f"verdict {gate['verdict']}; provision clear")


def version_direction(facts: dict) -> dict:
    src = (facts.get("version") or "").split(".")[0]
    if not src:
        return _c("version_direction", WARN, "source version unknown", "Re-run discovery.")
    if int(src) > int(policy.TARGET_MAJOR):
        return _c("version_direction", WARN,
                  f"source is {facts.get('version_full')}, target will be {policy.TARGET_MAJOR}c -- a "
                  "DOWNGRADE. RDS offers no 21c for this engine in this region.",
                  f"Phase 7 must export with Data Pump VERSION={policy.TARGET_MAJOR}. Anything that "
                  f"exists only in 21c will not import; validate for it explicitly.")
    return _c("version_direction", PASS, f"source {src}c, target {policy.TARGET_MAJOR}c")


def aws_checks(session, *, engine: str, licence: str, instance_class: str, storage_type: str,
               stack_name: str) -> tuple[list[dict], dict]:
    """Read-only AWS facts. Returns (checks, resolved values for the render)."""
    checks, resolved = [], {}
    try:
        ident = session.client("sts").get_caller_identity()
        resolved["account"] = ident["Account"]
        checks.append(_c("aws_identity", PASS, f"account {ident['Account']} as {ident['Arn'].split('/')[-1]}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_c("aws_identity", BLOCKED, str(exc).splitlines()[0],
                         "Refresh the session credentials in the dbshift-static profile."))
        for n in ("engine_version", "network", "quota", "stack_name_free", "budget"):
            checks.append(_c(n, BLOCKED, "no valid credentials"))
        return checks, resolved

    region = policy.REGION
    rds = session.client("rds", region_name=region)
    ec2 = session.client("ec2", region_name=region)

    # engine version: latest orderable, standard patch line only
    try:
        versions = set()
        for page in rds.get_paginator("describe_orderable_db_instance_options").paginate(
                Engine=engine, LicenseModel=licence, DBInstanceClass=instance_class):
            for o in page["OrderableDBInstanceOptions"]:
                v = o["EngineVersion"]
                # .spb is the Spatial Patch Bundle line -- a specialised build, not the default.
                if o.get("StorageType") == storage_type and v.startswith(policy.TARGET_MAJOR + ".") \
                        and ".spb" not in v:
                    versions.add(v)
        if versions:
            resolved["engine_version"] = max(versions)
            checks.append(_c("engine_version", PASS,
                             f"{instance_class} {engine} {licence} {storage_type} orderable; "
                             f"latest {resolved['engine_version']} ({len(versions)} versions)"))
        else:
            checks.append(_c("engine_version", FAIL,
                             f"no {policy.TARGET_MAJOR}c version orderable for {instance_class} "
                             f"{engine} {licence} on {storage_type} in {region}",
                             "Pick another class in Phase 3, or another region."))
    except Exception as exc:  # noqa: BLE001
        checks.append(_c("engine_version", BLOCKED, str(exc).splitlines()[0]))

    # network: default VPC, one subnet per AZ, at least two AZs
    try:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            checks.append(_c("network", FAIL, "no default VPC", "Create a VPC stack first."))
        else:
            vpc = vpcs[0]["VpcId"]
            subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]
            by_az = {}
            for s in sorted(subnets, key=lambda s: s["SubnetId"]):
                by_az.setdefault(s["AvailabilityZone"], s["SubnetId"])
            if len(by_az) < 2:
                checks.append(_c("network", FAIL, f"{vpc} has subnets in {len(by_az)} AZ(s); RDS needs 2"))
            else:
                resolved["vpc_id"] = vpc
                resolved["subnet_ids"] = [by_az[az] for az in sorted(by_az)][:3]
                checks.append(_c("network", PASS,
                                 f"default {vpc}, subnets in {', '.join(sorted(by_az))}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_c("network", BLOCKED, str(exc).splitlines()[0]))

    # quota headroom
    try:
        used = sum(len(p["DBInstances"]) for p in rds.get_paginator("describe_db_instances").paginate())
        sq = session.client("service-quotas", region_name=region)
        limit = next((q["Value"] for p in sq.get_paginator("list_service_quotas").paginate(ServiceCode="rds")
                      for q in p["Quotas"] if q["QuotaName"] == "DB instances"), None)
        if limit is None or used < limit:
            checks.append(_c("quota", PASS, f"{used} DB instance(s) in use of {limit or 'unknown'}"))
        else:
            checks.append(_c("quota", FAIL, f"{used} of {limit} DB instances in use",
                             "Request a quota increase in Service Quotas."))
    except Exception as exc:  # noqa: BLE001
        checks.append(_c("quota", BLOCKED, str(exc).splitlines()[0]))

    # stack name not already taken
    try:
        cfn = session.client("cloudformation", region_name=region)
        cfn.describe_stacks(StackName=stack_name)
        checks.append(_c("stack_name_free", WARN, f"{stack_name} already exists",
                         "A deploy would update it. Run the kill switch first for a clean target."))
    except Exception as exc:  # noqa: BLE001
        if "does not exist" in str(exc):
            checks.append(_c("stack_name_free", PASS, f"{stack_name} does not exist yet"))
        else:
            checks.append(_c("stack_name_free", BLOCKED, str(exc).splitlines()[0]))

    # budget: report what exists; the user decided to keep the company budget as is
    try:
        budgets = session.client("budgets", region_name="us-east-1").describe_budgets(
            AccountId=resolved["account"]).get("Budgets", [])
        names = ", ".join(f"{b['BudgetName']} ${float(b['BudgetLimit']['Amount']):.0f}" for b in budgets)
        checks.append(_c("budget", PASS if budgets else WARN,
                         names or "no budget on this account",
                         None if budgets else "Create a budget with alerts before deploying."))
    except Exception as exc:  # noqa: BLE001
        checks.append(_c("budget", BLOCKED, str(exc).splitlines()[0]))

    return checks, resolved


def operator_cidr() -> dict:
    """This machine's public address, as the one /32 the listener admits."""
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as r:
            ip = r.read().decode().strip()
        ipaddress.IPv4Address(ip)
        return _c("operator_ip", PASS, "public address resolved; the listener will admit this /32 only",
                  cidr=f"{ip}/32")
    except Exception as exc:  # noqa: BLE001
        return _c("operator_ip", BLOCKED, f"could not resolve public address: {exc}",
                  "Pass --operator-cidr x.x.x.x/32 explicitly.")


def validate_template(session, template: dict) -> dict:
    try:
        cfn = session.client("cloudformation", region_name=policy.REGION)
        out = cfn.validate_template(TemplateBody=json.dumps(template))
        caps = out.get("Capabilities") or []
        return _c("template_valid", PASS,
                  "CloudFormation accepted the template"
                  + (f"; deploy needs {', '.join(caps)}" if caps else ""), capabilities=caps)
    except Exception as exc:  # noqa: BLE001
        return _c("template_valid", FAIL, str(exc).splitlines()[0])
