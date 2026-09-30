"""Connecting to a source engine, and the three queries the run itself needs.

`db.py` holds the driver-neutral `Session`; this module holds the parts that
cannot be neutral: how to open a connection, which exception class means "the
driver failed", how to bind an IN list, and the handful of catalogue queries
`run.py` issues directly rather than through a probe (schema discovery and the
identity snapshot).

**Why a dialect rather than a subclass.** The probes are the bulk of the
engine-specific SQL and they are already pluggable -- `probes/` for Oracle,
`probes_mysql/` for MySQL, each module a `collect(s, owners)`. What was left
hardcoded in `run.py` was three queries and a connect call. A dialect object
carrying those is smaller than a class hierarchy and keeps `Session` a plain
data holder, which is what let it stay driver-neutral in the first place.

**The bind style is the part with teeth.** Oracle takes named binds (`:o0`),
MySQL takes positional `%s`. Both exist so that schema names never reach the SQL
text as literals -- that is a security property of the collector, not a style
choice, and a MySQL dialect that built an IN list by string concatenation would
quietly drop it. Each dialect therefore owns its own `in_binds`, and
`selftest_dialect` asserts neither produces a literal.
"""

from __future__ import annotations

import logging
from typing import Sequence

import engines

log = logging.getLogger("collector.dialect")


class Dialect:
    """What `run.py` and `Session` need to know about one source engine."""

    engine = None
    # The parameter marker style, for anything that builds SQL by hand.
    paramstyle = None

    def connect(self, cfg):
        raise NotImplementedError

    def driver_error(self) -> type[BaseException]:
        """The exception class that means the driver refused, not that we did."""
        raise NotImplementedError

    def in_binds(self, prefix: str, values: Sequence[str]):
        raise NotImplementedError

    def prepare_sql(self, sql: str, binds) -> str:
        """Last chance to adjust SQL for this driver's quirks. Default: none."""
        return sql

    # -- the queries run.py issues directly -------------------------------
    def discover_schemas_sql(self) -> str:
        raise NotImplementedError

    def verify_schemas_sql(self, fragment: str) -> str:
        raise NotImplementedError

    def identity_sql(self) -> str:
        raise NotImplementedError

    def schema_column(self) -> str:
        """Column name the two schema queries return."""
        return "username"


class OracleDialect(Dialect):
    engine = engines.ORACLE
    paramstyle = "named"

    def connect(self, cfg):
        import oracledb

        # CLOBs arrive as str rather than LOB handles; keeps probe code free of
        # read() calls. Set on the module default, so it applies to every
        # connection this process opens.
        oracledb.defaults.fetch_lobs = False
        conn = oracledb.connect(user=cfg.user, password=cfg.password, dsn=cfg.dsn)
        log.info("connected engine=ORACLE user=%s dsn=%s thin=%s",
                 cfg.user, cfg.dsn, conn.thin)
        return conn

    def driver_error(self):
        import oracledb

        return oracledb.Error

    def in_binds(self, prefix: str, values: Sequence[str]):
        """Bind an IN list. Schema names never reach the SQL text as literals."""
        names = [f"{prefix}{i}" for i in range(len(values))]
        fragment = ", ".join(f":{n}" for n in names)
        return fragment, dict(zip(names, values))

    def discover_schemas_sql(self) -> str:
        # Oracle flags its own schemas with ORACLE_MAINTAINED='Y'. Anything 'N'
        # that is not configured gets reported rather than silently included or
        # dropped -- schema drift should be visible, not inferred.
        return ("SELECT username FROM dba_users "
                "WHERE oracle_maintained = 'N' ORDER BY username")

    def verify_schemas_sql(self, fragment: str) -> str:
        return (f"SELECT username FROM dba_users WHERE username IN ({fragment}) "
                "ORDER BY username")

    def identity_sql(self) -> str:
        return """SELECT sys_context('USERENV','CON_NAME') AS con_name,
                         sys_context('USERENV','DB_NAME')  AS db_name
                  FROM dual"""


