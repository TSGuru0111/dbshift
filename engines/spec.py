"""Per-source-engine facts, as data rather than branches.

Every value here is something a later phase would otherwise discover with an
`if engine == "ORACLE"`. Collected in one place because the alternative -- the
same conditional spelled into the collector, the SCT scenario, the DMS mappings
and the validator -- is how four copies of a fact drift apart.

The test for whether something belongs here: **could a phase read it and act,
without knowing which engine it came from?** `identifier_folding` passes, because
the DMS mapping rule is "fold the way the source folds". `dictionary_schemas`
passes. How to parse MySQL's SQL/PSM does not -- that is conversion logic with
one caller, and it lives in `convert/`.
"""

from __future__ import annotations

from . import MYSQL, ORACLE, EngineError, normalize

# Identifier folding. Oracle folds unquoted identifiers to UPPER, MySQL stores
# them as written (and on Linux compares them case-sensitively for tables).
#
# This is the fact Phase 7's table mappings need. The existing Oracle->PostgreSQL
# mappings fold every name DOWN, which is correct *because* Oracle folded it up
# in the first place. Applying the same blanket down-fold to a MySQL source is
# wrong for any mixed-case table name -- it would rename the table mid-migration
# and Phase 8 would then report it missing.
UPPER = "upper"
PRESERVE = "preserve"

# Where the catalogue lives. Oracle's DBA_* views need explicit grants; MySQL's
# information_schema is readable by anyone but shows only what the account can
# see, which is a different failure mode: not "access denied" but "fewer rows".
# Phase 1's preflight has to check reach differently for each.
SPECS = {
    ORACLE: {
        "engine": ORACLE,
        "label": "Oracle",
        "product": "Oracle Database",
        "default_port": 1521,
        # host:port/service -- a service name, not a SID. XEPDB1 is a pluggable
        # database and is only reachable by service name.
        "dsn_shape": "host:port/service",
        "dsn_example": "localhost:1521/XEPDB1",
        "driver_module": "oracledb",
        "identifier_folding": UPPER,
        "quote_char": '"',
        "dictionary_schemas": ("SYS", "SYSTEM"),
        # Objects the engine generates and manages. Never migrated, never
        # assessed -- without this exclusion every structural rule fires on
        # internals as a false positive.
        "internal_prefixes": ("DR$", "AQ$", "MLOG$", "RUPD$", "SYS_IOT", "BIN$"),
        # Schema names are folded up by the engine, so the collector may fold
        # them up too without changing which schema it reads.
        "fold_schema_names": True,
        "has_licence_model": True,
        "cdc_requirements": ("ARCHIVELOG", "supplemental logging"),
        "collector_privileges": (
            "SELECT on the DBA_* catalogue views",
            "explicit SELECT on each profiled table",
        ),
        "sct_privilege": "SELECT ANY DICTIONARY",
    },
    MYSQL: {
        "engine": MYSQL,
        "label": "MySQL",
        "product": "MySQL",
        "default_port": 3306,
        # No service name. A MySQL connection names a host, a port and
        # optionally a default database -- which is a *schema*, not a container.
        "dsn_shape": "host:port/database",
        "dsn_example": "10.0.1.42:3306/dbmig_mysql_app",
        "driver_module": "pymysql",
        "identifier_folding": PRESERVE,
        "quote_char": "`",
        "dictionary_schemas": ("information_schema", "performance_schema",
                              "mysql", "sys"),
        # MySQL generates far less on the user's behalf than Oracle does. The
        # notable case is a copied table left behind by a failed online DDL,
        # which is named with these prefixes.
        "internal_prefixes": ("#sql-", "#sql2-"),
        # NOT folded. A MySQL schema is a directory on disk, so on Linux
        # `Sales` and `SALES` are different databases. Upper-casing a schema
        # name here is how the collector silently reads nothing.
        "fold_schema_names": False,
        # No editions, no processor licences, no BYOL arithmetic. The same
        # simplification the PostgreSQL target already enjoys.
        "has_licence_model": False,
        "cdc_requirements": ("binlog_format=ROW", "binlog_row_image=FULL",
                             "binlog retention", "REPLICATION CLIENT"),
        "collector_privileges": (
            "SELECT on the profiled schemas",
            "SHOW VIEW (view definitions are hidden without it)",
            "PROCESS (server-wide status)",
            "REPLICATION CLIENT (binlog position and CDC readiness)",
        ),
        "sct_privilege": None,  # MySQL needs no dictionary grant beyond the above
    },
}

LABEL_OF = {e: s["label"] for e, s in SPECS.items()}


