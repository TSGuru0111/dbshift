-- ============================================================================
-- DBShift MySQL source estate -- 02: schema objects
--
--     mysql -u dbmig_app -p dbmig_mysql_app < 02_schema_objects.sql
--
-- A retail order estate: customers, orders, order lines, products, payments,
-- plus a partitioned audit table and an archive. Deliberately NOT a copy of the
-- Oracle estate's lending domain -- a second estate that exercises the same
-- shapes tells you less than one that exercises different ones.
--
-- Every object here is CLEAN. The defects go in 04_seed_defects.sql, so that
-- file alone is the answer key's counterpart and this one can be read as "what
-- good looks like". The Oracle estate mixes the two and it is harder to review.
--
-- WHAT THIS EXERCISES, and why each matters to a migration:
--   InnoDB + utf8mb4              the correct baseline
--   generated columns             MySQL 8 feature with a PostgreSQL equivalent
--   CHECK constraints             8.0.16+; PostgreSQL has them, Oracle has them
--   ENUM / SET                    no PostgreSQL equivalent -- needs a type or a check
--   unsigned integers            PostgreSQL has none; BIGINT UNSIGNED can overflow
--   JSON columns                  both engines have JSON, with different operators
--   RANGE partitioning            different syntax and semantics on PostgreSQL
--   stored procs / funcs          SQL/PSM -> PL/pgSQL, Phase 4b's work
--   triggers                      BEFORE/AFTER, and the RETURN NEW trap on PG
--   events                        the scheduler; RDS manages it differently
--   FULLTEXT index                reported as an index type, not a catalogue
-- ============================================================================

