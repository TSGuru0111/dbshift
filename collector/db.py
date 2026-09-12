from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import oracledb

log = logging.getLogger("collector.db")

# CLOBs arrive as str rather than LOB handles; keeps probe code free of read() calls.
oracledb.defaults.fetch_lobs = False


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


def in_binds(prefix: str, values: Sequence[str]) -> tuple[str, dict[str, str]]:
    """Bind an IN list. Schema names never reach the SQL text as literals."""
    names = [f"{prefix}{i}" for i in range(len(values))]
    fragment = ", ".join(f":{n}" for n in names)
    return fragment, dict(zip(names, values))


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
    connection: Any
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

    def fetch(self, label: str, sql: str, binds: dict | None = None) -> list[dict]:
        binds = binds or {}
        flat = normalize_sql(sql)
        started = time.perf_counter()
        try:
            with self.connection.cursor() as cur:
                cur.execute(sql, binds)
                columns = [d[0].lower() for d in cur.description]
                self.columns_by_label[label] = columns
                rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        except oracledb.Error as exc:
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


def connect(cfg) -> Any:
    """Thin mode: no Oracle Instant Client on the machine, by design."""
    conn = oracledb.connect(user=cfg.user, password=cfg.password, dsn=cfg.dsn)
    log.info("connected user=%s dsn=%s thin=%s", cfg.user, cfg.dsn, conn.thin)
    return conn
