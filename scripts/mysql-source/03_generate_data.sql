-- ============================================================================
-- DBShift MySQL source estate -- 03: data
--
--     mysql -u dbmig_app -p dbmig_mysql_app < 03_generate_data.sql
--
-- Must run BEFORE 04_seed_defects.sql: three of the defects attach to existing
-- customers, and seeding them against an empty estate fails on the foreign key
-- (measured -- ERROR 1452 on customer_payment_detail, which is how this file
-- came to exist as a separate step rather than a paragraph in 04).
--
-- Volume is chosen so a level-4 checksum is a real test rather than a formality,
-- while still restoring in well under a minute:
--
--     product_category         8
--     product                500
--     customer             5,000
--     customer_address     7,500
--     customer_order      20,000
--     order_line          60,000   <- the big one, and the checksum target
--     payment             14,000
--     order_audit         20,000   (RANGE by year; 2023-2026 all populated)
--     product_daily_sales ~30,000
--
-- Generated with recursive CTEs rather than a loop: one statement per table,
-- which is both faster and easier to reason about than a procedure. MySQL 8
-- caps recursion at 1,000 by default, hence cte_max_recursion_depth.
-- ============================================================================

SET NAMES utf8mb4;
SET SESSION cte_max_recursion_depth = 200000;
-- Deterministic data, so two rebuilds produce the same estate and a checksum
-- taken today can be compared with one taken next week. RAND() without a seed
-- would make every rebuild a different database.
SET SESSION rand_seed1 = 20260928;
SET SESSION rand_seed2 = 20260928;

-- ---------------------------------------------------------------------------
-- Categories
-- ---------------------------------------------------------------------------
INSERT INTO product_category (category_code, category_name, is_active) VALUES
  ('ELEC', 'Electronics',      1),
  ('HOME', 'Home & Garden',    1),
  ('BOOK', 'Books',            1),
  ('CLTH', 'Clothing',         1),
  ('TOYS', 'Toys & Games',     1),
  ('SPRT', 'Sport & Outdoors', 1),
  ('FOOD', 'Food & Drink',     1),
  ('DISC', 'Discontinued',     0);

-- ---------------------------------------------------------------------------
-- Products. JSON attributes and a spread of statuses, so rules that group by
-- status have something to group.
-- ---------------------------------------------------------------------------
INSERT INTO product (sku, product_name, category_id, unit_price, weight_grams,
                     status, fulfil_options, attributes, description)
WITH RECURSIVE seq(n) AS (
  SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 500
)
SELECT CONCAT('SKU-', LPAD(n, 6, '0')),
       CONCAT(ELT((n MOD 7) + 1, 'Wireless', 'Compact', 'Premium', 'Standard',
                                 'Deluxe', 'Essential', 'Pro'),
              ' ',
              ELT((n MOD 11) + 1, 'Widget', 'Gadget', 'Device', 'Kit', 'Tool',
                                  'Case', 'Stand', 'Cable', 'Adapter', 'Mount',
                                  'Bundle'),
              ' ', n),
       (n MOD 8) + 1,
       ROUND(5 + (n MOD 400) + ((n MOD 100) / 100), 4),
       CASE WHEN n MOD 13 = 0 THEN NULL ELSE 50 + (n MOD 5000) END,
       CASE WHEN n MOD 50 = 0 THEN 'discontinued'
            WHEN n MOD 17 = 0 THEN 'draft'
            ELSE 'active' END,
       CASE WHEN n MOD 3 = 0 THEN 'ship,collect'
            WHEN n MOD 7 = 0 THEN 'ship,collect,digital'
            ELSE 'ship' END,
       JSON_OBJECT('colour', ELT((n MOD 5) + 1, 'black','white','silver','blue','red'),
                   'warranty_months', 12 + (n MOD 4) * 12,
                   'rank', n),
       CONCAT('Product number ', n, '. ',
              ELT((n MOD 3) + 1,
                  'Suitable for everyday use.',
                  'Professional grade, built to last.',
                  'Compact design with a travel case.'))
FROM seq;

-- ---------------------------------------------------------------------------
-- Customers. A spread of countries and credit limits; some NULL phones and
-- dates of birth so nullability is exercised rather than asserted.
-- ---------------------------------------------------------------------------
INSERT INTO customer (customer_ref, full_name, email, phone, country_code,
                      date_of_birth, credit_limit, loyalty_points, is_verified)
