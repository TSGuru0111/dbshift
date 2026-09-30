"""Self-test for the MySQL probe set, with dataset parity as the point.

Runs **offline against a fake session**. There is no MySQL server here, and that
is deliberate: what this module needs to prove is a property of the SQL and the
dataset names, not of any particular estate. A fake `Session` records every query
and returns no rows, which is enough to check that

  * every probe emits the dataset names the rest of the pipeline reads,
  * schema names are bound rather than interpolated,
  * no probe writes,

and none of those need a database to be true or false.

**Why parity is the headline.** `assess/loader.py` creates one SQLite table per
dataset and the 51 rules are SQL over those tables and the `v_user_*` views built
on them. A MySQL probe that renamed `tables` to `mysql_tables` would not fail
here -- it would fail inside a rule, three phases later, as "no such column",
looking like a broken rule rather than a missing dataset. So the parity check is
mechanical and complete rather than a spot check.

    python -m collector.selftest_probes_mysql
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import engines
from collector import dialect as dialect_mod
from collector.probes import PROBES as ORACLE_PROBES
from collector.probes_mysql import PROBES as MYSQL_PROBES

ROOT = Path(__file__).resolve().parent.parent

# Datasets Oracle emits that MySQL has no analogue for at all. Each must still
# be emitted shaped-and-empty, and each needs a reason here -- the list is a
# record of decisions, not a list of gaps to be tidied away later.
NO_MYSQL_ANALOGUE = {
    "tablespaces": "InnoDB has per-table files; there is no tablespace a migration sizes or moves",
    "awr_coverage": "AWR is an Oracle licensed feature; MySQL has performance_schema instead",
    "profiles": "no Oracle-style resource/password profiles",
    "synonyms": "MySQL has no synonyms; a view is the nearest thing",
    "db_links": "no database links; federation is a separate engine (FEDERATED) and out of scope",
    "queues": "no Advanced Queuing",
    "materialized_views": "MySQL has no materialized views",
    "mview_logs": "follows from materialized_views",
    "directories": "no server-side DIRECTORY objects",
    "external_tables": "no external tables; LOAD DATA is a statement, not an object",
    "text_indexes": "FULLTEXT is an index type, reported in `indexes`, not a separate catalogue",
    "xml_schemas": "no registered XML schemas",
    "types": "no user-defined object types",
    "lobs": "LOB storage is not separately catalogued; TEXT/BLOB appear in `columns`",
    "database_options": "no installed-options catalogue",
    "nls_parameters": "character set and collation are per column, in `columns` and `mysql_charsets`",
    "os_statistics": "no v$osstat equivalent this collector reads",
    "system_metrics": "no v$sysmetric equivalent this collector reads",
    "roles": "roles exist only from 8.0 and are accounts; grants are in role_privileges",
    "index_expressions": "functional indexes are implemented as hidden generated columns",
    "plsql_errors": "MySQL refuses to create a routine that does not parse",
    "invalid_objects": "follows from plsql_errors: no standing population of broken objects",
}


class _FakeCursor:
    def __init__(self, log, dialect):
        self._log = log
        self._dialect = dialect
        self.description = None

    def execute(self, sql, binds=None):
        self._log.append((sql, binds))
        # Enough shape for Session.fetch to build zero rows without guessing.
        self.description = []

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, log, dialect):
        self._log = log
        self._dialect = dialect

    def cursor(self):
        return _FakeCursor(self._log, self._dialect)


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _datasets(probes, dialect):
    """Run every probe against a fake session; return dataset names and the SQL."""
    from collector.db import Session

    log: list = []
    names: set[str] = set()
    session = Session(connection=_FakeConnection(log, dialect), dialect=dialect)
    owners = ("dbmig_mysql_app", "dbmig_rpt")
    for probe in probes:
        out = probe.collect(session, owners)
        for key in out:
            assert key.startswith("source_inventory."), key
            names.add(key.split(".", 1)[1])
    return names, log


def main(argv=None) -> int:
    c = _Check()

    mysql_dialect = dialect_mod.for_engine(engines.MYSQL)
    oracle_dialect = dialect_mod.for_engine(engines.ORACLE)

    print("probes load and run against a fake session")
    my_names, my_log = _datasets(MYSQL_PROBES, mysql_dialect)
    c("every MySQL probe ran without raising", True)
    c("the MySQL probes issued queries", len(my_log) > 0, str(len(my_log)))
    c("every MySQL probe exports a NAME", all(getattr(p, "NAME", None) for p in MYSQL_PROBES))
    c("probe NAMEs are unique", len({p.NAME for p in MYSQL_PROBES}) == len(MYSQL_PROBES))
    c("MySQL probe NAMEs are all names the Oracle set also uses, so the console's "
      "toggles and descriptions work on either engine",
      {p.NAME for p in MYSQL_PROBES} <= {p.NAME for p in ORACLE_PROBES},
      str(sorted({p.NAME for p in MYSQL_PROBES} - {p.NAME for p in ORACLE_PROBES})))

    or_names, _ = _datasets(ORACLE_PROBES, oracle_dialect)

    print()
    print("dataset parity -- the contract assess/ and every later phase reads")
    missing = sorted(or_names - my_names)
    c("MySQL emits every dataset the Oracle probes do", not missing,
      f"missing: {missing}")
    for ds in sorted(NO_MYSQL_ANALOGUE):
        c(f"{ds}: has no MySQL analogue but is still emitted (shaped, empty)",
          ds in my_names, "absent -- a rule reading it would die with 'no such column'")
    extra = sorted(my_names - or_names)
    c("every MySQL-only dataset is namespaced `mysql_`, so it cannot collide with "
      "an Oracle dataset name",
      all(e.startswith("mysql_") for e in extra), str(extra))
    c("the MySQL-only datasets are the ones this path is for",
      {"mysql_table_storage", "mysql_charsets", "mysql_binlog",
       "mysql_tables_without_pk"} <= my_names,
      str(extra))

    print()
    print("rules and views: nothing they read is missing")
    rules = json.loads((ROOT / "assess" / "rules.json").read_text(encoding="utf-8"))
    referenced = set()
    for r in rules:
        for m in re.finditer(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", r["sql"], re.I):
            referenced.add(m.group(1).lower())
    # The v_user_* views are built by the loader over these three.
    view_sources = {"tables", "objects", "columns"}
    # Aliases and SQL keywords the crude regex picks up; not datasets.
    not_datasets = {"a", "this", "directory", "x", "v_user_tables", "v_user_objects",
                    "v_user_columns", "dual", "select"}
    needed = (referenced - not_datasets) | view_sources
    unmet = sorted(n for n in needed if n not in my_names)
    c("every table the Oracle rule catalogue queries exists on the MySQL path too, "
      "so a shared rule cannot fail with 'no such table'",
      not unmet, f"unmet: {unmet}")
    c("the three datasets the v_user_* views are built from are present",
      view_sources <= my_names)

    print()
    print("schema names are bound, never interpolated")
    for sql, binds in my_log:
        for owner in ("dbmig_mysql_app", "dbmig_rpt"):
            if owner in sql:
                c(f"no owner literal in SQL: {sql[:56]}...", False,
                  "a schema name reached the SQL text; bind it instead")
                break
    c("no probe put a schema name into SQL text",
      not any(o in sql for sql, _ in my_log for o in ("dbmig_mysql_app", "dbmig_rpt")))
    bound = [b for _s, b in my_log if b]
    c("the queries that filter by schema passed binds", len(bound) > 0, str(len(bound)))
    c("MySQL binds are positional tuples, matching pymysql's paramstyle",
      all(isinstance(b, tuple) for b in bound),
      str({type(b).__name__ for b in bound}))

    print()
    print("bind-marker arithmetic -- the UNION trap")
    frag, binds = mysql_dialect.in_binds("o", ("a", "b", "c"))
    c("an IN list of three produces three markers", frag == "%s, %s, %s", frag)
    c("...and three binds", binds == ("a", "b", "c"), str(binds))
    c("a marker count mismatch is what a UNION probe gets wrong, so every probe's "
      "marker count equals its bind count",
      all(sql.count("%s") == len(b or ()) for sql, b in my_log),
      str([(s[:40], s.count("%s"), len(b or ())) for s, b in my_log
           if s.count("%s") != len(b or ())][:3]))

    print()
    print("read-only: no probe writes")
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE ", "ALTER ",
                 "TRUNCATE ", "GRANT ", "REVOKE ", "REPLACE ")
    offenders = [sql[:60] for sql, _ in my_log
                 if any(w in sql.upper() for w in forbidden)]
    c("every MySQL probe query is read-only", not offenders, str(offenders[:2]))

    print()
    print("identity: the CDC columns mode.readiness() reads are produced")
    from collector.probes_mysql import identity as ident
    # Drive the mapping directly: a fake session returns no rows, so the
    # Oracle-shaped derivation is exercised here with values instead.
    class _OneRow(Session_stub := object):
        pass

    for binlog, fmt, image, expect_log, expect_supp in (
        ("1", "ROW", "FULL", "ARCHIVELOG", "YES"),
        ("1", "STATEMENT", "FULL", "ARCHIVELOG", "NO"),
        ("1", "ROW", "MINIMAL", "ARCHIVELOG", "NO"),
        ("0", "ROW", "FULL", "NOARCHIVELOG", "NO"),
    ):
        row = {"log_bin": binlog, "binlog_format": fmt, "binlog_row_image": image}

        class _S:
            dialect = mysql_dialect

            def fetch(self, label, sql, binds=None):
                return [dict(row)]

            def binds(self, p, v):
                return mysql_dialect.in_binds(p, v)

        out = ident.collect(_S(), ("s",))["source_inventory.database"][0]
        c(f"log_bin={binlog} format={fmt} image={image} -> log_mode={expect_log}, "
          f"supplemental={expect_supp}",
          out["log_mode"] == expect_log and out["supplemental_log_data_min"] == expect_supp,
          f"got {out['log_mode']}/{out['supplemental_log_data_min']}")

    from collector import mode as migration_mode
    ready = migration_mode.readiness("ARCHIVELOG", "YES")
    c("a ROW+FULL MySQL source reads as CDC-ready through the existing "
      "mode.readiness(), with no MySQL-specific branch", ready["ready"])
    not_ready = migration_mode.readiness("NOARCHIVELOG", "NO")
    c("a source with the binary log off reads as not CDC-ready, and says why",
      not not_ready["ready"] and len(not_ready["unmet"]) == 2, str(not_ready["unmet"]))

    print()
    print("container columns are NULL, not faked")
    class _S2:
        dialect = mysql_dialect

        def fetch(self, label, sql, binds=None):
            return [{"log_bin": "1", "binlog_format": "ROW", "binlog_row_image": "FULL"}]

        def binds(self, p, v):
            return mysql_dialect.in_binds(p, v)

    row = ident.collect(_S2(), ("s",))["source_inventory.database"][0]
    c("cdb is NULL on MySQL rather than 'NO' -- there is no container question "
      "to answer, and 'NO' would assert one", row["cdb"] is None)
    c("con_name is NULL for the same reason", row["con_name"] is None)

    print()
    print("identifier guard before interpolation")
    from collector.probes_mysql import dataprofile as dp
    c("a well-formed identifier is backtick-quoted", dp._quote("ORDERS") == "`ORDERS`")
    for bad in ("a`b", "a b", "a;drop", "a'b", "a-b"):
        refused = False
        try:
            dp._quote(bad)
        except ValueError:
            refused = True
        c(f"refuses to interpolate {bad!r}", refused)

    print()
    print("literal %% escaping -- the bug a fake cursor cannot catch")
    # Found 2026-09-28 against MySQL 8.0.46. pymysql interpolates with
    # `query % args`, so a literal % in the SQL is read as a format specifier and
    # `LIKE '%TEMPORARY%'` dies with "unsupported format character 'T'". The
    # offline fake cursor never interpolates, so only a live server showed it.
    for sql, binds, expect in (
        ("LIKE '%TEMPORARY%' AND s IN (%s)", ("a",),
         "LIKE '%%TEMPORARY%%' AND s IN (%s)"),
        ("DATE_FORMAT(d,'%Y%m%d') AND s=%s", ("a",),
         "DATE_FORMAT(d,'%%Y%%m%%d') AND s=%s"),
        # No binds: pymysql does not interpolate, so doubling would reach the
        # server literally and match the wrong rows.
        ("LIKE '%unsigned%'", None, "LIKE '%unsigned%'"),
        # Idempotent: an already-escaped query survives a second pass.
        ("LIKE '%%done%%' AND s=%s", ("a",), "LIKE '%%done%%' AND s=%s"),
    ):
        got = mysql_dialect.prepare_sql(sql, binds)
        c(f"prepare_sql({sql[:30]!r}, binds={bool(binds)})", got == expect, got)
    c("the bind marker %s is never escaped, because it IS the marker",
      mysql_dialect.prepare_sql("a=%s", ("x",)).count("%s") == 1)
    c("Oracle's dialect does not rewrite SQL at all -- named binds need none",
      oracle_dialect.prepare_sql("LIKE '%X%' AND a=:o0", {"o0": 1})
      == "LIKE '%X%' AND a=:o0")
    # Every real probe query must survive the round trip with its markers intact.
    for sql, binds in my_log:
        prepared = mysql_dialect.prepare_sql(sql, binds)
        if prepared.count("%s") != len(binds or ()):
            c(f"marker count preserved: {sql[:40]}", False,
              f"{prepared.count('%s')} markers vs {len(binds or ())} binds")
            break
    else:
        c("every probe query keeps exactly its bind markers after escaping", True)

    print()
    print("empty binds are passed as None, not {}")
    # `{}` is not None, so pymysql would interpolate and the same % bug returns.
    class _Recorder:
        def __init__(self):
            self.seen = []

        def cursor(self):
            rec = self

            class _C:
                description = []

                def execute(self, sql, binds=None):
                    rec.seen.append(binds)

                def fetchall(self):
                    return []

                def close(self):
                    pass

            return _C()

    from collector.db import Session as _S
    r = _Recorder()
    sess = _S(connection=r, dialect=mysql_dialect)
    sess.fetch("no.binds", "SELECT 1")
    sess.fetch("empty.dict", "SELECT 1", {})
    sess.fetch("empty.tuple", "SELECT 1", ())
    sess.fetch("real.binds", "SELECT %s", ("x",))
    c("a query with no binds passes None to the driver", r.seen[0] is None)
    c("an empty dict is normalised to None, so pymysql does not interpolate",
      r.seen[1] is None, repr(r.seen[1]))
    c("an empty tuple is normalised to None too", r.seen[2] is None, repr(r.seen[2]))
    c("real binds are passed through unchanged", r.seen[3] == ("x",))

    print()
    print("privileges the collector cannot do without (measured on 8.0.46)")
    # Recorded as assertions so the grant script and the probes cannot drift:
    # without these, information_schema returns FEWER ROWS rather than an error,
    # which is indistinguishable from a small estate.
    from engines import spec as _spec
    privs = " ".join(_spec.spec("mysql")["collector_privileges"]).upper()
    for needed in ("SELECT", "SHOW VIEW", "PROCESS", "REPLICATION CLIENT"):
        c(f"{needed} is named in the engine spec's collector_privileges",
          needed in privs, privs)
    admin = (ROOT / "scripts" / "mysql-source" / "01_setup_admin.sql")
    if admin.exists():
        sql_text = admin.read_text(encoding="utf-8").upper()
        for needed in ("EXECUTE", "TRIGGER", "EVENT", "SHOW_ROUTINE"):
            c(f"01_setup_admin.sql grants {needed} -- without it "
              f"information_schema returns zero rows, with no error",
              needed in sql_text)

    print()
    print("COLUMN parity, not just dataset names -- the gap found 2026-09-29")
    # A diff of a real Oracle run against a real MySQL run found 15 datasets whose
    # MySQL rows carried fewer columns. The name-parity checks above passed all of
    # them. conform() now pads every dataset to Oracle's column set.
    from collector.probes_mysql import conform, oracle_columns
    ref = oracle_columns()
    c("the Oracle column reference is loaded", len(ref) > 40, str(len(ref)))
    missing_ref = sorted(n for n in or_names if n not in ref and n not in NO_MYSQL_ANALOGUE)
    c("every dataset the Oracle probes emit has a column reference",
      not missing_ref, str(missing_ref[:5]))
    produced = {"source_inventory.feature_usage": [{"name": "x", "detected_usages": 1,
                                                    "mysql_only": "kept"}]}
    conform(produced)
    row = produced["source_inventory.feature_usage"][0]
    c("conform() adds Oracle's columns as NULL -- last_usage_date broke Phase 3",
      "last_usage_date" in row and row["last_usage_date"] is None)
    c("conform() keeps MySQL-only columns: it adds, never removes",
      row.get("mysql_only") == "kept")
    c("conform() never overwrites a value the probe set", row["name"] == "x")
    c("conform() leaves an empty dataset empty rather than inventing a row",
      conform({"source_inventory.queues": []})["source_inventory.queues"] == [])

    print()
    print("plsql_source is ONE ROW PER OBJECT, as Oracle's is")
    # The first version emitted one row per LINE under different column names.
    # convert/inventory.py reads object_type / object_name / source_text, so
    # Phase 4b would have seen an estate with no stored code at all.
    from collector.probes_mysql import routines as rt
    c("the routines probe declares EXTERNALIZE, like the Oracle plsql probe",
      getattr(rt, "EXTERNALIZE", None) == [("source_inventory.plsql_source",
                                            "source_text", "source_sha256", 800)])

    class _ShowSession:
        """Answers SHOW CREATE with a full statement; records what it was asked."""
        dialect = mysql_dialect
        asked: list = []

        def fetch(self, label, sql, binds=None):
            self.asked.append(sql)
            if sql.startswith("SHOW CREATE FUNCTION"):
                return [{"create function": "CREATE FUNCTION `f`(p INT) RETURNS INT BEGIN RETURN p; END"}]
            if sql.startswith("SHOW CREATE TRIGGER"):
                return [{"sql original statement": "CREATE TRIGGER `t` BEFORE INSERT ON x FOR EACH ROW SET NEW.a = 1"}]
            return []

    sess = _ShowSession()
    rows = rt._source_rows(
        sess,
        [{"owner": "app", "name": "f", "type": "FUNCTION", "body": "BEGIN RETURN p; END"}],
        [{"owner": "app", "trigger_name": "t", "trigger_body": "SET NEW.a = 1"}])
    c("one row per object -- a function and a trigger give two rows", len(rows) == 2, str(len(rows)))
    for col in ("owner", "object_type", "object_name", "source_text", "source_sha256",
                "line_count", "char_length"):
        c(f"each row carries Oracle's {col}", all(col in r for r in rows))
    f = [r for r in rows if r["object_type"] == "FUNCTION"][0]
    c("the text is the FULL statement with its signature, not the bare body -- a "
      "body without its parameter list cannot be converted or compiled",
      f["source_text"].startswith("CREATE FUNCTION") and "(p INT)" in f["source_text"])
    c("and the row says where the text came from", f["source_origin"] == "show_create")
    c("triggers are included, as Oracle's dataset includes them",
      any(r["object_type"] == "TRIGGER" for r in rows))
    c("identifiers are backtick-quoted in SHOW CREATE",
      any("`app`.`f`" in q for q in sess.asked))
    fb = rt._source_rows(type("S", (), {"dialect": mysql_dialect,
                                        "fetch": lambda self, *a, **k: []})(),
                         [{"owner": "app", "name": "g", "type": "PROCEDURE", "body": "BEGIN END"}], [])
    c("without SHOW_ROUTINE the body is used, and the row says so",
      fb[0]["source_text"] == "BEGIN END" and fb[0]["source_origin"] == "routine_definition")
    c("an unsafe identifier is never interpolated into SHOW CREATE",
      rt._show_create(sess, "app", "x`; DROP", "FUNCTION") is None)

    print()
    print("generated columns are VIRTUAL/STORED GENERATED, not DEFAULT_GENERATED")
    # `extra LIKE '%GENERATED%'` also matched DEFAULT_GENERATED -- every
    # DEFAULT CURRENT_TIMESTAMP column -- and Phase 4c dropped those as computed.
    import inspect
    from collector.probes_mysql import tables as tb
    tsrc = inspect.getsource(tb)
    c("the probe no longer tests a bare '%GENERATED%'", "extra LIKE '%GENERATED%'" not in tsrc)
    c("it names VIRTUAL and STORED GENERATED explicitly",
      "%VIRTUAL GENERATED%" in tsrc and "%STORED GENERATED%" in tsrc)
    c("and carries the generation expression for Phase 4c's note", "generation_expression" in tsrc)

    print()
    print("MySQL DSN parsing")
    for dsn, expect in (
        ("10.0.1.42:3306/appdb", ("10.0.1.42", 3306, "appdb")),
        ("db.internal:3307/x", ("db.internal", 3307, "x")),
        ("localhost:3306", ("localhost", 3306, None)),
        ("localhost", ("localhost", 3306, None)),
        ("localhost/appdb", ("localhost", 3306, "appdb")),
    ):
        got = dialect_mod.parse_mysql_dsn(dsn)
        c(f"{dsn!r} -> {expect}", got == expect, str(got))
    for bad in ("", "   ", ":3306/db", "host:33o6/db"):
        refused = False
        try:
            dialect_mod.parse_mysql_dsn(bad)
        except ValueError:
            refused = True
        c(f"refuses malformed DSN {bad!r} rather than defaulting the port", refused)

    print()
    print(f"{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
