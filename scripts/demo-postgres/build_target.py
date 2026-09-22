"""Create the target schema the converted code binds to, from Phase 4c's DDL.

    python build_target.py DBMIG_APP           # build it
    python build_target.py DBMIG_APP count     # report what is there

**This writes to the target and drops the estate schema first.** It is the one
destructive step in the demo, and it is scoped to a single schema on the local
PostgreSQL container -- not an RDS instance.

Why it exists: `convert.apply_run` creates functions, and a function that reads
`customer` needs `customer` to exist. Phase 4c generates that DDL and proves it
runs; this applies it, so the apply that follows has something to bind to. On a
real migration the same DDL is applied to the provisioned target before DMS
loads any data.

The user-defined types are a subtlety. Phase 4c's tables reference them, but the
types themselves are Phase 4b objects that the *apply* creates. So they are
created here as placeholders and dropped again before the apply runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from convert import ddl as ddl_mod  # noqa: E402
from convert import target as target_mod  # noqa: E402
from provision import records as prov_records  # noqa: E402

DATASETS = ("columns", "tables", "objects", "external_tables", "constraints",
            "constraint_columns", "indexes", "index_columns", "sequences", "queues")


def _target() -> target_mod.PgTarget:
    import os
    return target_mod.PgTarget.parse(
        os.environ.get("DBSHIFT_PG_DSN", "localhost:5432/dbshift"),
        os.environ.get("DBSHIFT_PG_USER", "dbshift"),
        os.environ.get("DBSHIFT_PG_PASSWORD", "dbshift-local-only"))


def count(owner: str) -> int:
    pg = _target()
    conn = pg.connect()
    cur = conn.cursor()
    schema = owner.lower()

    def scalar(sql):
        cur.execute(sql, (schema,))
        return cur.fetchone()[0]

    tables = scalar("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_type = 'BASE TABLE'")
    constraints = scalar("SELECT count(*) FROM pg_constraint c JOIN pg_namespace n "
                         "ON n.oid = c.connamespace WHERE n.nspname = %s")
    indexes = scalar("SELECT count(*) FROM pg_indexes WHERE schemaname = %s")
    types = scalar("SELECT count(*) FROM pg_type t JOIN pg_namespace n "
                   "ON n.oid = t.typnamespace WHERE n.nspname = %s AND t.typtype IN ('c','d')")
    functions = scalar("SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
                       "ON n.oid = p.pronamespace WHERE n.nspname = %s")
    conn.close()

    print(f"  in the target: {tables} tables, {constraints} constraints, {indexes} indexes, "
          f"{types} types, {functions} functions")
    return 0


def build(owner: str) -> int:
    plan_path = ROOT / "convert" / "output" / "conversion_plan.json"
    if not plan_path.exists():
        print("no conversion plan; run `python -m convert.run` first")
        return 1
    run_id = json.loads(plan_path.read_text(encoding="utf-8"))["collector_run_id"]

    datasets = {n: prov_records._dataset(run_id, n) for n in DATASETS}
    plan = ddl_mod.build(owner=owner, datasets=datasets)

    pg = _target()
    conn = pg.connect()
    cur = conn.cursor()
    schema = owner.lower()
    try:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute(plan["schema"])

        sequences = [s for s in datasets["sequences"]
                     if s.get("sequence_owner") == owner or s.get("owner") == owner]
        for s in sequences:
            cur.execute(f"CREATE SEQUENCE {schema}.{s['sequence_name'].lower()}")

        # Placeholders, so the tables that reference them can be created. The
        # real types are APPROVED Phase 4b objects and the apply creates them.
        for t in plan.get("depends_on_types", []):
            cur.execute(f"CREATE TYPE {schema}.{t.lower()} AS (placeholder text)")

        for stmt in ddl_mod.statements_in_order(plan)[1:]:
            cur.execute(stmt)
        conn.commit()

        for t in plan.get("depends_on_types", []):
            cur.execute(f"DROP TYPE IF EXISTS {schema}.{t.lower()} CASCADE")
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        print(f"  failed: {str(exc).splitlines()[0][:200]}")
        return 1
    finally:
        conn.close()

    c = plan["counts"]
    print(f"  built {c['tables']} table(s), {c['primary_unique']} key(s), "
          f"{c['foreign']} foreign key(s), {c['indexes']} index(es), "
          f"{len(sequences)} sequence(s)")
    if plan.get("depends_on_types"):
        print(f"  the apply will create: {', '.join(plan['depends_on_types'])}")
    return 0


def main() -> int:
    owner = sys.argv[1] if len(sys.argv) > 1 else "DBMIG_APP"
    mode = sys.argv[2] if len(sys.argv) > 2 else "build"
    return count(owner) if mode == "count" else build(owner)


if __name__ == "__main__":
    raise SystemExit(main())
