"""Generate the PostgreSQL schema DDL for an estate, and optionally prove it runs.

    python -m convert.ddl_run                       # write the plan, print a summary
    python -m convert.ddl_run --compile             # also run it on PostgreSQL, then ROLL BACK
    python -m convert.ddl_run --run <id> --owner DBMIG_TELCO

Nothing is ever applied. `--compile` creates everything inside one transaction
and rolls it back, the same way Phase 4b proves converted code compiles: the
only way to know a foreign key resolves or a check constraint parses is to run
it, and reading the SQL would pass statements that fail.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "convert"

from provision import records as prov_records

from . import ddl

OUTPUT = Path(__file__).resolve().parent / "output"
PLAN = OUTPUT / "schema_ddl.json"

DATASETS = ("columns", "tables", "objects", "external_tables", "constraints",
            "constraint_columns", "indexes", "index_columns", "sequences", "queues")


def _latest_run() -> str:
    root = Path(__file__).resolve().parent.parent / "collector" / "output"
    runs = [d for d in root.iterdir() if d.is_dir() and (d / "manifest.json").exists()]
    if not runs:
        raise SystemExit("no collector runs found")
    runs.sort(key=lambda d: json.loads(
        (d / "manifest.json").read_text(encoding="utf-8"))["started_at_utc"])
    return runs[-1].name


def build(run_id: str, owner: str) -> dict:
    datasets = {n: prov_records._dataset(run_id, n) for n in DATASETS}
    plan = ddl.build(owner=owner, datasets=datasets)
    plan["collector_run_id"] = run_id
    plan["estate"] = owner
    plan["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    return plan



def _pg_message(exc: Exception) -> str:
    """The engine's own sentence, out of pg8000's wire dict.

    pg8000 raises with the raw PostgreSQL error fields -- {'S': 'ERROR', 'C':
    '42P01', 'M': 'relation "x" does not exist', ...}. `str(exc)` on that is a
    Python dict repr, which reached the console screen verbatim and read like a
    crash in the tool rather than a statement the target refused.
    """
    args = getattr(exc, "args", None)
    fields = args[0] if args and isinstance(args[0], dict) else None
    if not fields:
        return str(exc).splitlines()[0][:200]
    msg = fields.get("M") or ""
    code = fields.get("C") or ""
    out = msg + (f" [{code}]" if code else "")
    for extra in (fields.get("D"), fields.get("H")):
        if extra:
            out += f" -- {extra}"
    return out[:300]

def compile_check(plan: dict, dsn: str, user: str, password: str) -> dict:
    """Run every statement inside one transaction, then roll it back."""
    import pg8000.dbapi
    host, rest = dsn.split(":", 1)
    port, database = rest.split("/", 1)
    conn = pg8000.dbapi.connect(host=host, port=int(port), database=database,
                                user=user, password=password)
    cur = conn.cursor()
    owner = plan["estate"].lower()
    ran, failures = 0, []
    try:
        cur.execute("BEGIN")
        cur.execute(f"DROP SCHEMA IF EXISTS {owner} CASCADE")
        cur.execute(plan["schema"])
        # Sequences and user-defined types are other phases' output, created
        # here as scaffolding so the dependency is exercised rather than
        # sidestepped. They are rolled back with everything else.
        for s in plan.get("sequences_needed", []):
            cur.execute(f"CREATE SEQUENCE IF NOT EXISTS {owner}.{s}")
        for t in plan.get("depends_on_types", []):
            cur.execute(f"CREATE TYPE {owner}.{t.lower()} AS (placeholder text)")

        for stmt in ddl.statements_in_order(plan)[1:]:
            try:
                cur.execute("SAVEPOINT s")
                cur.execute(stmt)
                cur.execute("RELEASE SAVEPOINT s")
                ran += 1
            except Exception as exc:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT s")
                failures.append({"statement": stmt[:200],
                                 "error": _pg_message(exc)})
    finally:
        conn.rollback()
        conn.close()
    return {"ran": ran, "failed": len(failures), "failures": failures,
            "ok": not failures, "rolled_back": True}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PostgreSQL schema DDL from discovery")
    ap.add_argument("--run", default=None, help="collector run id (default: the latest)")
    ap.add_argument("--owner", default=None, help="schema (default: the estate in the run)")
    ap.add_argument("--compile", action="store_true",
                    help="run every statement on PostgreSQL inside a transaction, then roll back")
    ap.add_argument("--pg-dsn", default=os.environ.get("DBSHIFT_PG_DSN", "localhost:5432/dbshift"))
    ap.add_argument("--pg-user", default=os.environ.get("DBSHIFT_PG_USER", "dbshift"))
    ap.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = ap.parse_args(argv)

    run_id = args.run or _latest_run()
    owner = args.owner
    if not owner:
        tables = prov_records._dataset(run_id, "tables")
        owners = {t["owner"] for t in tables}
        if len(owners) != 1:
            raise SystemExit(f"run {run_id} holds {len(owners)} schemas; name one with --owner")
        owner = owners.pop()

    plan = build(run_id, owner)
    seqs = [ddl.ident(s["sequence_name"]) for s in prov_records._dataset(run_id, "sequences")
            if s.get("sequence_owner") == owner or s.get("owner") == owner]
    plan["sequences_needed"] = seqs

    print(f"\nestate    : {owner}   run {run_id}")
    print(f"tables    : {plan['counts']['tables']}")
    print(f"keys      : {plan['counts']['primary_unique']} primary/unique, "
          f"{plan['counts']['foreign']} foreign")
    print(f"checks    : {plan['counts']['check']}")
    print(f"indexes   : {plan['counts']['indexes']}")
    if plan.get("depends_on_types"):
        print(f"needs     : Phase 4b types {', '.join(plan['depends_on_types'])}")

    warn = [n for n in plan["notes"] if n["severity"] in ("warn", "error")]
    if warn:
        print(f"\nNEEDS A LOOK ({len(warn)})")
        for n in warn:
            print(f"  [{n['severity']:5}] {n['kind']:22} {n['subject']}")
            print(f"          {n['detail']}")

    if args.compile:
        pw = os.environ.get("DBSHIFT_PG_PASSWORD")
        if not pw:
            print("\nset DBSHIFT_PG_PASSWORD to compile. Nothing was run.")
            return 2
        print(f"\ncompiling on {args.pg_dsn} -- everything is rolled back")
        result = compile_check(plan, args.pg_dsn, args.pg_user, pw)
        plan["compile"] = result
        print(f"  {result['ran']} statement(s) ran, {result['failed']} failed")
        for f in result["failures"][:10]:
            print(f"    {f['error']}")
            print(f"      {f['statement'][:100]}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / "schema_ddl.json"
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    sql_path = args.output_dir / "schema.sql"
    sql_path.write_text(
        f"-- {owner} -> PostgreSQL, from collector run {run_id}\n"
        f"-- Generated by convert/ddl.py. NOTHING IS APPLIED by generating this.\n"
        f"-- Run the groups in this order: tables, then the data load, then keys,\n"
        f"-- then indexes. Loading into a table that already has its indexes and\n"
        f"-- foreign keys is the slowest way to do it.\n\n"
        + "\n\n".join(ddl.statements_in_order(plan)) + "\n",
        encoding="utf-8")

    print(f"\nwritten: {out}")
    print(f"         {sql_path}")
    if plan.get("compile") and not plan["compile"]["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
