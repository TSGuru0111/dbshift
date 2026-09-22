"""What a DMS migration is allowed to be.

Deterministic, no model. Every value is a cost or safety decision, and each is
explained where it is set.

**DMS bills differently from everything else in this project.** A Data Pump
export finishes and stops; a replication instance bills by the hour for as long
as it exists, whether or not a task is running. That single fact drives most of
the choices here: the smallest instance that works, the shortest storage, no
Multi-AZ, and a TTL that makes an abandoned instance visible.
"""

from __future__ import annotations

# The kill switch acts on `dbshift*` names, so the prefix is load-bearing.
PREFIX = "dbshift-"

REGION = "ap-south-1"

# dms.t3.small is the smallest orderable class in this region (verified with
# describe-orderable-replication-instances on 2026-09-14). At this estate's size
# the work is trivial; a larger class would only cost more per hour.
INSTANCE_CLASS = "dms.t3.small"

# 5 GB is the documented minimum. Replication storage holds cached changes and
# task logs, not the data itself -- the rows stream through memory to the
# target. Nothing here needs to hold a 1 GB estate.
STORAGE_GB = 5

# Pinned rather than "latest": an engine upgrade between a rehearsal and the
# real run would change behaviour underneath a proven plan.
ENGINE_VERSION = "3.5.4"

MULTI_AZ = False              # a standby doubles the hourly rate; a migration needs none
PUBLICLY_ACCESSIBLE = True    # the source is on-premises and no VPN exists in this account
AUTO_MINOR_UPGRADE = False    # a pinned version keeps runs reproducible

# Hours after which an instance is considered abandoned. Stamped as a tag, the
# same way Phase 6 stamps the RDS target, so the kill switch and a human both
# see it.
TTL_HOURS = 8

# Transport encryption on the endpoints.
#
# DMS defaults to `none`, and RDS for PostgreSQL refuses an unencrypted
# connection outright:
#
#   FATAL: no pg_hba.conf entry for host "...", user "...", no encryption
#
# `require` encrypts without verifying the server certificate, which is what
# works against RDS without distributing a CA bundle to the replication
# instance. `verify-ca` is stronger and needs that bundle; it is the right
# setting for a production migration and the reason this is a named constant
# rather than a literal.
#
# The source is on-premises over the public internet, so encryption there is
# not optional either -- though an Oracle source needs its listener configured
# for TCPS before `require` can work, which most are not. It is left at the
# DMS default and the preflight reports it.
TARGET_SSL_MODE = "require"
SOURCE_SSL_MODE = "none"

# --- task settings -----------------------------------------------------------

# DMS migration types, in its own vocabulary.
FULL_LOAD = "full-load"
FULL_LOAD_AND_CDC = "full-load-and-cdc"
CDC_ONLY = "cdc"

MIGRATION_TYPES = (FULL_LOAD, FULL_LOAD_AND_CDC, CDC_ONLY)

# What DMS does with rows already in a target table when a full load starts.
#
# DO_NOTHING, deliberately. TRUNCATE_BEFORE_LOAD and DROP_AND_CREATE both
# destroy target data, and a migration tool that silently empties a table it did
# not create is the wrong default however convenient. A non-empty target is
# something a person should see and decide about, so the preflight checks for it
# and refuses rather than the task quietly overwriting.
TARGET_TABLE_PREP = "DO_NOTHING"

# LOB handling. DMS cannot stream a LOB of unknown length inline, so it either
# takes a declared maximum ("limited") or makes a second pass per LOB ("full").
# Limited is far faster and truncates silently past the limit -- which is a data
# loss no validation on row counts would catch.
#
# 32 MB covers every LOB the collector has seen on these estates. Phase 8
# compares LOB columns explicitly, so truncation would be caught; this limit is
# chosen to make that never fire, not to rely on it.
LOB_MAX_KB = 32768

# Logging. CloudWatch logs for a task cost little and are the only way to see
# why a table was suspended. DEFAULT keeps the volume sane.
CLOUDWATCH_LOGS = True

# How long to wait for a full load before treating the task as stuck.
#
# Was 30, on the assumption that "a 1 GB estate loads in minutes". DBMIG_TELCO
# is 5.7 GB and 32,971,741 rows across 11 tables, and on 2026-09-22 it took 47
# minutes -- so the console gave up watching at 99%, recorded
# `status: error, full load did not finish within 30 minutes`, and showed a
# failed migration while DMS went on to finish all 11 tables with zero errors
# and exact row-count matches. A watcher timing out is not the load failing,
# and reporting it as one is worse than waiting: it invites someone to re-run
# a migration that already succeeded, against a target that is no longer empty.
# Four hours is a ceiling for a genuinely stuck task, not an expectation.
FULL_LOAD_TIMEOUT_MINUTES = 240

# CDC latency, in seconds, at or below which a cutover may be considered. This
# is the number that makes a short outage window possible: the target is at most
# this far behind when the source is quiesced.
CDC_CUTOVER_LATENCY_SECONDS = 30


def instance_name(estate: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (estate or "").lower()).strip("-")
    return f"{PREFIX}dms-{slug}"


def security_group_name(estate: str) -> str:
    """The replication instance's own security group.

    Without one AWS attaches the VPC **default** group, which is what happened
    on 2026-09-21: every rule the operator had written named a purpose-built
    `dbshift-dms-sg` that nothing was using, so the source endpoint test failed
    with `ORA-12170` -- indistinguishable from the NAT problem the whole EC2
    host was built to solve. A group the instance actually carries is what the
    source and target grant to.
    """
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (estate or "").lower()).strip("-")
    return f"{PREFIX}dms-sg-{slug}"


def task_name(estate: str, migration_type: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (estate or "").lower()).strip("-")
    kind = {FULL_LOAD: "full", FULL_LOAD_AND_CDC: "full-cdc", CDC_ONLY: "cdc"}[migration_type]
    return f"{PREFIX}task-{slug}-{kind}"


def endpoint_name(estate: str, role: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (estate or "").lower()).strip("-")
    return f"{PREFIX}{role}-{slug}"
