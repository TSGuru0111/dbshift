-- ============================================================================
-- DBShift MySQL source estate -- 04: seeded defects
--
--     mysql -u dbmig_app -p dbmig_mysql_app < 04_seed_defects.sql
--     then ALWAYS:  python verify_defects.py
--
-- Twelve defects, each paired with an entry in answer_key.json. This file is the
-- answer key's counterpart: 02 builds what good looks like, this builds what is
-- wrong with it, and nothing else in the estate is broken on purpose.
--
-- WHY THE VERIFY STEP IS NOT OPTIONAL. On the Oracle estate, defect 7 was seeded
-- by a script that reported a row modified and changed nothing -- CHR(146) is a
-- bare UTF-8 continuation byte on AL32UTF8 and was dropped during concatenation.
-- Nobody noticed for weeks, and maximum recall silently became 7/8 while the
-- assessment looked like it was under-detecting. See docs/04-defects.md.
--
-- So every defect below is followed by a SELECT that PROVES it landed, and
-- verify_defects.py re-checks all twelve independently. A defect that cannot be
-- proven present is not a defect, it is a belief.
-- ============================================================================

SET NAMES utf8mb4;

-- RE-RUNNABLE. Every defect object is dropped first, because a partial run of
-- this file is a realistic state -- it happened during development, when the
-- foreign key on customer_payment_detail rejected a defect seeded before
-- 03_generate_data.sql had created any customers. A seeder that cannot be
-- re-run leaves the operator choosing between a full reset and hand-editing SQL.
--
-- Order matters: shipment and customer_payment_detail carry foreign keys, so
-- they go before the tables they reference would be touched.
DROP VIEW  IF EXISTS v_customer_pii;
DROP FUNCTION IF EXISTS fn_legacy_tax_rate;
DROP TABLE IF EXISTS shipment;
DROP TABLE IF EXISTS customer_payment_detail;
DROP TABLE IF EXISTS warehouse_stock;
DROP TABLE IF EXISTS supplier;
DROP TABLE IF EXISTS `order`;
DROP TABLE IF EXISTS ledger_entry;
DROP TABLE IF EXISTS contract_term;
DROP TABLE IF EXISTS clickstream_raw;
DROP TABLE IF EXISTS customer_feedback;
DROP TABLE IF EXISTS legacy_import_log;


-- ---------------------------------------------------------------------------
-- DEFECT 1 -- MyISAM table.
-- No transactions, no crash recovery, and DMS CDC cannot replicate it: the
-- binlog records the change but MyISAM offers no transactional boundary to
-- apply it within. This is the single most important MySQL migration finding.
-- ---------------------------------------------------------------------------
CREATE TABLE legacy_import_log (
  log_id      INT UNSIGNED NOT NULL AUTO_INCREMENT,
  imported_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  source_file VARCHAR(255) NOT NULL,
  row_count   INT UNSIGNED NOT NULL DEFAULT 0,
  notes       TEXT             NULL,
  PRIMARY KEY (log_id)
) ENGINE=MyISAM DEFAULT CHARSET=utf8mb4
  COMMENT='DEFECT 1: MyISAM. No transactions, and DMS CDC cannot track it.';

INSERT INTO legacy_import_log (source_file, row_count, notes) VALUES
  ('orders_2023.csv', 15000, 'nightly batch'),
  ('orders_2024.csv', 18500, 'nightly batch'),
  ('refunds_2024.csv', 320, 'manual load');

