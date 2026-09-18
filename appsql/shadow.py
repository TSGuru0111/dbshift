"""A schema for the parse gate to resolve against, built and rolled back.

The parse gate asks whether the target accepts a converted statement. That
question has no answer on an empty database: every statement fails with
`relation "customer" does not exist`, which says nothing about the rewrite. The
first version of this phase reported all twelve conversions REJECTED for that
reason -- condemning the work for a fact about the database.

**The schema comes from Phase 4c, not from a second implementation here.** 4c
already generates the tables, keys and indexes the target will carry, with the
real type mapping -- `NUMBER(10)` is a `BIGINT`, not a `numeric`. Rebuilding
that would give the application SQL a different schema from the one it will
actually meet, so a statement could parse here and fail on the real target, or
the reverse. Reusing 4c's output is the only version that proves anything.

Everything happens inside one transaction that is rolled back, the same trick
Phase 4b's compile gate uses and for the same reason: PostgreSQL DDL is
transactional where Oracle's is not, so the target is left exactly as found.

**Three things this deliberately does not do.**

  1. **No data.** A parse resolves names and types; it does not read rows. A
     shadow with data would invite someone to believe the *result* gate had
     run, and it has not.
  2. **No constraints or indexes.** Only the tables. A foreign key cannot make
     a SELECT parse or fail to parse, and creating one needs its referenced
     table to exist first, which is ordering work for no gain.
  3. **It never persists.** If a caller wants a durable schema to parse
     against, that is applying 4c, which is a different decision with a named
     approver.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("appsql.shadow")

# The shadow lives in its own schema so a parse can never touch -- or collide
# with -- what an apply created. Distinct from `convert.target.SHADOW_PREFIX`
# because the two phases may hold shadows at the same time.
SCHEMA = "dbshift_appsql_shadow"

_CREATE_TABLE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:(\w+)\.)?(\w+)", re.IGNORECASE)


def tables_from_ddl(plan: dict) -> list[str]:
    """4c's CREATE TABLE statements, re-pointed at the shadow schema.

    Only the tables: see the module docstring on why constraints and indexes
    are left out.
    """
    out = []
    for stmt in plan.get("tables") or []:
        m = _CREATE_TABLE.match(stmt)
        if not m:
            log.warning("appsql.shadow: not a CREATE TABLE, skipped: %s", stmt[:60])
            continue
        owner, table = m.group(1), m.group(2)
        # Rewrite only the statement's own qualified name, so a column default
        # or a type reference elsewhere in the text is untouched.
        if owner:
            stmt = stmt.replace(f"{owner}.{table}", f"{SCHEMA}.{table}", 1)
        else:
            stmt = re.sub(r"(CREATE\s+TABLE\s+)", rf"\1{SCHEMA}.", stmt, count=1,
                          flags=re.IGNORECASE)
        out.append(stmt)
    return out


def sequences_from_ddl(plan: dict) -> list[str]:
    """The sequences a table default refers to.

    A `DEFAULT nextval('seq_customer_id')` fails on a sequence that does not
    exist, which is what made 24 of 30 statements fail the first time Phase 4c
    ran from the console. Created unqualified-into-the-shadow so the defaults
    resolve.
    """
    return [f"CREATE SEQUENCE IF NOT EXISTS {SCHEMA}.{s}"
            for s in (plan.get("sequences_needed") or [])]


def types_from_ddl(plan: dict) -> list[str]:
    """Placeholder composite types for columns typed as user-defined types.

    4b converts those properly; here they only need to exist so a CREATE TABLE
    naming one does not fail. A placeholder is honest because nothing about the
    type's shape affects whether a SELECT over the table parses.
    """
    return [f"CREATE TYPE {SCHEMA}.{t.lower()} AS (placeholder text)"
            for t in (plan.get("depends_on_types") or [])]


class Shadow:
    """An open transaction holding the shadow schema. Rolled back on exit.

    Used as a context manager, so the rollback happens on any path out --
    including an exception mid-parse:

        with Shadow(target, ddl_plan) as sh:
            ok, detail = sh.parses(sql)
    """

    def __init__(self, target, ddl_plan: dict):
        self.target = target
        self.plan = ddl_plan or {}
        self.conn = None
        self.cur = None
        self.built = 0
        self._n = 0                 # probe counter; see `parses`
        self.failures: list[str] = []
        self.error: str | None = None

    # `parse_check` prefers a `parses()` method, so a Shadow can be handed
    # straight to the gate in place of a bare target.
    def parses(self, sql: str) -> tuple[bool, str]:
        from . import gates
        if self.cur is None:
            return False, self.error or "the shadow schema was not built"
        probe = (sql or "").strip().rstrip(";")
        if not probe:
            return False, "nothing to parse"
        # A unique name per probe. PREPARE is **not** undone by ROLLBACK TO
        # SAVEPOINT -- prepared statements live at session scope -- so reusing
        # one name made every statement after the first fail with `prepared
        # statement "..." already exists`, which read as a conversion defect
        # and was an artefact of this gate.
        self._n += 1
        name = f"dbshift_appsql_probe_{self._n}"
        try:
            self.cur.execute("SAVEPOINT appsql_probe")
            self.cur.execute(f"PREPARE {name} AS {probe}")
            self.cur.execute(f"DEALLOCATE {name}")
            self.cur.execute("RELEASE SAVEPOINT appsql_probe")
            return True, (f"parsed and planned against {self.built} shadow table(s), "
                          "then rolled back")
        except Exception as exc:                                  # noqa: BLE001
            msg = gates._pg_message(exc)
            try:
                self.cur.execute("ROLLBACK TO SAVEPOINT appsql_probe")
            except Exception:                                     # noqa: BLE001
                pass
            return False, msg

    def __enter__(self) -> "Shadow":
        from . import gates
        try:
            self.conn = self.target.connect()
            self.cur = self.conn.cursor()
            self.cur.execute("BEGIN")
            self.cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            self.cur.execute(f"CREATE SCHEMA {SCHEMA}")
            # The search path is what lets an unqualified `FROM customer` in the
            # application's SQL resolve. Without it every statement would have
            # to be rewritten to name the shadow, which would be testing a
            # statement the application never sends.
            self.cur.execute(f"SET LOCAL search_path TO {SCHEMA}")
            for stmt in (sequences_from_ddl(self.plan) + types_from_ddl(self.plan)
                         + tables_from_ddl(self.plan)):
                try:
                    self.cur.execute("SAVEPOINT appsql_build")
                    self.cur.execute(stmt)
                    self.built += 1
                except Exception as exc:                          # noqa: BLE001
                    # One unbuildable table must not take the whole shadow with
                    # it: the statements that do not reference it can still be
                    # parsed. Recorded so the gate can say the shadow is partial.
                    self.failures.append(f"{stmt.splitlines()[0][:70]}: {gates._pg_message(exc)}")
                    try:
                        self.cur.execute("ROLLBACK TO SAVEPOINT appsql_build")
                    except Exception:                             # noqa: BLE001
                        pass
        except Exception as exc:                                  # noqa: BLE001
            self.error = f"could not build the shadow schema: {gates._pg_message(exc)}"
            self.cur = None
            log.warning("appsql.shadow: %s", self.error)
        return self

    def __exit__(self, *exc_info) -> bool:
        # Rolled back on every path, so nothing here ever persists.
        for step in ("ROLLBACK",):
            try:
                if self.cur is not None:
                    self.cur.execute(step)
            except Exception:                                     # noqa: BLE001
                pass
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:                                         # noqa: BLE001
            pass
        return False

    def describe(self) -> dict:
        return {
            "schema": SCHEMA,
            "tables_built": self.built,
            "build_failures": self.failures,
            "error": self.error,
            "rolled_back": True,
        }
