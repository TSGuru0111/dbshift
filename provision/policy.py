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