-- ---------------------------------------------------------------------------
-- DEFECT 2 -- utf8mb3 columns.
-- Legacy `utf8` is a 3-byte encoding and CANNOT store a 4-byte character.
-- Emoji and several CJK ranges are truncated or rejected on insert. The target
-- should be utf8mb4, and PostgreSQL's UTF8 is 4-byte throughout -- so this is a
-- silent data-loss risk that only shows up on the rows that contain one.
-- ---------------------------------------------------------------------------
CREATE TABLE customer_feedback (
  feedback_id  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  customer_id  BIGINT UNSIGNED NOT NULL,
  submitted_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- The defect: utf8mb3, in an otherwise utf8mb4 estate.
  subject      VARCHAR(200) CHARACTER SET utf8mb3 NOT NULL,
  body         TEXT         CHARACTER SET utf8mb3     NULL,
  rating       TINYINT UNSIGNED                       NULL,
  PRIMARY KEY (feedback_id),
  KEY ix_feedback_customer (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='DEFECT 2: utf8mb3 columns cannot hold 4-byte characters.';

-- Attached to real customers rather than a hardcoded id 1: 03 must have run,
-- and picking the lowest existing ids means this survives a reseed that renumbers.
INSERT INTO customer_feedback (customer_id, subject, body, rating)
SELECT customer_id, 'Great service', 'Very happy with the delivery time.', 5
FROM customer ORDER BY customer_id LIMIT 1;
INSERT INTO customer_feedback (customer_id, subject, body, rating)
SELECT customer_id, 'Late delivery', 'Arrived two days after the estimate.', 2
FROM customer ORDER BY customer_id LIMIT 1;

-- ---------------------------------------------------------------------------
-- DEFECT 3 -- no primary key, with real volume.
-- On Oracle this is DQ-001 and matters only for CDC. On MySQL it is worse:
-- InnoDB silently creates a hidden 6-byte clustered index that DMS cannot
-- address, so an UPDATE or DELETE has no reliable way to find its target row.
-- Volume matters -- a 2-row table is a curiosity, 50,000 is a blocker.
-- ---------------------------------------------------------------------------
CREATE TABLE clickstream_raw (
  session_token CHAR(36)     NOT NULL,
  event_at      DATETIME     NOT NULL,
  page_path     VARCHAR(500) NOT NULL,
  referrer      VARCHAR(500)     NULL,
  customer_id   BIGINT UNSIGNED  NULL
  -- deliberately NO PRIMARY KEY, and no unique index either
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='DEFECT 3: no primary key. DMS CDC cannot apply updates or deletes.';

-- 50,000 rows via a recursive CTE, so the volume is real rather than asserted.
SET SESSION cte_max_recursion_depth = 100000;
INSERT INTO clickstream_raw (session_token, event_at, page_path, referrer, customer_id)
WITH RECURSIVE seq(n) AS (
  SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 50000
)
SELECT UUID(),
       NOW() - INTERVAL (n MOD 8760) HOUR,
       CONCAT('/product/', (n MOD 500) + 1),
       CASE WHEN n MOD 7 = 0 THEN 'https://search.example.com' ELSE NULL END,
       CASE WHEN n MOD 11 = 0 THEN 1 ELSE NULL END
FROM seq;

-- ---------------------------------------------------------------------------
-- DEFECT 4 -- zero dates.
-- '0000-00-00' is not a valid date in PostgreSQL and has no representation at
-- all. It only exists here because the server's sql_mode permits it. On
-- migration these arrive NULL, which is data loss -- and unlike most of the
-- differences in this estate it cannot be normalised away.
-- ---------------------------------------------------------------------------
CREATE TABLE contract_term (
  contract_id  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  customer_id  BIGINT UNSIGNED NOT NULL,
  signed_on    DATE            NOT NULL,
  expires_on   DATE            NOT NULL,
  PRIMARY KEY (contract_id),
  KEY ix_contract_customer (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='DEFECT 4: zero dates, which PostgreSQL cannot represent.';

-- Assigning sql_mode outright (rather than appending) is deliberate: it CLEARS
-- NO_ZERO_DATE and STRICT_TRANS_TABLES, which are both in MySQL 8's default mode
-- and both reject '0000-00-00'. Appending ALLOW_INVALID_DATES alone does not
-- work, because NO_ZERO_DATE would still be in force.
--
-- Session-scoped, so the server's own configuration is untouched: the defect is
-- the DATA, not the setting. A reader restoring this estate gets the zero dates
-- without inheriting a permissive server.
SET SESSION sql_mode = 'ALLOW_INVALID_DATES';
INSERT INTO contract_term (customer_id, signed_on, expires_on)
SELECT customer_id, '2024-01-15', '0000-00-00' FROM customer ORDER BY customer_id LIMIT 1;
INSERT INTO contract_term (customer_id, signed_on, expires_on)
SELECT customer_id, '0000-00-00', '2025-12-31' FROM customer ORDER BY customer_id LIMIT 1;
INSERT INTO contract_term (customer_id, signed_on, expires_on)
SELECT customer_id, '2024-06-01', '2026-06-01' FROM customer ORDER BY customer_id LIMIT 1;
SET SESSION sql_mode = DEFAULT;

-- ---------------------------------------------------------------------------
-- DEFECT 5 -- unsigned BIGINT near the signed ceiling.
-- PostgreSQL bigint is SIGNED: its maximum is 9223372036854775807. An unsigned
-- MySQL BIGINT goes to 18446744073709551615, so any value above the signed
-- ceiling has nowhere to land and needs numeric on the target.
--
-- This is the direct analogue of the Oracle FLOAT -> DOUBLE PRECISION
-- truncation that lost 38 significant digits and was caught ONLY by the level-4
-- checksum. Row counts and DMS's own error report both missed it.
-- ---------------------------------------------------------------------------
CREATE TABLE ledger_entry (
  entry_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  -- The defect: values here exceed PostgreSQL's bigint.
  balance_minor BIGINT UNSIGNED NOT NULL,
  recorded_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (entry_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='DEFECT 5: BIGINT UNSIGNED above PostgreSQL bigint max.';

INSERT INTO ledger_entry (balance_minor) VALUES
  (9223372036854775807),   -- exactly the signed maximum: still fits
  (9223372036854775808),   -- one over: does NOT fit in PostgreSQL bigint
  (18446744073709551615),  -- MySQL's unsigned maximum
  (42);                    -- a normal value, so the column is not uniformly odd

-- ---------------------------------------------------------------------------
-- DEFECT 6 -- definer-rights routine naming an account that will not exist.
-- RDS grants no SUPER privilege, so a routine whose DEFINER is root@localhost
-- cannot be created on the target -- and if forced through, fails at RUNTIME
-- rather than at migration time, which is the expensive way to find out.
-- ---------------------------------------------------------------------------
DELIMITER $$
CREATE DEFINER = 'dbmig_app'@'%' FUNCTION fn_legacy_tax_rate(p_country CHAR(2))
RETURNS DECIMAL(6,4)
DETERMINISTIC
SQL SECURITY DEFINER
BEGIN
  -- SQL SECURITY DEFINER is the defect; the body is deliberately trivial so the
  -- finding is about the security context and not about conversion difficulty.
  RETURN CASE p_country
           WHEN 'GB' THEN 0.2000
           WHEN 'IE' THEN 0.2300
           WHEN 'DE' THEN 0.1900
           ELSE 0.0000
         END;
END$$
DELIMITER ;

-- ---------------------------------------------------------------------------
-- DEFECT 7 -- a SQL SECURITY DEFINER view.
-- Same problem, different object type, and easier to miss because a view looks
-- like metadata rather than code.
-- ---------------------------------------------------------------------------
CREATE DEFINER = 'dbmig_app'@'%' SQL SECURITY DEFINER
VIEW v_customer_pii AS
SELECT customer_id, customer_ref, full_name, email, phone, date_of_birth
FROM customer;

-- ---------------------------------------------------------------------------
-- DEFECT 8 -- reserved-word identifiers.
-- `order`, `group` and `key` are reserved in MySQL and must be backtick-quoted
-- forever; on PostgreSQL they need double quotes instead. Any tool, ORM or
-- ad-hoc query that omits the quoting fails at runtime, not at migration time.
-- Deliberately DIFFERENT words from the Oracle estate's ORDER/COMMENT/LEVEL.
-- ---------------------------------------------------------------------------
CREATE TABLE `order` (
  `key`       INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `group`     VARCHAR(50)  NOT NULL,
  `desc`      VARCHAR(200)     NULL,
  `primary`   TINYINT(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='DEFECT 8: reserved words as table and column names.';

INSERT INTO `order` (`group`, `desc`, `primary`) VALUES
  ('alpha', 'first group', 1),
  ('beta',  'second group', 0);

-- ---------------------------------------------------------------------------
-- DEFECT 9 -- duplicate values in a should-be-unique column.
-- No unique constraint, and the data proves why one is needed. On a target with
-- a unique index added during migration, the load fails partway through.
-- ---------------------------------------------------------------------------
CREATE TABLE supplier (
  supplier_id  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  -- Should be UNIQUE and is not, and the data has duplicates.
  tax_ref      VARCHAR(30)  NOT NULL,
  supplier_name VARCHAR(200) NOT NULL,
  PRIMARY KEY (supplier_id),
  KEY ix_supplier_tax (tax_ref)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='DEFECT 9: tax_ref should be unique; duplicates present.';

INSERT INTO supplier (tax_ref, supplier_name) VALUES
  ('GB123456789', 'Acme Supplies Ltd'),
  ('GB123456789', 'ACME SUPPLIES LIMITED'),   -- duplicate tax_ref
  ('GB987654321', 'Beta Trading'),
  ('gb123456789', 'Acme Supplies (dup case)'); -- duplicate under a _ci collation

-- ---------------------------------------------------------------------------
-- DEFECT 10 -- an orphaned reference with no foreign key to enforce it.
-- The column names a parent that does not exist. Adding the FK on the target
-- fails; not adding it carries the corruption forward.
-- ---------------------------------------------------------------------------
CREATE TABLE warehouse_stock (
  stock_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  -- No FOREIGN KEY, deliberately, and row 3 points at a product that is gone.
  product_id   BIGINT UNSIGNED NOT NULL,
  warehouse_code VARCHAR(10)   NOT NULL,
  quantity     INT             NOT NULL DEFAULT 0,
  PRIMARY KEY (stock_id),
  KEY ix_stock_product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='DEFECT 10: orphaned product_id, no FK to catch it.';

INSERT INTO warehouse_stock (product_id, warehouse_code, quantity)
SELECT product_id, 'LON-01', 500 FROM product ORDER BY product_id LIMIT 1;
INSERT INTO warehouse_stock (product_id, warehouse_code, quantity)
SELECT product_id, 'MAN-02', 250 FROM product ORDER BY product_id LIMIT 1;
-- The defect: a product_id that does not exist, and no FK to reject it.
INSERT INTO warehouse_stock (product_id, warehouse_code, quantity)
VALUES (999999, 'LON-01', 75);

-- ---------------------------------------------------------------------------
-- DEFECT 11 -- redundant index on the same leading column.
-- ix_dup_a is a prefix of ix_dup_ab, so it earns nothing and costs write time
-- and storage on every insert. Carried to the target unless someone looks.
-- ---------------------------------------------------------------------------
CREATE TABLE shipment (
  shipment_id  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id     BIGINT UNSIGNED NOT NULL,
  carrier_code VARCHAR(20)     NOT NULL,
  shipped_at   DATETIME            NULL,
  PRIMARY KEY (shipment_id),
  KEY ix_dup_a  (order_id),                 -- redundant
  KEY ix_dup_ab (order_id, carrier_code),   -- ...because of this one
  CONSTRAINT fk_shipment_order FOREIGN KEY (order_id)
    REFERENCES customer_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='DEFECT 11: ix_dup_a is a redundant prefix of ix_dup_ab.';

-- ---------------------------------------------------------------------------
-- DEFECT 12 -- sensitive columns in clear text.
-- Bank details and a national identifier with no encryption, no masking and a
-- plain SELECT grant. A migration is the moment this gets noticed, and moving
-- it unchanged to a cloud target is a decision someone should make knowingly.
-- ---------------------------------------------------------------------------
CREATE TABLE customer_payment_detail (
  detail_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  customer_id     BIGINT UNSIGNED NOT NULL,
  bank_account_no VARCHAR(34)     NOT NULL,   -- IBAN, clear text
  sort_code       VARCHAR(10)     NOT NULL,
  national_id     VARCHAR(20)         NULL,
  card_last_four  CHAR(4)             NULL,
  PRIMARY KEY (detail_id),
  KEY ix_paydetail_customer (customer_id),
  CONSTRAINT fk_paydetail_customer FOREIGN KEY (customer_id)
    REFERENCES customer (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='DEFECT 12: bank_account_no and national_id in clear text.';

INSERT INTO customer_payment_detail
  (customer_id, bank_account_no, sort_code, national_id, card_last_four)
SELECT customer_id, 'GB29NWBK60161331926819', '60-16-13', 'QQ123456C', '4242'
FROM customer ORDER BY customer_id LIMIT 1;

-- ============================================================================
-- PROOF. Every defect, counted rather than assumed.
--
-- A zero in the `found` column means that defect is NOT in the database,
-- whatever this script appeared to do. verify_defects.py checks the same twelve
-- independently and is the gate -- do not skip it.
-- ============================================================================
SELECT 1 AS defect, 'MyISAM table' AS title,
       (SELECT COUNT(*) FROM information_schema.tables
         WHERE table_schema = DATABASE() AND engine = 'MyISAM') AS found, 1 AS expected
UNION ALL SELECT 2, 'utf8mb3 columns',
       (SELECT COUNT(*) FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND character_set_name IN ('utf8', 'utf8mb3')), 2
UNION ALL SELECT 3, 'table with no primary key',
       (SELECT COUNT(*) FROM information_schema.tables t
         WHERE t.table_schema = DATABASE() AND t.table_type = 'BASE TABLE'
           AND NOT EXISTS (SELECT 1 FROM information_schema.table_constraints tc
                            WHERE tc.table_schema = t.table_schema
                              AND tc.table_name = t.table_name
                              AND tc.constraint_type = 'PRIMARY KEY')), 1
UNION ALL SELECT 4, 'zero dates',
       -- Compared as TEXT, not as a DATE literal. Under the default sql_mode
       -- (NO_ZERO_DATE) the literal '0000-00-00' in a WHERE clause is itself
       -- rejected with ERROR 1525 -- even though the stored rows are fine. The
       -- first version of this check failed for that reason and looked exactly
       -- like a seeding failure, which is the thing this section exists to rule
       -- out. CAST to CHAR and the comparison is about the data again.
       (SELECT COUNT(*) FROM contract_term
         WHERE CAST(signed_on AS CHAR) = '0000-00-00'
            OR CAST(expires_on AS CHAR) = '0000-00-00'), 2
UNION ALL SELECT 5, 'BIGINT UNSIGNED over signed max',
       (SELECT COUNT(*) FROM ledger_entry
         WHERE balance_minor > 9223372036854775807), 2
UNION ALL SELECT 6, 'definer-rights routine',
       (SELECT COUNT(*) FROM information_schema.routines
         WHERE routine_schema = DATABASE() AND security_type = 'DEFINER'
           AND routine_name = 'fn_legacy_tax_rate'), 1
UNION ALL SELECT 7, 'definer-rights view',
       -- Named explicitly. SQL SECURITY DEFINER is MySQL's DEFAULT for a view,
       -- so the two clean views from 02 are DEFINER too and counting them all
       -- returned 3 for a defect seeded once. That is worth knowing beyond this
       -- check: on a real estate, "how many DEFINER views" is not the finding --
       -- "whose definer will not exist on the target" is.
       (SELECT COUNT(*) FROM information_schema.views
         WHERE table_schema = DATABASE() AND security_type = 'DEFINER'
           AND table_name = 'v_customer_pii'), 1
UNION ALL SELECT 8, 'reserved-word identifiers',
       (SELECT COUNT(*) FROM information_schema.tables
         WHERE table_schema = DATABASE() AND table_name = 'order'), 1
UNION ALL SELECT 9, 'duplicate should-be-unique values',
       (SELECT COUNT(*) FROM (SELECT tax_ref FROM supplier
                               GROUP BY tax_ref HAVING COUNT(*) > 1) d), 1
UNION ALL SELECT 10, 'orphaned reference',
       (SELECT COUNT(*) FROM warehouse_stock w
         WHERE NOT EXISTS (SELECT 1 FROM product p
                            WHERE p.product_id = w.product_id)), 1
UNION ALL SELECT 11, 'redundant index',
       (SELECT COUNT(*) FROM information_schema.statistics
         WHERE table_schema = DATABASE() AND index_name = 'ix_dup_a'), 1
UNION ALL SELECT 12, 'clear-text sensitive columns',
       (SELECT COUNT(*) FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND column_name IN ('bank_account_no', 'national_id')), 2
ORDER BY defect;
