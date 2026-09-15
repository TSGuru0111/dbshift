"""What a provisioned target is allowed to be.

Deterministic, no model. Every value here is a cost or safety decision for a
beta running on $100 of credit, and each is explained where it is set. The
values that describe the *estate* -- class, storage, edition, character set --
are not here: they come from the Phase 3 decision and are traced in the
rendered provenance.
"""

from __future__ import annotations

# CloudFormation is scoped to dbshift-* in the IAM policy, and the kill switch
# acts only on dbshift* names. The prefix is load-bearing twice over.
STACK_PREFIX = "dbshift-"

REGION = "ap-south-1"

# Edition -> (RDS engine, licence model). There is no licence-included EE on RDS.
ENGINE = {
    "EE": ("oracle-ee", "bring-your-own-license"),
    "SE2": ("oracle-se2", "license-included"),
}

# RDS for Oracle in ap-south-1 offers 19c only for EE -- verified with
# describe-db-engine-versions on 2026-09-11. A 21c source therefore migrates
# DOWN a version, which preflight reports; it is not something to configure away.
TARGET_MAJOR = "19"

# --- beta guardrails ---------------------------------------------------------
MULTI_AZ = False              # a standby doubles the instance bill; a rehearsal target needs none
BACKUP_RETENTION_DAYS = 1     # 0 disables backups entirely; 1 day is the cheapest real setting
DELETION_PROTECTION = False   # the kill switch must be able to remove it without an override
PUBLICLY_ACCESSIBLE = True    # no bastion is possible (ec2:RunInstances is denied), so the
                              # endpoint is public and the security group admits one /32 only
STORAGE_ENCRYPTED = True      # AWS-managed key; kms:CreateKey is denied by the permission set
AUTO_MINOR_UPGRADE = False    # a pinned version keeps rehearsal and validation reproducible
MONITORING_INTERVAL = 0       # enhanced monitoring needs its own role and bills CloudWatch
PERFORMANCE_INSIGHTS = False
TTL_HOURS = 8                 # stamped as expires-at; a target outliving a working day is a leak

MASTER_USERNAME = "dbshiftadm"
DB_NAME = "DBSHIFT"           # RDS Oracle DBName: at most 8 characters, starts with a letter
PORT = 1521

# The password never appears in a template, a file, or this repo. It lives in SSM
# Parameter Store as a SecureString (Secrets Manager is denied by the permission
# set) and CloudFormation resolves it at deploy time.
MASTER_PASSWORD_PARAMETER = "/dbshift/{stack}/master-password"


# --- account tagging ---------------------------------------------------------
#
# Every resource this project creates carries these, on top of the per-resource
# tags each phase adds. `Purpose=DMA` is a requirement of the AWS account the
# work now runs in, so it is set here rather than repeated at each call site:
# a resource created without it is a resource somebody has to find and fix.
#
# DBSHIFT_TAGS overrides, as `Key=Value,Key=Value`, for an account with
# different conventions.
REQUIRED_TAGS = {"Purpose": "DMA"}


def required_tags() -> dict:
    import os
    raw = os.environ.get("DBSHIFT_TAGS")
    if not raw:
        return dict(REQUIRED_TAGS)
    out = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out or dict(REQUIRED_TAGS)


def as_tag_list(extra: dict | None = None) -> list[dict]:
    """The required tags plus any caller tags, in the Key/Value shape AWS wants."""
    merged = {**required_tags(), **(extra or {})}
    return [{"Key": k, "Value": v} for k, v in merged.items()]


# --- PostgreSQL target -------------------------------------------------------
# The heterogeneous path. Everything the Oracle branch computes about editions,
# options and processor licences is absent: the engine is open source, so RDS
# charges for the instance alone and `LicenseModel` is `postgresql-license`.

PG_ENGINE = "postgres"
PG_LICENCE = "postgresql-license"

# Pinned to the major version the local compile gate runs, so what Phase 4b
# proves on Docker is what the target actually runs. 16 is current-generation
# and supported well past this project's horizon.
PG_TARGET_MAJOR = "16"

PG_PORT = 5432
PG_MASTER_USERNAME = "dbshiftadm"   # `postgres` is reserved by RDS
PG_DB_NAME = "dbshift"              # lower case: PostgreSQL folds unquoted identifiers

# PostgreSQL has no option groups and no CharacterSetName. Encoding is fixed at
# instance creation and RDS creates the database as UTF8, which is what the
# Phase 3 encoding check verifies the source can map to.
PG_ENCODING = "UTF8"

# S3 integration on RDS for PostgreSQL is an extension (aws_s3) enabled inside
# the database, not an option group. DMS is the data path here anyway, so the
# exchange bucket carries no dump for this engine -- it stays for the run
# artefacts the console writes.