class MySQLDialect(Dialect):
    engine = engines.MYSQL
    paramstyle = "format"

    def connect(self, cfg):
        import pymysql

        host, port, database = parse_mysql_dsn(cfg.dsn)
        conn = pymysql.connect(
            host=host,
            port=port,
            user=cfg.user,
            password=cfg.password,
            # Connecting without a default database is deliberate when the DSN
            # names none: the collector reads information_schema across several
            # schemas, and a default database would not change what it can see.
            database=database or None,
            charset="utf8mb4",
            # Read-only discipline, asserted on the connection rather than
            # trusted from the grant -- the same belt-and-braces the console's
            # custom-probe validator uses.
            autocommit=True,
            # A cursor returning dicts would duplicate Session.fetch's own
            # zip(columns, row); keep the default tuple cursor so one code path
            # builds the dicts.
        )
        log.info("connected engine=MYSQL user=%s host=%s port=%d database=%s",
                 cfg.user, host, port, database or "(none)")
        return conn

    def driver_error(self):
        import pymysql

        return pymysql.Error

    def in_binds(self, prefix: str, values: Sequence[str]):
        """Positional binds, for the same reason Oracle uses named ones.

        pymysql's paramstyle is `format`, so the fragment is `%s, %s` and the
        binds are a tuple. `prefix` is accepted and ignored so probe code reads
        identically across engines -- the alternative is every probe knowing
        which engine it is running against, which is the thing this module
        exists to prevent.
        """
        fragment = ", ".join(["%s"] * len(values))
        return fragment, tuple(values)

    def prepare_sql(self, sql: str, binds) -> str:
        """Escape literal `%` so pymysql's `query % args` leaves it alone.

        pymysql interpolates positional binds with the `%` operator, so any other
        `%` in the SQL is read as a format specifier: `LIKE '%TEMPORARY%'` dies
        with "unsupported format character 'T'", and `DATE_FORMAT(d,'%Y%m%d')`
        would too. The fix is `%%`, but only in a query that actually carries
        binds -- pymysql does not interpolate when args is None, and a doubled
        `%%` would then reach the server literally and match the wrong rows.

        Doing it here rather than in each probe is deliberate. A probe author
        writing `LIKE '%unsigned%'` is writing correct SQL; making that wrong
        depending on whether the same query happens to bind a schema name is a
        rule nobody will remember, and the failure is a ValueError from inside
        the driver rather than anything that names the real problem.

        `%s` is left alone because that IS the bind marker. Anything already
        written `%%` is left alone too, so this is safe to apply twice.
        """
        if not binds:
            return sql
        out = []
        i = 0
        n = len(sql)
        while i < n:
            ch = sql[i]
            if ch == "%" and i + 1 < n:
                nxt = sql[i + 1]
                if nxt in ("s", "%"):
                    # A bind marker, or an escape that is already correct.
                    out.append(sql[i:i + 2])
                    i += 2
                    continue
                out.append("%%")
                i += 1
                continue
            if ch == "%":
                out.append("%%")
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def discover_schemas_sql(self) -> str:
        # MySQL has no ORACLE_MAINTAINED flag. The equivalent is "not one of the
        # four schemas the server owns" -- information_schema, performance_schema,
        # mysql and sys. Listed explicitly rather than pattern-matched, because a
        # user schema legitimately called `mysql_reports` should not be excluded.
        return ("SELECT schema_name AS username FROM information_schema.schemata "
                "WHERE schema_name NOT IN "
                "('information_schema','performance_schema','mysql','sys') "
                "ORDER BY schema_name")

    def verify_schemas_sql(self, fragment: str) -> str:
        return ("SELECT schema_name AS username FROM information_schema.schemata "
                f"WHERE schema_name IN ({fragment}) ORDER BY schema_name")

    def identity_sql(self) -> str:
        # con_name has no MySQL meaning -- there is no container. Reported as
        # NULL rather than faked, so a reader can tell "not applicable" from
        # "not collected". db_name is the default database, which may be NULL
        # when the DSN named none; that is a fact about the connection, and the
        # schema list is what actually decides what gets collected.
        return ("SELECT NULL AS con_name, DATABASE() AS db_name, "
                "VERSION() AS version")


def parse_mysql_dsn(dsn: str) -> tuple[str, int, str | None]:
    """Split `host:port/database` (or `host:port`, or `host`) into its parts.

    MySQL has no service name, so the third field is a default *database* and is
    optional. Refusing a malformed port rather than defaulting it: a DSN of
    `host:3306x/db` is a typo, and silently connecting to 3306 would make the
    typo invisible.
    """
    text = str(dsn or "").strip()
    if not text:
        raise ValueError("empty MySQL DSN; expected host:port/database "
                         "(for example 10.0.1.42:3306/dbmig_mysql_app)")
    rest, sep, database = text.partition("/")
    host, sep_port, port_text = rest.partition(":")
    if not host:
        raise ValueError(f"DSN {dsn!r} names no host; expected host:port/database")
    if sep_port:
        if not port_text.isdigit():
            raise ValueError(f"DSN {dsn!r} has a non-numeric port {port_text!r}; "
                             "expected host:port/database")
        port = int(port_text)
    else:
        port = 3306
    return host, port, (database or None) if sep else (None)


DIALECTS = {
    engines.ORACLE: OracleDialect,
    engines.MYSQL: MySQLDialect,
}


def for_engine(engine: str | None) -> Dialect:
    """The dialect for one source engine. Oracle when nothing was declared."""
    return DIALECTS[engines.normalize(engine)]()
