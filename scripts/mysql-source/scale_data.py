"""Grow dbmig_mysql_app to a target size, in place, without touching a defect.

    DBSHIFT_MYSQL_APP_PASSWORD='...' python scripts/mysql-source/scale_data.py \
        --dsn <host>:3306/dbmig_mysql_app --target-gb 5

The estate 03_generate_data.sql builds is ~35 MB -- right for a checksum that is
a real test, too small to say anything about load time, DMS throughput or how
long a level-4 validation takes on a database a client would recognise. This adds
orders (with their lines, payments and audit rows) and the customers who placed
them, in batches, until the schema reaches --target-gb.

What it deliberately leaves alone, so verify_defects.py still reports 12/12 and
answer_key.json still describes the estate:

  * every table that carries a seeded defect -- clickstream_raw (described in the
    answer key as 50,000 rows), contract_term, ledger_entry, supplier,
    warehouse_stock, customer_payment_detail, customer_feedback, order (the
    reserved-word table), legacy_import_log
  * product, product_category: new lines reference the existing 500 products
  * product_daily_sales and dbmig_mysql_rpt.revenue_snapshot: aggregates of the
    original 20,000 orders, left as they are rather than rewritten
  * the existing rows of every table -- this only inserts

The rows are generated ON THE SERVER by INSERT ... SELECT over a digits cross
join, so only statements cross the network. No helper table, procedure or view
is created: the estate's object counts are what Phases 1, 2 and 8 compare.

New keys never collide with the original estate's: ids continue from MAX(id),
and the new customer_ref / order_ref / email use widths and a domain the original
rows do not (CUST-0005001 vs CUST-005001; ...@scale.example.com).

Re-runnable: it measures the current size first and only adds what is missing.
Each batch is its own transaction, so an interruption leaves whole orders only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import pymysql

DIGITS = "(SELECT 0 d UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL " \
         "SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL " \
         "SELECT 8 UNION ALL SELECT 9)"
# 0 .. 999,999 -- enough for any batch this script issues.
SEQ = (f"(SELECT a.d + 10*b.d + 100*c.d + 1000*e.d + 10000*f.d + 100000*g.d AS n "
       f"FROM {DIGITS} a, {DIGITS} b, {DIGITS} c, {DIGITS} e, {DIGITS} f, {DIGITS} g)")

LINES_PER_ORDER = 3
ORDERS_PER_CUSTOMER = 6
GROWN = ("customer", "customer_address", "customer_order", "order_line", "payment", "order_audit")


def size_bytes(cur, schema: str) -> int:
    cur.execute("SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables "
                "WHERE table_schema = %s", (schema,))
    return int(cur.fetchone()[0])


def scalar(cur, sql: str) -> int:
    cur.execute(sql)
    return int(cur.fetchone()[0] or 0)


def add_batch(cur, orders: int) -> None:
    """One batch: `orders` orders, their customers, addresses, lines, payments, audit."""
    base_c = scalar(cur, "SELECT MAX(customer_id) FROM customer")
    base_o = scalar(cur, "SELECT MAX(order_id) FROM customer_order")
    customers = max(1, orders // ORDERS_PER_CUSTOMER)

    cur.execute(f"""
        INSERT INTO customer (customer_id, customer_ref, full_name, email, phone, country_code,
                              date_of_birth, credit_limit, loyalty_points, is_verified)
        SELECT {base_c} + s.n + 1 AS id,
               CONCAT('CUST-', LPAD({base_c} + s.n + 1, 7, '0')),
               CONCAT(ELT(((s.n + {base_c}) MOD 20) + 1,
                          'Alice','Benjamin','Chloe','Daniel','Elena','Francis','Grace',
                          'Hassan','Isabel','Jack','Karen','Liam','Maya','Noah','Olivia',
                          'Priya','Quentin','Rosa','Samuel','Tara'), ' ',
                      ELT(((s.n + {base_c}) MOD 15) + 1,
                          'Adams','Bennett','Clarke','Davies','Evans','Fletcher','Gray',
                          'Hughes','Ibrahim','Jones','Khan','Lewis','Morgan','Novak','Owen')),
               CONCAT('cust', {base_c} + s.n + 1, '@scale.example.com'),
               CASE WHEN s.n MOD 9 = 0 THEN NULL
                    ELSE CONCAT('+44 7', LPAD(({base_c} + s.n + 1) MOD 1000000000, 9, '0')) END,
               ELT((s.n MOD 6) + 1, 'GB','GB','GB','IE','DE','FR'),
               CASE WHEN s.n MOD 11 = 0 THEN NULL
                    ELSE DATE_SUB('2006-01-01', INTERVAL (s.n MOD 18250) DAY) END,
               ROUND((s.n MOD 40) * 250, 2),
               ((s.n + {base_c}) * 7) MOD 50000,
               CASE WHEN s.n MOD 4 = 0 THEN 0 ELSE 1 END
        FROM {SEQ} s WHERE s.n < {customers}""")

    # 1 billing address each, plus shipping for every second customer -- the
    # original estate's 1.5 per customer.
    cur.execute(f"""
        INSERT INTO customer_address (customer_id, address_type, line1, line2, city, postcode, country_code)
        SELECT c.customer_id, 'billing',
               CONCAT(c.customer_id MOD 200 + 1, ' ',
                      ELT((c.customer_id MOD 8) + 1, 'High Street','Station Road','Church Lane',
                          'Mill Road','Park Avenue','Queens Road','Victoria Street','Kings Way')),
               CASE WHEN c.customer_id MOD 5 = 0 THEN 'Flat 2' ELSE NULL END,
               ELT((c.customer_id MOD 10) + 1, 'London','Manchester','Birmingham','Leeds',
                   'Glasgow','Bristol','Dublin','Berlin','Munich','Paris'),
               CONCAT(ELT((c.customer_id MOD 4) + 1, 'SW','NW','SE','NE'),
                      c.customer_id MOD 20 + 1, ' ', c.customer_id MOD 9 + 1, 'AB'),
               c.country_code
        FROM customer c WHERE c.customer_id > {base_c}""")
    cur.execute(f"""
        INSERT INTO customer_address (customer_id, address_type, line1, city, postcode, country_code)
        SELECT c.customer_id, 'shipping', CONCAT('Unit ', c.customer_id MOD 50 + 1, ' Industrial Estate'),
               'London', 'E1 6AN', c.country_code
        FROM customer c WHERE c.customer_id > {base_c} AND c.customer_id MOD 2 = 0""")

    # Dated across the same three years as the original, so every order_audit
    # RANGE partition (2023-2026) keeps filling and none spills into pmax.
    cur.execute(f"""
        INSERT INTO customer_order (order_id, order_ref, customer_id, order_date, status,
                                    order_total, currency, ship_address_id)
        SELECT {base_o} + s.n + 1,
               CONCAT('ORD-', LPAD({base_o} + s.n + 1, 9, '0')),
               {base_c} + (s.n MOD {customers}) + 1,
               DATE_SUB('2026-09-01', INTERVAL (({base_o} + s.n + 1) MOD 1095) DAY),
               ELT((({base_o} + s.n + 1) MOD 10) + 1, 'new','paid','paid','paid','picking',
                   'shipped','shipped','shipped','cancelled','refunded'),
               0.00,
               ELT((({base_o} + s.n + 1) MOD 8) + 1, 'GBP','GBP','GBP','GBP','GBP','EUR','EUR','USD'),
               NULL
        FROM {SEQ} s WHERE s.n < {orders}""")

    # line_total is a STORED generated column -- the engine computes it.
    cur.execute(f"""
        INSERT INTO order_line (order_id, line_no, product_id, quantity, unit_price)
        SELECT o.order_id, l.line_no,
               ((o.order_id * 3 + l.line_no) MOD 500) + 1,
               ((o.order_id + l.line_no) MOD 7) + 1,
               ROUND(5 + ((o.order_id + l.line_no) MOD 400) + (((o.order_id + l.line_no) MOD 100) / 100), 4)
        FROM customer_order o
        JOIN (SELECT 1 AS line_no UNION ALL SELECT 2 UNION ALL SELECT 3) l
        WHERE o.order_id > {base_o}""")

    # Header totals from their lines, as 03 does. trg_order_audit_upd fires only
    # when status changes, so this UPDATE writes no audit rows.
    cur.execute(f"""
        UPDATE customer_order o
        JOIN (SELECT order_id, SUM(line_total) AS t FROM order_line
              WHERE order_id > {base_o} GROUP BY order_id) s ON s.order_id = o.order_id
        SET o.order_total = ROUND(s.t, 2)
        WHERE o.order_id > {base_o}""")

    cur.execute(f"""
        INSERT INTO payment (order_id, paid_at, amount, method, fx_rate, reference)
        SELECT o.order_id, o.order_date + INTERVAL (o.order_id MOD 72) HOUR, o.order_total,
               ELT((o.order_id MOD 4) + 1, 'card','card','paypal','transfer'),
               CASE o.currency WHEN 'GBP' THEN 1.0000000000 WHEN 'EUR' THEN 0.8567230000
                               ELSE 0.7891450000 END,
               CONCAT('PAY-', LPAD(o.order_id, 10, '0'))
        FROM customer_order o
        WHERE o.order_id > {base_o} AND o.status IN ('paid','picking','shipped')""")

    cur.execute(f"""
        INSERT INTO order_audit (event_year, order_id, event_type, old_status, new_status,
                                 changed_by, changed_at)
        SELECT YEAR(o.order_date), o.order_id, 'STATUS_CHANGE', 'new', o.status,
               CONCAT('batch_loader_', (o.order_id MOD 5) + 1), o.order_date
        FROM customer_order o WHERE o.order_id > {base_o}""")


def remove(conn, cur, chunk: int = 50_000) -> None:
    """Undo: delete only what this script added, recognised by its markers.

    Added orders carry a 9-digit order_ref (ORD-000020001; the original's are 8)
    and added customers an @scale.example.com email. Lines and addresses go with
    them by ON DELETE CASCADE; payments and audit rows have no cascade and are
    deleted first. Chunked, so no single transaction holds millions of rows.
    """
    def chunks(sql_ids: str, sql_del: str) -> int:
        total = 0
        while True:
            cur.execute(sql_ids + f" LIMIT {chunk}")
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                return total
            cur.execute(sql_del.format(ids=",".join(map(str, ids))))
            conn.commit()
            total += len(ids)

    orders = "SELECT order_id FROM customer_order WHERE CHAR_LENGTH(order_ref) = 13"
    cur.execute("SELECT MIN(order_id) FROM customer_order WHERE CHAR_LENGTH(order_ref) = 13")
    first = cur.fetchone()[0]
    if first is not None:
        n = chunks(f"SELECT DISTINCT order_id FROM payment WHERE order_id >= {first} "
                   f"AND reference LIKE 'PAY-%'", "DELETE FROM payment WHERE order_id IN ({ids})")
        print(f"payments removed: {n:,}")
        n = chunks(f"SELECT DISTINCT order_id FROM order_audit WHERE order_id >= {first} "
                   f"AND changed_by LIKE 'batch\\_loader\\_%'",
                   "DELETE FROM order_audit WHERE order_id IN ({ids})")
        print(f"audit rows removed for {n:,} orders")
    n = chunks(orders, "DELETE FROM customer_order WHERE order_id IN ({ids})")
    print(f"orders removed (their lines cascade): {n:,}")
    n = chunks("SELECT customer_id FROM customer WHERE email LIKE '%@scale.example.com'",
               "DELETE FROM customer WHERE customer_id IN ({ids})")
    print(f"customers removed (their addresses cascade): {n:,}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", required=True, help="host:port/database")
    ap.add_argument("--user", default="dbmig_app")
    ap.add_argument("--target-gb", type=float, default=5.0)
    ap.add_argument("--batch-orders", type=int, default=200_000)
    ap.add_argument("--dry-run", action="store_true", help="measure and report; insert nothing")
    ap.add_argument("--remove", action="store_true",
                    help="undo: delete every row this script added, nothing else")
    args = ap.parse_args(argv)

    password = os.environ.get("DBSHIFT_MYSQL_APP_PASSWORD")
    if not password:
        print("set DBSHIFT_MYSQL_APP_PASSWORD (the dbmig_app password); it is never printed",
              file=sys.stderr)
        return 2
    hostport, _, schema = args.dsn.partition("/")
    host, _, port = hostport.partition(":")
    schema = schema or "dbmig_mysql_app"

    conn = pymysql.connect(host=host, port=int(port or 3306), user=args.user, password=password,
                           database=schema, autocommit=False, read_timeout=3600, write_timeout=3600)
    cur = conn.cursor()
    # Fresh sizes after every batch, not the cached ones (24h by default).
    cur.execute("SET SESSION information_schema_stats_expiry = 0")
    target = int(args.target_gb * 1024 ** 3)

    if args.remove:
        remove(conn, cur)
        cur.execute("ANALYZE TABLE " + ", ".join(GROWN))
        cur.fetchall()
        print(f"{schema}: {size_bytes(cur, schema) / 1024**2:,.1f} MB after removal "
              "(InnoDB keeps freed pages; OPTIMIZE TABLE returns them to the disk)")
        conn.close()
        return 0

    start = size_bytes(cur, schema)
    print(f"{schema}: {start / 1024**2:,.1f} MB now, target {target / 1024**2:,.0f} MB")
    if args.dry_run or start >= target:
        print("nothing to add" if start >= target else "dry run: nothing inserted")
        return 0

    batches, t0 = 0, time.monotonic()
    while True:
        tb = time.monotonic()
        try:
            add_batch(cur, args.batch_orders)
            conn.commit()
        except Exception:
            conn.rollback()      # a batch is all or nothing
            raise
        batches += 1
        cur.execute("ANALYZE TABLE " + ", ".join(GROWN))
        cur.fetchall()
        now = size_bytes(cur, schema)
        print(f"batch {batches}: +{args.batch_orders:,} orders in {time.monotonic() - tb:,.0f}s "
              f"-> {now / 1024**2:,.0f} MB ({100 * now / target:.0f}%)", flush=True)
        if now >= target:
            break

    print(f"\ndone in {(time.monotonic() - t0) / 60:,.1f} min -- exact row counts:")
    for t in GROWN + ("clickstream_raw", "product_daily_sales"):
        print(f"  {t:<22}{scalar(cur, f'SELECT COUNT(*) FROM `{t}`'):>14,}")
    print(f"  {'TOTAL SIZE':<22}{size_bytes(cur, schema) / 1024**3:>13.2f} GB")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