-- RE-RUNNABLE. Run 00_reset.sql first, or run this against an empty schema.
-- The cross-schema table in dbmig_mysql_rpt is dropped explicitly here because
-- resetting dbmig_mysql_app alone leaves it behind -- which is exactly how the
-- first re-run of this script failed with "Table 'revenue_snapshot' already
-- exists" after the app schema had been recreated.
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 1;

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------
CREATE TABLE product_category (
  category_id    INT UNSIGNED    NOT NULL AUTO_INCREMENT,
  category_code  VARCHAR(20)     NOT NULL,
  category_name  VARCHAR(100)    NOT NULL,
  is_active      TINYINT(1)      NOT NULL DEFAULT 1,
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (category_id),
  UNIQUE KEY uq_category_code (category_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='Product categories. is_active is TINYINT(1), which DMS maps to boolean.';

CREATE TABLE product (
  product_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku            VARCHAR(40)     NOT NULL,
  product_name   VARCHAR(200)    NOT NULL,
  category_id    INT UNSIGNED    NOT NULL,
  -- DECIMAL, not FLOAT: the Oracle path lost 38 significant digits to a FLOAT
  -- mapped onto DOUBLE PRECISION, caught only by the level-4 checksum. The
  -- correct type is the one that survives the move.
  unit_price     DECIMAL(12,4)   NOT NULL,
  weight_grams   INT UNSIGNED        NULL,
  -- ENUM has no PostgreSQL equivalent; it becomes a CHECK or an enum type.
  status         ENUM('draft','active','discontinued') NOT NULL DEFAULT 'draft',
  -- SET is worse: PostgreSQL has nothing like it at all.
  fulfil_options SET('ship','collect','digital') NOT NULL DEFAULT 'ship',
  attributes     JSON                NULL,
  description     TEXT               NULL,
  -- A generated column. PostgreSQL has GENERATED ALWAYS AS too, so this
  -- converts -- but STORED vs VIRTUAL is not the same distinction on both.
  price_with_tax DECIMAL(14,4) AS (unit_price * 1.20) STORED,
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (product_id),
  UNIQUE KEY uq_product_sku (sku),
  KEY ix_product_category (category_id),
  KEY ix_product_status (status),
  CONSTRAINT fk_product_category FOREIGN KEY (category_id)
    REFERENCES product_category (category_id),
  CONSTRAINT ck_product_price CHECK (unit_price >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- FULLTEXT: an index type, reported in `indexes`, with no separate catalogue the
-- way Oracle Text has one.
CREATE FULLTEXT INDEX ft_product_desc ON product (product_name, description);

-- ---------------------------------------------------------------------------
-- Customers. utf8mb4 throughout here -- the utf8mb3 defect is seeded in 04.
-- ---------------------------------------------------------------------------
CREATE TABLE customer (
  customer_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  customer_ref   VARCHAR(20)     NOT NULL,
  full_name      VARCHAR(200)    NOT NULL,
  email          VARCHAR(255)    NOT NULL,
  phone          VARCHAR(40)         NULL,
  country_code   CHAR(2)         NOT NULL DEFAULT 'GB',
  date_of_birth  DATE                NULL,
  credit_limit   DECIMAL(14,2)   NOT NULL DEFAULT 0.00,
  loyalty_points INT UNSIGNED    NOT NULL DEFAULT 0,
  is_verified    TINYINT(1)      NOT NULL DEFAULT 0,
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (customer_id),
  UNIQUE KEY uq_customer_ref (customer_ref),
  UNIQUE KEY uq_customer_email (email),
  KEY ix_customer_country (country_code),
  CONSTRAINT ck_customer_credit CHECK (credit_limit >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE customer_address (
  address_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  customer_id    BIGINT UNSIGNED NOT NULL,
  address_type   ENUM('billing','shipping') NOT NULL,
  line1          VARCHAR(200)    NOT NULL,
  line2          VARCHAR(200)        NULL,
  city           VARCHAR(100)    NOT NULL,
  postcode       VARCHAR(20)     NOT NULL,
  country_code   CHAR(2)         NOT NULL DEFAULT 'GB',
  PRIMARY KEY (address_id),
  KEY ix_address_customer (customer_id),
  CONSTRAINT fk_address_customer FOREIGN KEY (customer_id)
    REFERENCES customer (customer_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- Orders
-- ---------------------------------------------------------------------------
CREATE TABLE customer_order (
  order_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_ref      VARCHAR(30)     NOT NULL,
  customer_id    BIGINT UNSIGNED NOT NULL,
  order_date     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status         ENUM('new','paid','picking','shipped','cancelled','refunded')
                                 NOT NULL DEFAULT 'new',
  order_total    DECIMAL(14,2)   NOT NULL DEFAULT 0.00,
  currency       CHAR(3)         NOT NULL DEFAULT 'GBP',
  ship_address_id BIGINT UNSIGNED    NULL,
  notes          TEXT                NULL,
  PRIMARY KEY (order_id),
  UNIQUE KEY uq_order_ref (order_ref),
  KEY ix_order_customer (customer_id),
  KEY ix_order_date (order_date),
  KEY ix_order_status_date (status, order_date),
  CONSTRAINT fk_order_customer FOREIGN KEY (customer_id)
    REFERENCES customer (customer_id),
  CONSTRAINT fk_order_address FOREIGN KEY (ship_address_id)
    REFERENCES customer_address (address_id),
  CONSTRAINT ck_order_total CHECK (order_total >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE order_line (
  order_line_id  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id       BIGINT UNSIGNED NOT NULL,
  line_no        SMALLINT UNSIGNED NOT NULL,
  product_id     BIGINT UNSIGNED NOT NULL,
  quantity       INT UNSIGNED    NOT NULL DEFAULT 1,
  unit_price     DECIMAL(12,4)   NOT NULL,
  line_total     DECIMAL(14,4) AS (quantity * unit_price) STORED,
  PRIMARY KEY (order_line_id),
  UNIQUE KEY uq_order_line (order_id, line_no),
  KEY ix_line_product (product_id),
  CONSTRAINT fk_line_order FOREIGN KEY (order_id)
    REFERENCES customer_order (order_id) ON DELETE CASCADE,
  CONSTRAINT fk_line_product FOREIGN KEY (product_id)
    REFERENCES product (product_id),
  CONSTRAINT ck_line_qty CHECK (quantity > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE payment (
  payment_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id       BIGINT UNSIGNED NOT NULL,
  paid_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  amount         DECIMAL(14,2)   NOT NULL,
  method         ENUM('card','paypal','transfer','voucher') NOT NULL,
  -- A deliberately wide DECIMAL. MySQL's maximum is DECIMAL(65,30) and
  -- PostgreSQL numeric has no such ceiling, so this direction is safe -- but the
  -- reverse is not, and Phase 8's checksum is what proves which.
  fx_rate        DECIMAL(20,10)  NOT NULL DEFAULT 1.0000000000,
  reference      VARCHAR(100)        NULL,
  PRIMARY KEY (payment_id),
  KEY ix_payment_order (order_id),
  KEY ix_payment_paid_at (paid_at),
  CONSTRAINT fk_payment_order FOREIGN KEY (order_id)
    REFERENCES customer_order (order_id),
  CONSTRAINT ck_payment_amount CHECK (amount <> 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- A RANGE-partitioned audit table.
--
-- MySQL requires every column of every unique key to be in the partitioning
-- expression, which is why the PK is composite (audit_id, event_year) rather
-- than audit_id alone. PostgreSQL has the same rule for its partitioned tables,
-- so this converts -- but the partition DDL does not, and Phase 4c has to
-- generate PostgreSQL's declarative form rather than translate MySQL's.
-- ---------------------------------------------------------------------------
CREATE TABLE order_audit (
  audit_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_year     SMALLINT        NOT NULL,
  order_id       BIGINT UNSIGNED NOT NULL,
  event_type     VARCHAR(40)     NOT NULL,
  old_status     VARCHAR(20)         NULL,
  new_status     VARCHAR(20)         NULL,
  changed_by     VARCHAR(100)    NOT NULL,
  changed_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (audit_id, event_year),
  KEY ix_audit_order (order_id),
  KEY ix_audit_changed_at (changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
PARTITION BY RANGE (event_year) (
  PARTITION p2023 VALUES LESS THAN (2024),
  PARTITION p2024 VALUES LESS THAN (2025),
  PARTITION p2025 VALUES LESS THAN (2026),
  PARTITION p2026 VALUES LESS THAN (2027),
  PARTITION pmax  VALUES LESS THAN MAXVALUE
);

-- ---------------------------------------------------------------------------
-- Views. The second one is SQL SECURITY DEFINER, which is a real RDS problem:
-- the definer must exist on the target and RDS grants no SUPER, so a view
-- defined by root@localhost fails at runtime rather than at migration time.
-- Seeded clean here (definer is dbmig_app); 04 seeds the broken case.
-- ---------------------------------------------------------------------------
CREATE VIEW v_order_summary AS
SELECT o.order_id,
       o.order_ref,
       o.order_date,
       o.status,
       c.customer_ref,
       c.full_name,
       COUNT(l.order_line_id) AS line_count,
       SUM(l.line_total)      AS computed_total
FROM customer_order o
JOIN customer       c ON c.customer_id = o.customer_id
LEFT JOIN order_line l ON l.order_id   = o.order_id
GROUP BY o.order_id, o.order_ref, o.order_date, o.status,
         c.customer_ref, c.full_name;

CREATE VIEW v_product_revenue AS
SELECT p.product_id,
       p.sku,
       p.product_name,
       cat.category_name,
       COUNT(DISTINCT l.order_id) AS order_count,
       SUM(l.quantity)            AS units_sold,
       SUM(l.line_total)          AS revenue
FROM product p
JOIN product_category cat ON cat.category_id = p.category_id
LEFT JOIN order_line  l   ON l.product_id    = p.product_id
GROUP BY p.product_id, p.sku, p.product_name, cat.category_name;

-- ---------------------------------------------------------------------------
-- Stored routines -- Phase 4b's actual work on this path.
--
-- Each uses at least one construct that does NOT translate mechanically to
-- PL/pgSQL, so the tier routing has something real to classify:
--   GROUP_CONCAT      -> string_agg
--   IFNULL            -> COALESCE
--   LIMIT n, m        -> LIMIT m OFFSET n
--   SIGNAL SQLSTATE   -> RAISE EXCEPTION
--   ON DUPLICATE KEY  -> ON CONFLICT
--   DECLARE HANDLER   -> EXCEPTION block
-- ---------------------------------------------------------------------------
DELIMITER $$

-- Rule-tier: a straightforward function. IFNULL and a scalar SELECT.
CREATE FUNCTION fn_customer_balance(p_customer_id BIGINT UNSIGNED)
RETURNS DECIMAL(14,2)
DETERMINISTIC
READS SQL DATA
BEGIN
  DECLARE v_orders   DECIMAL(14,2);
  DECLARE v_payments DECIMAL(14,2);

  SELECT IFNULL(SUM(order_total), 0) INTO v_orders
  FROM customer_order
  WHERE customer_id = p_customer_id
    AND status NOT IN ('cancelled', 'refunded');

  SELECT IFNULL(SUM(p.amount), 0) INTO v_payments
  FROM payment p
  JOIN customer_order o ON o.order_id = p.order_id
  WHERE o.customer_id = p_customer_id;

  RETURN v_orders - v_payments;
END$$

-- Model-tier: GROUP_CONCAT with ORDER BY and SEPARATOR has no one-to-one
-- PL/pgSQL form -- string_agg takes its ordering differently.
CREATE FUNCTION fn_order_sku_list(p_order_id BIGINT UNSIGNED)
RETURNS TEXT
DETERMINISTIC
READS SQL DATA
BEGIN
  DECLARE v_list TEXT;
  SELECT GROUP_CONCAT(p.sku ORDER BY l.line_no SEPARATOR ', ')
    INTO v_list
  FROM order_line l
  JOIN product p ON p.product_id = l.product_id
  WHERE l.order_id = p_order_id;
  RETURN IFNULL(v_list, '');
END$$

-- Model-tier: SIGNAL SQLSTATE, a CONTINUE HANDLER, and ON DUPLICATE KEY UPDATE.
CREATE PROCEDURE sp_place_order(
  IN  p_customer_ref VARCHAR(20),
  IN  p_product_sku  VARCHAR(40),
  IN  p_quantity     INT UNSIGNED,
  OUT p_order_id     BIGINT UNSIGNED
)
MODIFIES SQL DATA
BEGIN
  DECLARE v_customer_id BIGINT UNSIGNED;
  DECLARE v_product_id  BIGINT UNSIGNED;
  DECLARE v_price       DECIMAL(12,4);
  DECLARE v_ref         VARCHAR(30);
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  IF p_quantity IS NULL OR p_quantity = 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'quantity must be greater than zero';
  END IF;

  SELECT customer_id INTO v_customer_id
  FROM customer WHERE customer_ref = p_customer_ref;
  IF v_customer_id IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'no such customer';
  END IF;

  SELECT product_id, unit_price INTO v_product_id, v_price
  FROM product WHERE sku = p_product_sku AND status = 'active';
  IF v_product_id IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'no such active product';
  END IF;

  START TRANSACTION;
  SET v_ref = CONCAT('ORD-', DATE_FORMAT(NOW(), '%Y%m%d'), '-',
                     LPAD(FLOOR(RAND() * 1000000), 6, '0'));
  INSERT INTO customer_order (order_ref, customer_id, status, order_total)
  VALUES (v_ref, v_customer_id, 'new', v_price * p_quantity);
  SET p_order_id = LAST_INSERT_ID();

  INSERT INTO order_line (order_id, line_no, product_id, quantity, unit_price)
  VALUES (p_order_id, 1, v_product_id, p_quantity, v_price);

  -- ON DUPLICATE KEY UPDATE -> ON CONFLICT ... DO UPDATE on PostgreSQL.
  INSERT INTO product_daily_sales (sale_date, product_id, units, revenue)
  VALUES (CURRENT_DATE, v_product_id, p_quantity, v_price * p_quantity)
  ON DUPLICATE KEY UPDATE
    units   = units   + VALUES(units),
    revenue = revenue + VALUES(revenue);

  COMMIT;
END$$

-- Model-tier: a cursor with a NOT FOUND handler and LIMIT n, m paging.
CREATE PROCEDURE sp_reprice_category(
  IN p_category_code VARCHAR(20),
  IN p_factor        DECIMAL(6,4)
)
MODIFIES SQL DATA
BEGIN
  DECLARE v_done       INT DEFAULT 0;
  DECLARE v_product_id BIGINT UNSIGNED;
  DECLARE v_price      DECIMAL(12,4);
  DECLARE cur CURSOR FOR
    SELECT p.product_id, p.unit_price
    FROM product p
    JOIN product_category c ON c.category_id = p.category_id
    WHERE c.category_code = p_category_code
    ORDER BY p.product_id
    LIMIT 0, 1000;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;

  IF p_factor IS NULL OR p_factor <= 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'factor must be positive';
  END IF;

  OPEN cur;
  reprice: LOOP
    FETCH cur INTO v_product_id, v_price;
    IF v_done = 1 THEN
      LEAVE reprice;
    END IF;
    UPDATE product
       SET unit_price = ROUND(v_price * p_factor, 4)
     WHERE product_id = v_product_id;
  END LOOP reprice;
  CLOSE cur;
END$$

-- ---------------------------------------------------------------------------
-- Triggers. The audit trigger is the one that matters for conversion: a MySQL
-- BEFORE trigger assigns to NEW.col, while a PostgreSQL BEFORE trigger must
-- also RETURN NEW or the row change is silently cancelled. `convert/rules.py`
-- already encodes that trap for Oracle and it applies identically here.
-- ---------------------------------------------------------------------------
CREATE TRIGGER trg_order_audit_upd
AFTER UPDATE ON customer_order
FOR EACH ROW
BEGIN
  IF OLD.status <> NEW.status THEN
    INSERT INTO order_audit (event_year, order_id, event_type,
                             old_status, new_status, changed_by)
    VALUES (YEAR(NOW()), NEW.order_id, 'STATUS_CHANGE',
            OLD.status, NEW.status, CURRENT_USER());
  END IF;
END$$

CREATE TRIGGER trg_order_total_ins
BEFORE INSERT ON customer_order
FOR EACH ROW
BEGIN
  IF NEW.order_total IS NULL THEN
    SET NEW.order_total = 0.00;
  END IF;
  IF NEW.currency IS NULL OR NEW.currency = '' THEN
    SET NEW.currency = 'GBP';
  END IF;
END$$

DELIMITER ;

-- ---------------------------------------------------------------------------
-- The aggregate sp_place_order writes to. Created after the routine references
-- it because MySQL does not resolve table names at CREATE PROCEDURE time --
-- which is itself a difference worth knowing: PostgreSQL does not either for
-- dynamic SQL, but it does for static, so a converted routine can fail to
-- create where the original did not.
-- ---------------------------------------------------------------------------
CREATE TABLE product_daily_sales (
  sale_date   DATE            NOT NULL,
  product_id  BIGINT UNSIGNED NOT NULL,
  units       INT UNSIGNED    NOT NULL DEFAULT 0,
  revenue     DECIMAL(14,4)   NOT NULL DEFAULT 0.0000,
  PRIMARY KEY (sale_date, product_id),
  KEY ix_daily_product (product_id),
  CONSTRAINT fk_daily_product FOREIGN KEY (product_id)
    REFERENCES product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- A scheduled event. RDS manages the event scheduler differently from a
-- self-managed server (event_scheduler is a parameter-group setting), so an
-- estate relying on one has a migration step that is easy to miss.
-- ---------------------------------------------------------------------------
DELIMITER $$
CREATE EVENT ev_purge_old_audit
ON SCHEDULE EVERY 1 DAY
STARTS (CURRENT_DATE + INTERVAL 1 DAY)
DO
BEGIN
  DELETE FROM order_audit
  WHERE changed_at < (NOW() - INTERVAL 3 YEAR);
END$$
DELIMITER ;

-- ---------------------------------------------------------------------------
-- The reporting schema, so cross-schema references exist. On MySQL a schema is
-- a database, so a cross-schema FK is a genuinely different thing from Oracle's
-- cross-schema reference -- worth having in the estate rather than assuming.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS dbmig_mysql_rpt.revenue_snapshot;
CREATE TABLE dbmig_mysql_rpt.revenue_snapshot (
  snapshot_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  snapshot_date DATE            NOT NULL,
  category_code VARCHAR(20)     NOT NULL,
  order_count   INT UNSIGNED    NOT NULL DEFAULT 0,
  revenue       DECIMAL(16,2)   NOT NULL DEFAULT 0.00,
  PRIMARY KEY (snapshot_id),
  UNIQUE KEY uq_snapshot (snapshot_date, category_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- Report what landed. Counted, not assumed.
-- ---------------------------------------------------------------------------
SELECT 'tables'   AS object_type, COUNT(*) AS n
FROM information_schema.tables
WHERE table_schema = 'dbmig_mysql_app' AND table_type = 'BASE TABLE'
UNION ALL
SELECT 'views',    COUNT(*) FROM information_schema.views
WHERE table_schema = 'dbmig_mysql_app'
UNION ALL
SELECT 'routines', COUNT(*) FROM information_schema.routines
WHERE routine_schema = 'dbmig_mysql_app'
UNION ALL
SELECT 'triggers', COUNT(*) FROM information_schema.triggers
WHERE trigger_schema = 'dbmig_mysql_app'
UNION ALL
SELECT 'events',   COUNT(*) FROM information_schema.events
WHERE event_schema = 'dbmig_mysql_app'
UNION ALL
SELECT 'partitions', COUNT(*) FROM information_schema.partitions
WHERE table_schema = 'dbmig_mysql_app' AND partition_name IS NOT NULL;
