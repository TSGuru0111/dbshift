"""Render the RDS target as a CloudFormation template, from the records.

Pure: no AWS calls, no clock beyond the `now` it is given. Every property that
describes the estate comes from an upstream record, and each is written to a
provenance list saying which record and why -- that list is the point. A
reviewer should be able to ask of any line in the template "where did this come
from?" and get an answer that is not "the tool decided".

Why CloudFormation JSON rather than CDK, which the architecture names: CDK here
would mean a Node toolchain, a bootstrap stack, and explicit role names on every
construct (the IAM policy only admits dbshift-* roles). What CDK would produce is
this template. Rendering it directly keeps the output identical and reviewable
while dropping the toolchain. See docs/phases/phase-06-provision.md.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import policy


class RenderError(ValueError):
    pass


def stack_name_for(estate: str) -> str:
    return policy.STACK_PREFIX + "target-" + re.sub(r"[^a-z0-9]+", "-", estate.lower()).strip("-")


def _tags(stack: str, estate: str, run_id: str, expires: str) -> list[dict]:
    return [
        {"Key": "project", "Value": "dbshift"},
        {"Key": "estate", "Value": estate},
        {"Key": "collector_run_id", "Value": run_id},
        {"Key": "expires-at", "Value": expires},
        {"Key": "created-by", "Value": f"dbshift-provision/{stack}"},
    ]


def render(records: dict, facts: dict, *, engine_version: str, stack_name: str, estate: str,
           now: datetime) -> dict:
    sizing, gate = records["sizing"], records["gate"]
    assessment, remediation = records["assessment"], records["remediation"]
    d = sizing["decision"]
    run_id = sizing["collector_run_id"]
    prov: list[dict] = []

    def trace(prop, value, source, why):
        prov.append({"property": prop, "value": value, "source": source, "why": why})
        return value

    # ---- edition and licence ------------------------------------------------
    if d["edition"] not in policy.ENGINE:
        raise RenderError(f"no RDS engine for edition {d['edition']!r}")
    engine, licence = policy.ENGINE[d["edition"]]
    forced = ", ".join(x["feature"] for x in d.get("forced_by", [])) or "no forcing feature"
    trace("Engine", engine, "sizing.decision.edition (Phase 3)",
          f"{d['edition']} decided by the rules engine; forced by {forced}")
    trace("LicenseModel", licence, "sizing.decision.licence_model (Phase 3)",
          f"{d['licence_model']}: {d.get('processor_licences')} processor licence(s) must be held")
    if not engine_version.startswith(policy.TARGET_MAJOR + "."):
        raise RenderError(f"engine version {engine_version} is not {policy.TARGET_MAJOR}c")
    src_major = (facts.get("version") or "").split(".")[0]
    trace("EngineVersion", engine_version, "preflight: latest orderable in region",
          f"source is {facts.get('version_full')}; RDS offers {policy.TARGET_MAJOR}c only for "
          f"{engine}, so this is a version DOWNGRADE -- export with VERSION={policy.TARGET_MAJOR}"
          if src_major and src_major != policy.TARGET_MAJOR else "matches the source major version")

    # ---- size ----------------------------------------------------------------
    instance_class = trace("DBInstanceClass", d["instance_class"],
                           "sizing.decision.instance_class (Phase 3)",
                           f"{d['vcpu']} vCPU / {d['memory_gib']} GiB -- "
                           f"{sizing['facts']['utilization']['basis']}-derived"
                           + (", a floor rather than measured load"
                              if sizing["facts"]["utilization"]["basis"] == "capacity" else ""))
    storage_gb = trace("AllocatedStorage", str(d["storage_gb"]), "sizing.decision.storage_gb (Phase 3)",
                       f"{sizing['facts']['segment_gb']} GB of segments; RDS Oracle minimum is 20 GB")
    storage_type = trace("StorageType", d["storage_type"], "sizing.decision.storage_type (Phase 3)",
                         "gp3 includes baseline IOPS without provisioning them")

    # ---- character sets: cannot be changed after creation ---------------------
    cs = d["character_set"]
    if facts.get("nls_characterset") and facts["nls_characterset"] != cs:
        raise RenderError(f"sizing says {cs} but the source reports {facts['nls_characterset']}")
    for e in remediation.get("entries", []):
        art = e.get("artefact") or {}
        if art.get("applies_to_phase") == "provision" and art.get("parameter") == "CharacterSetName" \
                and art.get("value") != cs:
            raise RenderError(f"Phase 4 advice {e['rule_id']} says {art['value']}, sizing says {cs}")
    trace("CharacterSetName", cs, "sizing.decision.character_set, agreed by Phase 4 advice RDS-015",
          "an RDS Oracle character set cannot be changed after the instance exists")
    nchar = facts.get("nls_nchar_characterset")
    if nchar:
        trace("NcharCharacterSetName", nchar, "collector nls_parameters NLS_NCHAR_CHARACTERSET",
              "NCHAR/NVARCHAR2 columns load into this; it must match the source too")

    # ---- options derived from findings -----------------------------------------
    rules = {f["rule_id"] for f in assessment["findings"]}
    s3_why = ["Phase 7: a Data Pump dump reaches an RDS instance only through S3_INTEGRATION -- "
              "there is no server filesystem to copy it to"]
    if "RDS-003" in rules:
        s3_why.append("RDS-003: DIRECTORY objects have no local path on RDS")
    if "RDS-004" in rules:
        s3_why.append("RDS-004: the external table's source file must move to S3")
    trace("OptionGroup.S3_INTEGRATION", "enabled", "assessment findings + Phase 7 load path",
          "; ".join(s3_why))
    if "RDS-008" in rules:
        trace("OracleText", "not rendered", "assessment finding RDS-008",
              "UNVERIFIED whether an option is needed on RDS 19c. Checked after create with "
              "SELECT comp_id, status FROM dba_registry WHERE comp_id = 'CONTEXT' rather than "
              "guessed here")

    # ---- gate ---------------------------------------------------------------------
    trace("gate", gate["by_phase"]["provision"]["status"], "gate_decision.by_phase.provision (Phase 5)",
          f"overall verdict {gate['verdict']}; provision is blocked by "
          f"{gate['by_phase']['provision']['blocked_by'] or 'nothing'}")

    # ---- what later phases receive -----------------------------------------------
    handoffs = [{"rule_id": e["rule_id"], "object": e.get("object_name"),
                 "type": (e.get("artefact") or {}).get("type"),
                 "phase": (e.get("artefact") or {}).get("applies_to_phase")
                 or next((s.get("applies_to_phase") for s in (e.get("artefact") or {}).get("steps", [])), None)}
                for e in remediation.get("entries", []) if e.get("artefact")]
    handoffs = [h for h in handoffs if h["phase"] and h["phase"] != "provision"]

    expires = (now + timedelta(hours=policy.TTL_HOURS)).strftime("%Y-%m-%dT%H:%MZ")
    tags = _tags(stack_name, estate, run_id, expires)
    password_ref = "{{resolve:ssm-secure:" + policy.MASTER_PASSWORD_PARAMETER.format(stack=stack_name) + "}}"

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": f"DBShift migration target for {estate} (collector run {run_id}). "
                       f"Rendered by provision/render.py; see provision_plan.json for provenance.",
        "Parameters": {
            "VpcId": {"Type": "AWS::EC2::VPC::Id"},
            "SubnetIds": {"Type": "List<AWS::EC2::Subnet::Id>",
                          "Description": "At least two subnets in different AZs"},
            "OperatorCidr": {"Type": "String", "AllowedPattern": r"^\d{1,3}(\.\d{1,3}){3}/32$",
                             "Description": "The single /32 allowed to reach the listener"},
        },
        "Resources": {
            "DbSecurityGroup": {
                "Type": "AWS::EC2::SecurityGroup",
                "Properties": {
                    "GroupDescription": f"{stack_name}: Oracle listener from one operator /32 only",
                    "VpcId": {"Ref": "VpcId"},
                    "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": policy.PORT,
                                              "ToPort": policy.PORT, "CidrIp": {"Ref": "OperatorCidr"}}],
                    "Tags": tags,
                },
            },
            "DbSubnetGroup": {
                "Type": "AWS::RDS::DBSubnetGroup",
                "Properties": {"DBSubnetGroupDescription": f"{stack_name} subnets",
                               "SubnetIds": {"Ref": "SubnetIds"}, "Tags": tags},
            },
            "ExchangeBucket": {
                "Type": "AWS::S3::Bucket",
                # The kill switch empties this before deleting the stack; a
                # non-empty bucket would otherwise leave the stack DELETE_FAILED.
                "DeletionPolicy": "Delete",
                "Properties": {
                    "BucketName": {"Fn::Sub": "${AWS::StackName}-${AWS::AccountId}-exchange"},
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True, "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
                    "BucketEncryption": {"ServerSideEncryptionConfiguration": [
                        {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
                    "LifecycleConfiguration": {"Rules": [
                        {"Id": "expire-dumps", "Status": "Enabled", "ExpirationInDays": 7}]},
                    "Tags": tags,
                },
            },
            "S3IntegrationRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    # Explicit name: the permission set only admits roles named dbshift-*.
                    "RoleName": {"Fn::Sub": "${AWS::StackName}-s3-integration"},
                    "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [
                        {"Effect": "Allow", "Principal": {"Service": "rds.amazonaws.com"},
                         "Action": "sts:AssumeRole"}]},
                    "Policies": [{"PolicyName": "exchange-bucket-only", "PolicyDocument": {
                        "Version": "2012-10-17", "Statement": [
                            {"Effect": "Allow", "Action": ["s3:ListBucket"],
                             "Resource": {"Fn::GetAtt": ["ExchangeBucket", "Arn"]}},
                            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
                             "Resource": {"Fn::Sub": "${ExchangeBucket.Arn}/*"}}]}}],
                    "Tags": tags,
                },
            },
            "OptionGroup": {
                "Type": "AWS::RDS::OptionGroup",
                "Properties": {
                    "EngineName": engine, "MajorEngineVersion": policy.TARGET_MAJOR,
                    "OptionGroupDescription": f"{stack_name}: S3_INTEGRATION",
                    "OptionConfigurations": [{"OptionName": "S3_INTEGRATION", "OptionVersion": "1.0"}],
                    "Tags": tags,
                },
            },
            "DbInstance": {
                "Type": "AWS::RDS::DBInstance",
                # RDS defaults to Snapshot here, which leaves a billable snapshot
                # behind every stack delete. The source is the system of record.
                "DeletionPolicy": "Delete",
                "UpdateReplacePolicy": "Delete",
                "Properties": {
                    "DBInstanceIdentifier": stack_name,
                    "Engine": engine, "EngineVersion": engine_version, "LicenseModel": licence,
                    "DBInstanceClass": instance_class,
                    "AllocatedStorage": storage_gb, "StorageType": storage_type,
                    "StorageEncrypted": policy.STORAGE_ENCRYPTED,
                    "CharacterSetName": cs,
                    **({"NcharCharacterSetName": nchar} if nchar else {}),
                    "DBName": policy.DB_NAME,
                    "MasterUsername": policy.MASTER_USERNAME,
                    "MasterUserPassword": password_ref,
                    "Port": str(policy.PORT),
                    "MultiAZ": policy.MULTI_AZ,
                    "PubliclyAccessible": policy.PUBLICLY_ACCESSIBLE,
                    "DBSubnetGroupName": {"Ref": "DbSubnetGroup"},
                    "VPCSecurityGroups": [{"Fn::GetAtt": ["DbSecurityGroup", "GroupId"]}],
                    "OptionGroupName": {"Ref": "OptionGroup"},
                    "AssociatedRoles": [{"RoleArn": {"Fn::GetAtt": ["S3IntegrationRole", "Arn"]},
                                         "FeatureName": "S3_INTEGRATION"}],
                    "BackupRetentionPeriod": policy.BACKUP_RETENTION_DAYS,
                    "DeletionProtection": policy.DELETION_PROTECTION,
                    "AutoMinorVersionUpgrade": policy.AUTO_MINOR_UPGRADE,
                    "MonitoringInterval": policy.MONITORING_INTERVAL,
                    "EnablePerformanceInsights": policy.PERFORMANCE_INSIGHTS,
                    "CopyTagsToSnapshot": True,
                    "Tags": tags,
                },
            },
        },
        "Outputs": {
            "Endpoint": {"Value": {"Fn::GetAtt": ["DbInstance", "Endpoint.Address"]}},
            "Port": {"Value": {"Fn::GetAtt": ["DbInstance", "Endpoint.Port"]}},
            "ExchangeBucket": {"Value": {"Ref": "ExchangeBucket"}},
            "S3IntegrationRoleArn": {"Value": {"Fn::GetAtt": ["S3IntegrationRole", "Arn"]}},
        },
    }

    for prop, value, why in [
        ("MultiAZ", policy.MULTI_AZ, "beta guardrail: a standby doubles the instance bill"),
        ("BackupRetentionPeriod", policy.BACKUP_RETENTION_DAYS, "beta guardrail: cheapest real setting"),
        ("DeletionProtection", policy.DELETION_PROTECTION, "the kill switch must be able to remove it"),
        ("PubliclyAccessible", policy.PUBLICLY_ACCESSIBLE,
         "no bastion is possible; the security group admits one /32"),
        ("DeletionPolicy", "Delete", "RDS defaults to Snapshot, which leaves a billable snapshot"),
        ("MasterUserPassword", "SSM SecureString", "never in the template, a file, or the repo"),
        ("expires-at", expires, f"TTL {policy.TTL_HOURS} h; a target outliving a working day is a leak"),
    ]:
        trace(prop, value, "provision/policy.py", why)

    return {"template": template, "provenance": prov, "handoffs": handoffs,
            "stack_name": stack_name, "estate": estate, "collector_run_id": run_id,
            "password_parameter": policy.MASTER_PASSWORD_PARAMETER.format(stack=stack_name),
            "engine": engine, "licence": licence, "instance_class": instance_class,
            "storage_gb": int(storage_gb), "storage_type": storage_type}
