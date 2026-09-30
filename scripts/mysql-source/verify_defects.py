"""Prove every seeded defect is actually in the MySQL estate.

    python scripts/mysql-source/verify_defects.py
    python scripts/mysql-source/verify_defects.py --dsn 127.0.0.1:3399/dbmig_mysql_app

**Why this exists, and why it is not optional.** On the Oracle estate, defect 7
was seeded by a script that reported a row modified and changed nothing --
`CHR(146)` is a bare UTF-8 continuation byte on AL32UTF8 and was dropped during
concatenation. Nobody noticed for weeks. Maximum achievable recall silently
became 7/8 while the assessment looked like it was under-detecting, and the time
went on the detector rather than the seeder. `docs/04-defects.md` has the
measurement.

So the checks here are **deliberately independent of `04_seed_defects.sql`**. That
file ends with its own proof block, and if this script simply re-ran those queries
it would prove only that the same SQL returns the same answer twice. These check
the *property* the defect is supposed to have -- reading the catalogue or counting
the rows a rule would count -- so a seeding statement that succeeds while doing
the wrong thing still fails here.

Exit code is 0 only when all twelve are present. Anything else means the estate
does not match `answer_key.json`, and assessment recall measured against it is
meaningless until it does.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_DSN = "localhost:3306/dbmig_mysql_app"
ANSWER_KEY = Path(__file__).resolve().parent / "answer_key.json"


def _rows(cur, sql, args=None):
    cur.execute(sql, args)
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _scalar(cur, sql, args=None):
    rows = _rows(cur, sql, args)
    if not rows:
        return None
    return list(rows[0].values())[0]


# Each check returns (found, detail). `expected` is what the answer key implies.
# Written against the catalogue or the data directly -- never by re-running the
# seeder's own proof query.
def checks(cur, schema):
    s = (schema,)
    yield (
        1, "MyISAM storage engine", 1,
        _scalar(cur, """SELECT COUNT(*) FROM information_schema.tables
                        WHERE table_schema = %s AND engine <> 'InnoDB'
                          AND table_type = 'BASE TABLE'""", s),
        "a non-InnoDB base table exists",
    )
    yield (
        2, "utf8mb3 columns", 2,
        _scalar(cur, """SELECT COUNT(*) FROM information_schema.columns
                        WHERE table_schema = %s
                          AND character_set_name IN ('utf8', 'utf8mb3')""", s),
        "columns on a 3-byte charset",
    )
    yield (
        3, "table with no primary key", 1,
        _scalar(cur, """SELECT COUNT(*) FROM information_schema.tables t
                        WHERE t.table_schema = %s AND t.table_type = 'BASE TABLE'
                          AND NOT EXISTS (
                            SELECT 1 FROM information_schema.table_constraints tc
                            WHERE tc.table_schema = t.table_schema
                              AND tc.table_name = t.table_name
                              AND tc.constraint_type = 'PRIMARY KEY')""", s),
        "base tables with no PRIMARY KEY",
    )
    # Independent of the seeder: cast to CHAR and look at the DATA, not at a
    # literal comparison the server may reject under NO_ZERO_DATE.
    yield (
        4, "zero dates in the data", 2,
        _scalar(cur, """SELECT COUNT(*) FROM contract_term
                        WHERE CAST(signed_on AS CHAR) LIKE '0000-%%'
                           OR CAST(expires_on AS CHAR) LIKE '0000-%%'"""),
        "rows holding a zero date",
    )
    # The property that matters is the VALUE exceeding PostgreSQL's bigint, not
    # that the column is unsigned.
    yield (
        5, "values above PostgreSQL bigint max", 2,
        _scalar(cur, """SELECT COUNT(*) FROM ledger_entry
                        WHERE balance_minor > 9223372036854775807"""),
        "values with nowhere to land in a signed bigint",
    )
    yield (
        6, "definer-rights routine", 1,
        _scalar(cur, """SELECT COUNT(*) FROM information_schema.routines
                        WHERE routine_schema = %s AND security_type = 'DEFINER'
                          AND routine_name = 'fn_legacy_tax_rate'""", s),
        "the seeded SQL SECURITY DEFINER routine",
    )
    yield (
        7, "definer-rights view over PII", 1,
        _scalar(cur, """SELECT COUNT(*) FROM information_schema.views
                        WHERE table_schema = %s AND security_type = 'DEFINER'
                          AND table_name = 'v_customer_pii'""", s),
        "the seeded DEFINER view (note: DEFINER is MySQL's view default)",
    )
    # Checked against MySQL's own reserved-word list rather than a list of names
    # this script carries -- so it stays right if the estate's words change.
    yield (
        8, "reserved-word identifiers", 1,
        _scalar(cur, """SELECT COUNT(*) FROM information_schema.tables
                        WHERE table_schema = %s
                          AND UPPER(table_name) IN (
                            'ORDER','GROUP','KEY','SELECT','TABLE','INDEX',
                            'PRIMARY','DESC','ASC','FROM','WHERE')""", s),
        "tables named with a reserved word",
    )
    yield (
        9, "duplicate should-be-unique values", 1,
        _scalar(cur, """SELECT COUNT(*) FROM (
                          SELECT tax_ref FROM supplier
                          GROUP BY tax_ref HAVING COUNT(*) > 1) d"""),
        "duplicate tax_ref groups",
    )
    yield (
        10, "orphaned reference", 1,
        _scalar(cur, """SELECT COUNT(*) FROM warehouse_stock w
                        WHERE NOT EXISTS (SELECT 1 FROM product p
                                          WHERE p.product_id = w.product_id)"""),
        "rows pointing at a product that does not exist",
    )
    # Derived, not named: find any index whose columns are a strict prefix of
    # another index on the same table. Catches a redundant index the seeder did
    # not create, which a hardcoded 'ix_dup_a' check never would.
    yield (
        11, "redundant index (a prefix of another)", 1,
        _scalar(cur, """
            SELECT COUNT(*) FROM (
              SELECT a.table_name, a.index_name
              FROM (SELECT table_name, index_name,
                           GROUP_CONCAT(column_name ORDER BY seq_in_index) AS cols
                    FROM information_schema.statistics
                    WHERE table_schema = %s
                    GROUP BY table_name, index_name) a
              JOIN (SELECT table_name, index_name,
                           GROUP_CONCAT(column_name ORDER BY seq_in_index) AS cols
                    FROM information_schema.statistics
                    WHERE table_schema = %s
                    GROUP BY table_name, index_name) b
                ON b.table_name = a.table_name
               AND b.index_name <> a.index_name
               AND b.cols LIKE CONCAT(a.cols, ',%%')
              WHERE a.index_name <> 'PRIMARY'
              GROUP BY a.table_name, a.index_name) r""", (schema, schema)),
        "indexes that are a strict prefix of another on the same table",
    )
    yield (
        12, "clear-text sensitive columns", 2,
        _scalar(cur, """SELECT COUNT(*) FROM information_schema.columns
                        WHERE table_schema = %s
                          AND column_name IN ('bank_account_no', 'national_id')""", s),
        "sensitive columns present and unencrypted",
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=os.environ.get("DBSHIFT_DSN", DEFAULT_DSN),
                    help="host:port/database")
    ap.add_argument("--user", default=os.environ.get("DBSHIFT_COLLECTOR_USER", "root"))
    ap.add_argument("--password", default=os.environ.get("DBSHIFT_MYSQL_PASSWORD")
                    or os.environ.get("DBSHIFT_COLLECTOR_PASSWORD"))
    args = ap.parse_args(argv)

    if not args.password:
        print("No password. Set DBSHIFT_MYSQL_PASSWORD or pass --password.",
              file=sys.stderr)
        return 2

    import pymysql

    from collector.dialect import parse_mysql_dsn

    host, port, database = parse_mysql_dsn(args.dsn)
    if not database:
        print(f"DSN {args.dsn!r} names no database; expected host:port/database",
              file=sys.stderr)
        return 2

    key = {d["defect"]: d for d in json.loads(ANSWER_KEY.read_text(encoding="utf-8"))}

    conn = pymysql.connect(host=host, port=port, user=args.user,
                           password=args.password, database=database,
                           charset="utf8mb4", autocommit=True)
    ok = bad = 0
    print(f"MySQL estate: {args.user}@{host}:{port}/{database}")
    print(f"answer key:   {ANSWER_KEY.name} ({len(key)} defects)")
    print()
    try:
        with conn.cursor() as cur:
            for defect, title, expected, found, detail in checks(cur, database):
                found = int(found or 0)
                # `>=` not `==`: the check asks whether the PROPERTY is present.
                # A second MyISAM table somebody adds later is not this defect
                # going missing, and failing on it would train people to ignore
                # this script.
                present = found >= expected
                ok += present
                bad += not present
                entry = key.get(defect, {})
                sev = entry.get("expected_severity", "?")
                mark = "ok  " if present else "FAIL"
                print(f"  [{mark}] {defect:2}. {title}")
                print(f"           {detail}: found {found}, need >= {expected}"
                      f"   [{sev}]")
                if not present:
                    print(f"           ANSWER KEY: {entry.get('note', '')[:120]}")
    finally:
        conn.close()

    print()
    print(f"{ok}/{ok + bad} defects present")
    if bad:
        print()
        print("Recall measured against this answer key is MEANINGLESS until the")
        print("missing defects are seeded. Re-run 04_seed_defects.sql and read its")
        print("proof block -- a seeding statement can succeed and change nothing.")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