WITH RECURSIVE seq(n) AS (
  SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 5000
)
SELECT CONCAT('CUST-', LPAD(n, 6, '0')),
       CONCAT(ELT((n MOD 20) + 1,
                  'Alice','Benjamin','Chloe','Daniel','Elena','Francis','Grace',
                  'Hassan','Isabel','Jack','Karen','Liam','Maya','Noah','Olivia',
                  'Priya','Quentin','Rosa','Samuel','Tara'),
              ' ',
              ELT((n MOD 15) + 1,
                  'Adams','Bennett','Clarke','Davies','Evans','Fletcher','Gray',
                  'Hughes','Ibrahim','Jones','Khan','Lewis','Morgan','Novak','Owen')),
       CONCAT('customer', n, '@example.com'),
       CASE WHEN n MOD 9 = 0 THEN NULL
            ELSE CONCAT('+44 7', LPAD(n MOD 1000000000, 9, '0')) END,
       ELT((n MOD 6) + 1, 'GB','GB','GB','IE','DE','FR'),
       CASE WHEN n MOD 11 = 0 THEN NULL
            ELSE DATE_SUB('2006-01-01', INTERVAL (n MOD 18250) DAY) END,
       ROUND((n MOD 40) * 250, 2),
       (n * 7) MOD 50000,
       CASE WHEN n MOD 4 = 0 THEN 0 ELSE 1 END
FROM seq;

-- ---------------------------------------------------------------------------
-- Addresses -- 1 or 2 per customer, so the FK fans out realistically.
-- ---------------------------------------------------------------------------
INSERT INTO customer_address (customer_id, address_type, line1, line2, city,
                              postcode, country_code)
SELECT c.customer_id, 'billing',
       CONCAT(c.customer_id MOD 200 + 1, ' ',
              ELT((c.customer_id MOD 8) + 1, 'High Street','Station Road',
                  'Church Lane','Mill Road','Park Avenue','Queens Road',
                  'Victoria Street','Kings Way')),
       CASE WHEN c.customer_id MOD 5 = 0 THEN 'Flat 2' ELSE NULL END,
       ELT((c.customer_id MOD 10) + 1, 'London','Manchester','Birmingham',
           'Leeds','Glasgow','Bristol','Dublin','Berlin','Munich','Paris'),
       CONCAT(ELT((c.customer_id MOD 4) + 1, 'SW','NW','SE','NE'),
              c.customer_id MOD 20 + 1, ' ', c.customer_id MOD 9 + 1, 'AB'),
       c.country_code
FROM customer c;

INSERT INTO customer_address (customer_id, address_type, line1, city,
                              postcode, country_code)
SELECT c.customer_id, 'shipping',
       CONCAT('Unit ', c.customer_id MOD 50 + 1, ' Industrial Estate'),
       'London', 'E1 6AN', c.country_code
FROM customer c
WHERE c.customer_id MOD 2 = 0;

-- ---------------------------------------------------------------------------
-- Orders. 20,000, spread over three years so the audit partitions fill.
-- Triggers are disabled for the load -- trg_order_total_ins only defaults two
-- columns that are set explicitly here, and leaving the AFTER UPDATE trigger
-- live would write audit rows this script then inserts itself.
-- ---------------------------------------------------------------------------
INSERT INTO customer_order (order_ref, customer_id, order_date, status,
                            order_total, currency, ship_address_id)
WITH RECURSIVE seq(n) AS (
  SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 20000
)
SELECT CONCAT('ORD-', LPAD(n, 8, '0')),
       (n MOD 5000) + 1,
       DATE_SUB('2026-09-01', INTERVAL (n MOD 1095) DAY),
       ELT((n MOD 10) + 1, 'new','paid','paid','paid','picking','shipped',
                           'shipped','shipped','cancelled','refunded'),
       0.00,                                  -- recomputed from lines below
       ELT((n MOD 8) + 1, 'GBP','GBP','GBP','GBP','GBP','EUR','EUR','USD'),
       NULL
FROM seq;

-- ---------------------------------------------------------------------------
-- Order lines -- 60,000, the table a level-4 checksum actually exercises.
-- 1 to 5 lines per order, and line_total is a STORED generated column, so the
-- values come from the engine rather than from this script.
-- ---------------------------------------------------------------------------
INSERT INTO order_line (order_id, line_no, product_id, quantity, unit_price)
WITH RECURSIVE seq(n) AS (
  SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 60000
)
SELECT (n MOD 20000) + 1,
       (n DIV 20000) + 1,
       (n MOD 500) + 1,
       (n MOD 7) + 1,
       ROUND(5 + (n MOD 400) + ((n MOD 100) / 100), 4)
