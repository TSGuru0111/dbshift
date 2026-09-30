from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Sequence

from . import dialect as dialect_mod

log = logging.getLogger("collector.db")

# The engine-specific parts moved to `dialect.py` on 2026-09-28, when MySQL
# became a second source. What stayed here is everything that never depended on
# the driver: `Session.fetch` needs only `cursor.execute`, `cursor.description`
# and `fetchall()`, which PEP 249 guarantees, so one implementation serves both
# engines and the 47 datasets keep their shape.
#
# `in_binds` is re-exported below because ten probe modules import it from here
# and their SQL is Oracle's regardless. It now delegates to a dialect so the
# MySQL probes can import the same name and get `%s` markers.


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


# The dialect used when a caller does not name one. Oracle, so that every
# existing probe, script and docs example behaves exactly as it did before the
# source-engine flag existed.
_DEFAULT_DIALECT = dialect_mod.OracleDialect()


def in_binds(prefix: str, values: Sequence[str], dialect=None) -> tuple[str, object]:
    """Bind an IN list. Schema names never reach the SQL text as literals.

    `dialect` defaults to Oracle, which is what the ten Oracle probes want and
    is why they need no change. The MySQL probes pass their own, or call the
    dialect directly -- either way the binding is the driver's job, never a
    format string.
    """
    return (dialect or _DEFAULT_DIALECT).in_binds(prefix, values)


@dataclass
class QueryRecord:
    label: str
    sql: str
    sql_sha256: str
    row_count: int
    elapsed_ms: int
    error: str | None = None


@dataclass
class Session:
    connection: object
    query_log: list[QueryRecord] = field(default_factory=list)
    # Column names per query label, captured from cur.description.
    #
    # A query that returns no rows still knows its own shape, and that shape is
    # the only record of it: the JSON dataset would otherwise be an empty list,
    # and the assessment loader -- which infers SQLite columns from the rows --
    # would build a table with no columns. Every rule referencing one then dies
    # with "no such column", which is a property of the estate having no jobs or
    # queues, not of the rule being wrong.
    columns_by_label: dict[str, list[str]] = field(default_factory=dict)
    # Which engine is on the other end. Carried so `fetch` can catch the right
    # driver exception and so a probe can ask without importing a driver.
    # Defaults to Oracle for the reason in the module comment above.
    dialect: object = field(default_factory=lambda: _DEFAULT_DIALECT)

    def fetch(self, label: str, sql: str, binds=None) -> list[dict]:
        # **Pass None, not {}, when there are no binds.**
        #
        # pymysql interpolates with `query % args` whenever `args is not None`,
        # so an empty dict still triggers it -- and then every literal `%` in the
        # SQL is read as a format specifier. `LIKE '%TEMPORARY%'` raises
        # "unsupported format character 'T'", and `DATE_FORMAT(x,'%Y%m%d')` would
        # too. Handing None through means a query with no binds is sent verbatim,
        # which is both correct and what oracledb already did with {}.
        #
        # Found on 2026-09-28 by running the probes against a real MySQL 8.0.46:
        # the offline selftest could not catch it, because a fake cursor never
        # performs the interpolation. Escaping every LIKE to `%%` was the
        # alternative and it is worse -- it would be correct only in the queries
        # that happen to carry binds, so the same SQL string would need different
        # escaping depending on its caller.
        binds = binds if binds else None
        # The SQL recorded in the query log is what the probe wrote, not what the
        # driver received: a log full of `%%` would be a worse record of intent,
        # and the sha256 is meant to identify the query a human reviewed.
        flat = normalize_sql(sql)
        sql = self.dialect.prepare_sql(sql, binds)
        started = time.perf_counter()
        driver_error = self.dialect.driver_error()
        try:
            cur = self.connection.cursor()
            try:
                cur.execute(sql, binds)
                columns = [d[0].lower() for d in cur.description]
                self.columns_by_label[label] = columns
                rows = [dict(zip(columns, r)) for r in cur.fetchall()]
            finally:
                # Not every driver's cursor is a context manager (oracledb's is,
                # pymysql's is too, but the guarantee is not in PEP 249), so
                # close it explicitly rather than rely on `with`.
                cur.close()
        except driver_error as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            message = str(exc).splitlines()[0]
            self.query_log.append(QueryRecord(label, flat, sha256_text(flat), 0, elapsed, message))
            log.warning("query=%s FAILED elapsed_ms=%d error=%s", label, elapsed, message)
            return []
        elapsed = int((time.perf_counter() - started) * 1000)
        self.query_log.append(QueryRecord(label, flat, sha256_text(flat), len(rows), elapsed))
        log.info("query=%s rows=%d elapsed_ms=%d", label, len(rows), elapsed)
        return rows

    def labels_since(self, mark: int) -> list[str]:
        return [q.label for q in self.query_log[mark:]]

    def binds(self, prefix: str, values: Sequence[str]):
        """This session's own IN-list binder, in its own dialect."""
        return self.dialect.in_binds(prefix, values)


def connect(cfg):
    """Open the source connection for whichever engine `cfg` names.

    Oracle uses thin mode: no Oracle Instant Client on the machine, by design.
    """
    dialect = dialect_mod.for_engine(getattr(cfg, "source_engine", None))
    return dialect.connect(cfg)
