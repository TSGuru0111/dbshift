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

# REGION is dynamic -- see __getattr__ at the foot of this file and awsregion.py.

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
PUBLICLY_ACCESSIBLE = True    # the endpoint is public and the security group admits one /32 only.
                              # This was originally forced -- ec2:RunInstances was denied account-wide,
                              # so no bastion was possible. That deny was lifted by 2026-09-28
                              # (see docs/05-aws-services.md), so the premise no longer holds; the
                              # setting stays because a single-/32 public endpoint is still the
                              # cheapest correct answer for a rehearsal target, not because it is forced
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


# --- MySQL target --------------------------------------------------------------
# The homogeneous MySQL path. No editions, no licence, no option group: the
# settings that matter live in a DB parameter group, rendered from the SOURCE's
# own values so the application meets the server it was written against.

MYSQL_ENGINE = "mysql"
MYSQL_LICENCE = "general-public-license"

# Majors this project renders. Which one is used is decided by preflight from
# RDS's own lifecycle data (describe-db-major-engine-versions): the source's
# major while it is in RDS standard support, else the next major that is --
# because a major past standard support bills RDS Extended Support per vCPU-hour
# on top of the instance, and a "same version as the source" default would put
# that charge on a rehearsal target without anyone choosing it. MySQL 8.0's
# community life ended in April 2026.
MYSQL_MAJORS = ("8.0", "8.4")

MYSQL_PORT = 3306
MYSQL_MASTER_USERNAME = "dbshiftadm"
MYSQL_DB_NAME = "dbshift"

# Server settings copied from the source into the parameter group. Each changes
# what the application's SQL does, so a target that differs is a behaviour change
# nobody decided. Read from the collector's `parameters` dataset (SHOW VARIABLES).
MYSQL_CARRIED = {
    "sql_mode": "decides what the server accepts: a stricter target rejects writes the "
                "application makes today, a looser one silently accepts bad data",
    "character_set_server": "the default for new tables and for columns without their own "
                            "charset; utf8mb4 keeps 4-byte characters",
    "collation_server": "decides string equality and ordering -- _ci makes 'A' = 'a' true, "
                        "which is also what uniqueness is judged by",
    "event_scheduler": "the estate has scheduled EVENTs; with the scheduler OFF they are "
                       "created on the target and never fire",
    "explicit_defaults_for_timestamp": "changes how a TIMESTAMP column with no default behaves "
                                       "on INSERT",
    "lower_case_table_names": "can only be set when the instance is created; a mismatch makes "
                              "table names resolve differently from the source",
}

# SHOW VARIABLES reports these booleans as ON/OFF, but the RDS parameter group
# accepts only 0/1 for them (event_scheduler, by contrast, takes ON/OFF). Found by
# the preflight's live check against describe-engine-default-parameters.
MYSQL_BOOLEAN_AS_01 = {"explicit_defaults_for_timestamp", "local_infile",
                       "log_bin_trust_function_creators"}

# Set regardless of the source, because RDS itself requires it.
MYSQL_FORCED = {
    "log_bin_trust_function_creators": (
        "1",
        "RDS grants no SUPER, and binary logging is on whenever backups are. Without this, "
        "CREATE FUNCTION and CREATE TRIGGER from the schema load fail with ERROR 1419 -- the "
        "estate's stored code would not arrive at all"),
    "local_infile": (
        "1",
        "AWS DMS loads a MySQL target with LOAD DATA LOCAL INFILE; with this off the full load "
        "cannot write a row (AWS DMS user guide, MySQL as a target, prerequisites)"),
}


def __getattr__(name):
    # `policy.REGION` is read at call time from awsregion, so the region chosen in
    # the console reaches every AWS call that already spells it `policy.REGION`.
    # Import-time uses (f-strings at module level) would freeze the first value;
    # there are none, and the selftest scans for them.
    if name == "REGION":
        import awsregion
        return awsregion.current()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

