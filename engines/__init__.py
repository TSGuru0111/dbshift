"""Which engine the estate is migrating *from*.

Until 2026-09-28 there was one answer, Oracle, and it was spelled into every
module that needed it -- `import oracledb` at the top of a probe, `ORACLE_VENDOR`
in the SCT scenario, `dba_tables` in the SQL. That was the right shape while the
scope was "one source engine, two targets": an abstraction over a set of one is
a cost with no return.

MySQL as a second source changes the arithmetic, and this module is the seam.

**What lives here:** the engine names, the legal (source, target) pairs, and the
per-engine facts other phases branch on -- as data, in `spec.py`. Nothing else.
This module knows *that* MySQL folds identifiers down and Oracle folds them up;
it does not know how to connect to either, collect from either or convert
between them. Those belong to the phase that does the work.

**Why the pairs live here and nowhere else.** MySQL -> RDS for Oracle is not a
migration this project performs, and neither is Oracle -> RDS for MySQL. Both
are easy to ask for by accident: the target picker offers two engines, the
source picker offers two engines, and four combinations look available when only
four-minus-two are. Refusing them in one place means a new caller cannot forget
to -- and `pairs.selftest` asserts exactly that.

**Oracle stays the default**, so a caller that says nothing behaves as it did
before this module existed. That is deliberate: every existing script, every
docs example and the whole proven Oracle path predate the source-engine flag,
and none of them should have to learn about it to keep working.
"""

from __future__ import annotations

ORACLE = "ORACLE"
MYSQL = "MYSQL"

SOURCE_ENGINES = (ORACLE, MYSQL)

# The environment variable, named like its siblings (DBSHIFT_DSN,
# DBSHIFT_SCHEMAS, DBSHIFT_MIGRATION_MODE) so it is guessable from the others.
ENV = "DBSHIFT_SOURCE_ENGINE"

# Oracle, for the reason in the module docstring: the path that already works
# must not need a new flag to keep working.
DEFAULT = ORACLE

LABEL = {
    ORACLE: "Oracle",
    MYSQL: "MySQL",
}


class EngineError(ValueError):
    """An unknown engine, or a (source, target) pair this project will not run."""


def normalize(value: str | None) -> str:
    """Accept the spellings a person or a script will actually produce.

    Deliberately permissive about case and separators and deliberately strict
    about everything else: a typo should fail here with a list of what is
    valid, not three phases later as an empty result set.
    """
    if value is None or not str(value).strip():
        return DEFAULT
    raw = str(value).strip().upper().replace("-", "").replace("_", "").replace(" ", "")
    aliases = {
        "ORACLE": ORACLE,
        "ORA": ORACLE,
        "ORACLEDB": ORACLE,
        "XE": ORACLE,
        "MYSQL": MYSQL,
        "MY": MYSQL,
        # MariaDB is wire-compatible and DMS treats it as a MySQL source. The
        # collector reads information_schema, which it also has. Accepting the
        # name is honest; claiming MariaDB is *tested* would not be, so the
        # record keeps the normalized value and docs/19 states the limit.
        "MARIADB": MYSQL,
        "MARIA": MYSQL,
    }
    if raw not in aliases:
        raise EngineError(
            f"unknown source engine {value!r}. Use one of: "
            + ", ".join(repr(e) for e in SOURCE_ENGINES)
        )
    return aliases[raw]


def is_oracle(engine: str | None) -> bool:
    return normalize(engine) == ORACLE


def is_mysql(engine: str | None) -> bool:
    return normalize(engine) == MYSQL