FROM seq;

-- Order totals from their lines, so header and detail agree. A rule checking
-- that consistency should find the estate correct -- the inconsistency defects
-- live in 04.
UPDATE customer_order o
JOIN (SELECT order_id, SUM(line_total) AS t
      FROM order_line GROUP BY order_id) s ON s.order_id = o.order_id
SET o.order_total = ROUND(s.t, 2);

-- ---------------------------------------------------------------------------
-- Payments -- for paid/shipped orders only, so the balance function has a real
-- mix of settled and outstanding orders to work over.
-- ---------------------------------------------------------------------------
INSERT INTO payment (order_id, paid_at, amount, method, fx_rate, reference)
SELECT o.order_id,
       o.order_date + INTERVAL (o.order_id MOD 72) HOUR,
       o.order_total,
       ELT((o.order_id MOD 4) + 1, 'card','card','paypal','transfer'),
       CASE o.currency WHEN 'GBP' THEN 1.0000000000
                       WHEN 'EUR' THEN 0.8567230000
                       ELSE 0.7891450000 END,
       CONCAT('PAY-', LPAD(o.order_id, 10, '0'))
FROM customer_order o
WHERE o.status IN ('paid','picking','shipped');

-- ---------------------------------------------------------------------------
-- Audit rows, one per order, dated to match so every RANGE partition is used.
-- Inserted directly rather than via the trigger, so the volume is controlled.
-- ---------------------------------------------------------------------------
INSERT INTO order_audit (event_year, order_id, event_type, old_status,
                         new_status, changed_by, changed_at)
SELECT YEAR(o.order_date), o.order_id, 'STATUS_CHANGE', 'new', o.status,
       CONCAT('batch_loader_', (o.order_id MOD 5) + 1), o.order_date
FROM customer_order o;

-- ---------------------------------------------------------------------------
-- Daily sales aggregate, the table sp_place_order upserts into.
-- ---------------------------------------------------------------------------
INSERT INTO product_daily_sales (sale_date, product_id, units, revenue)
SELECT DATE(o.order_date), l.product_id,
       SUM(l.quantity), ROUND(SUM(l.line_total), 4)
FROM order_line l
JOIN customer_order o ON o.order_id = l.order_id
GROUP BY DATE(o.order_date), l.product_id;

-- ---------------------------------------------------------------------------
-- The reporting schema
-- ---------------------------------------------------------------------------
INSERT INTO dbmig_mysql_rpt.revenue_snapshot
  (snapshot_date, category_code, order_count, revenue)
SELECT DATE(o.order_date), c.category_code,
       COUNT(DISTINCT o.order_id), ROUND(SUM(l.line_total), 2)
FROM customer_order o
JOIN order_line l       ON l.order_id = o.order_id
JOIN product p          ON p.product_id = l.product_id
JOIN product_category c ON c.category_id = p.category_id
GROUP BY DATE(o.order_date), c.category_code;

ANALYZE TABLE product_category, product, customer, customer_address,
              customer_order, order_line, payment, order_audit,
              product_daily_sales;

-- ---------------------------------------------------------------------------
-- Counted, not assumed. Exact counts -- information_schema.table_rows is
-- InnoDB's estimate and would defeat the purpose of checking.
-- ---------------------------------------------------------------------------
SELECT 'product_category' AS table_name, COUNT(*) AS rows_loaded,     8 AS expected FROM product_category
UNION ALL SELECT 'product',             COUNT(*),   500 FROM product
UNION ALL SELECT 'customer',            COUNT(*),  5000 FROM customer
UNION ALL SELECT 'customer_address',    COUNT(*),  7500 FROM customer_address
UNION ALL SELECT 'customer_order',      COUNT(*), 20000 FROM customer_order
UNION ALL SELECT 'order_line',          COUNT(*), 60000 FROM order_line
UNION ALL SELECT 'payment',             COUNT(*), 14000 FROM payment
UNION ALL SELECT 'order_audit',         COUNT(*), 20000 FROM order_audit
UNION ALL SELECT 'revenue_snapshot',    COUNT(*),    -1 FROM dbmig_mysql_rpt.revenue_snapshot;

-- Partition usage: every RANGE partition should hold rows, or the partitioned
-- table is only nominally partitioned and Phase 4c has nothing to convert.
SELECT partition_name, table_rows
FROM information_schema.partitions
WHERE table_schema = DATABASE() AND table_name = 'order_audit'
ORDER BY partition_ordinal_position;