# ---------------------------------------------------------------------------
# The legal (source, target) pairs.
#
# Target names match sizing/target.py's vocabulary so a pair can be handed
# straight to Phase 3 without a translation table.

ORACLE_TARGET = "ORACLE"
POSTGRESQL_TARGET = "POSTGRESQL"
MYSQL_TARGET = "MYSQL"

# What each source may migrate to, and what each pair *is*. "homogeneous" and
# "heterogeneous" are not decoration: they decide whether Phase 4b/4c/4d run at
# all, whether SCT has conversion work to report, and whether Phase 8 compares
# bytes or canonical text.
PAIRS = {
    (ORACLE, ORACLE_TARGET): {
        "kind": "homogeneous",
        "label": "Oracle to Amazon RDS for Oracle",
        "converts_code": False,
        "data_mover": "datapump",
        "sct_conversion": False,
        "sct_assessment": True,
    },
    (ORACLE, POSTGRESQL_TARGET): {
        "kind": "heterogeneous",
        "label": "Oracle to Amazon RDS for PostgreSQL",
        "converts_code": True,
        "data_mover": "dms",
        "sct_conversion": True,
        "sct_assessment": True,
    },
    (MYSQL, MYSQL_TARGET): {
        "kind": "homogeneous",
        "label": "MySQL to Amazon RDS for MySQL",
        "converts_code": False,
        # One mover for both MySQL targets. Data Pump is an Oracle-only format,
        # and DMS reads MySQL's binlog for CDC on either target -- so unlike the
        # Oracle paths, the homogeneous case does not get a different tool.
        "data_mover": "dms",
        # AWS publishes no MySQL -> MySQL *conversion* path, because there is
        # nothing to convert. SCT still produces a same-engine assessment
        # (compatibility and licence/cost), which is a weaker but real claim.
        #
        # Phase 5 must read "no conversion action items" here as CLEAR with a
        # reason -- not as missing evidence. Absent evidence reporting blocked is
        # the right default everywhere else and would be wrong exactly here.
        "sct_conversion": False,
        "sct_assessment": True,
    },
    (MYSQL, POSTGRESQL_TARGET): {
        "kind": "heterogeneous",
        "label": "MySQL to Amazon RDS for PostgreSQL",
        "converts_code": True,
        "data_mover": "dms",
        "sct_conversion": True,
        "sct_assessment": True,
    },
}


def spec(engine: str | None) -> dict:
    """The facts for one source engine."""
    return SPECS[normalize(engine)]


def targets_for(engine: str | None) -> tuple[str, ...]:
    """Which targets this source may migrate to, in display order.

    Homogeneous first, because it is the weaker claim and the easier sell: a
    client comparing paths should see the like-for-like option before the one
    that rewrites their stored code.
    """
    src = normalize(engine)
    found = [t for (s, t) in PAIRS if s == src]
    order = {ORACLE_TARGET: 0, MYSQL_TARGET: 0, POSTGRESQL_TARGET: 1}
    return tuple(sorted(found, key=lambda t: (order.get(t, 9), t)))


def pair(engine: str | None, target: str | None) -> dict:
    """The facts for one (source, target) pair, or refuse it by name.

    Refusing here rather than returning None is the point: a caller that forgets
    to check gets an exception naming what is legal, instead of a KeyError or a
    silently empty plan.
    """
    src = normalize(engine)
    tgt = str(target or "").strip().upper()
    try:
        return PAIRS[(src, tgt)]
    except KeyError:
        raise EngineError(
            f"{LABEL_OF.get(src, src)} does not migrate to {tgt or '(none)'} in this "
            f"project. From {LABEL_OF.get(src, src)} the supported targets are: "
            + ", ".join(targets_for(src))
        ) from None


def is_legal(engine: str | None, target: str | None) -> bool:
    try:
        pair(engine, target)
        return True
    except EngineError:
        return False


def converts_code(engine: str | None, target: str | None) -> bool:
    """Do Phases 4b/4c/4d have anything to do on this pair?"""
    return bool(pair(engine, target)["converts_code"])


def data_mover(engine: str | None, target: str | None) -> str:
    """'datapump' or 'dms'. Phase 7 reads this instead of testing for PostgreSQL."""
    return pair(engine, target)["data_mover"]


def folds_identifiers_up(engine: str | None) -> bool:
    """Did the source engine upper-case unquoted identifiers?

    Phase 7's mapping rules need this, not a test for the target engine. A name
    is folded down for PostgreSQL because Oracle folded it up; a MySQL name was
    stored as written and must be carried across unchanged.
    """
    return spec(engine)["identifier_folding"] == UPPER
