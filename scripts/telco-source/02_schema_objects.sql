-- ============================================================================
-- 02_schema_objects.sql  --  TELCO source estate
-- Run connected AS dbmig_telco @ XEPDB1. F5 (Run Script), nothing highlighted.
--
-- Deliberately spans the same breadth of object types as the DBMIG_APP estate
-- so every collector probe has something to find here too: partitioned tables,
-- LOBs, object types, IOT, MV + MV log, PL/SQL, trigger, sequence, synonym,
-- view, function-based index.
--
-- Everything lands in DBMIG_TELCO_TS, never USERS.
-- ============================================================================

SET SERVEROUTPUT ON
SET DEFINE OFF
WHENEVER SQLERROR CONTINUE

-- ----------------------------------------------------------------------------
-- 1. SEQUENCES
-- ----------------------------------------------------------------------------
CREATE SEQUENCE seq_subscriber_id START WITH 1 INCREMENT BY 1 CACHE 100;
CREATE SEQUENCE seq_device_id     START WITH 1 INCREMENT BY 1 CACHE 100;
CREATE SEQUENCE seq_cdr_id        START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE seq_invoice_id    START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE seq_ticket_id     START WITH 1 INCREMENT BY 1 CACHE 100;

-- ----------------------------------------------------------------------------
-- 2. OBJECT TYPES
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TYPE ty_service_addr AS OBJECT (
  line1       VARCHAR2(100),
  line2       VARCHAR2(100),
  city        VARCHAR2(60),
  region      VARCHAR2(60),
  postal_code VARCHAR2(20),
  country     VARCHAR2(60)
);
/

CREATE OR REPLACE TYPE ty_msisdn_list AS VARRAY(4) OF VARCHAR2(20);
/

-- ----------------------------------------------------------------------------
-- 3. PLAN_CATALOG  -- small dimension, index-organized table (IOT)
-- ----------------------------------------------------------------------------
CREATE TABLE plan_catalog (
  plan_code       VARCHAR2(20)  NOT NULL,
  plan_name       VARCHAR2(80)  NOT NULL,
  monthly_rental  NUMBER(10,2)  NOT NULL,
  data_quota_gb   NUMBER(6,2),
  voice_mins      NUMBER(8),
  plan_type       VARCHAR2(20)  DEFAULT 'POSTPAID' NOT NULL,
  active_flag     CHAR(1)       DEFAULT 'Y' NOT NULL,
  CONSTRAINT pk_plan_catalog PRIMARY KEY (plan_code),
  CONSTRAINT ck_plan_type    CHECK (plan_type IN ('POSTPAID','PREPAID','ENTERPRISE')),
  CONSTRAINT ck_plan_active  CHECK (active_flag IN ('Y','N'))
) ORGANIZATION INDEX TABLESPACE dbmig_telco_ts;

-- ----------------------------------------------------------------------------
-- 4. SUBSCRIBER  -- object column + varray, the customer dimension
-- ----------------------------------------------------------------------------
CREATE TABLE subscriber (
  subscriber_id   NUMBER(12)    DEFAULT seq_subscriber_id.NEXTVAL NOT NULL,
  account_no      VARCHAR2(24)  NOT NULL,
  full_name       VARCHAR2(120) NOT NULL,
  email           VARCHAR2(150),
  national_id     VARCHAR2(30),
  plan_code       VARCHAR2(20)  NOT NULL,
  sub_status      VARCHAR2(20)  DEFAULT 'ACTIVE' NOT NULL,
  activated_on    DATE          DEFAULT SYSDATE NOT NULL,
  churn_date      DATE,
  credit_limit    NUMBER(12,2),
  service_addr    ty_service_addr,
  msisdns         ty_msisdn_list,
  CONSTRAINT pk_subscriber      PRIMARY KEY (subscriber_id),
  CONSTRAINT uq_subscriber_acct UNIQUE (account_no),
  CONSTRAINT ck_sub_status      CHECK (sub_status IN ('ACTIVE','SUSPENDED','CHURNED','BARRED')),
  CONSTRAINT fk_sub_plan        FOREIGN KEY (plan_code) REFERENCES plan_catalog (plan_code)
) TABLESPACE dbmig_telco_ts;

-- ----------------------------------------------------------------------------
-- 5. DEVICE  -- SIM / handset per subscriber
-- ----------------------------------------------------------------------------
CREATE TABLE device (
  device_id       NUMBER(12)    DEFAULT seq_device_id.NEXTVAL NOT NULL,
  subscriber_id   NUMBER(12)    NOT NULL,
  imei            VARCHAR2(20)  NOT NULL,
  sim_serial      VARCHAR2(24)  NOT NULL,
  make            VARCHAR2(40),
  model           VARCHAR2(60),
  activated_on    DATE          DEFAULT SYSDATE NOT NULL,
  device_status   VARCHAR2(16)  DEFAULT 'IN_USE' NOT NULL,
  CONSTRAINT pk_device      PRIMARY KEY (device_id),
  CONSTRAINT fk_device_sub  FOREIGN KEY (subscriber_id) REFERENCES subscriber (subscriber_id),
  CONSTRAINT ck_device_stat CHECK (device_status IN ('IN_USE','SPARE','LOST','RETIRED'))
) TABLESPACE dbmig_telco_ts;

CREATE INDEX ix_device_sub  ON device (subscriber_id) TABLESPACE dbmig_telco_ts;
CREATE INDEX ix_device_imei ON device (imei)          TABLESPACE dbmig_telco_ts;

-- ----------------------------------------------------------------------------
-- 6. CDR  -- the volume table. Range-partitioned by call date.
--    This is the bulk of the 5 GB and the table that makes partition,
--    storage and sizing probes meaningful.
-- ----------------------------------------------------------------------------
CREATE TABLE cdr (
  cdr_id          NUMBER(14)    NOT NULL,
  subscriber_id   NUMBER(12)    NOT NULL,
  call_date       DATE          NOT NULL,
  called_number   VARCHAR2(24),
  call_type       VARCHAR2(12)  NOT NULL,
  duration_sec    NUMBER(8),
  bytes_up        NUMBER(12),
  bytes_down      NUMBER(12),
  cell_id         VARCHAR2(16),
  roaming_flag    CHAR(1)       DEFAULT 'N' NOT NULL,
  rated_amount    NUMBER(10,4),
  CONSTRAINT pk_cdr       PRIMARY KEY (cdr_id, call_date),
  CONSTRAINT ck_cdr_type  CHECK (call_type IN ('VOICE','SMS','DATA','MMS','ROAM')),
  CONSTRAINT ck_cdr_roam  CHECK (roaming_flag IN ('Y','N'))
)
TABLESPACE dbmig_telco_ts
PARTITION BY RANGE (call_date) (
  PARTITION p_2025q1 VALUES LESS THAN (DATE '2025-04-01'),
  PARTITION p_2025q2 VALUES LESS THAN (DATE '2025-07-01'),
  PARTITION p_2025q3 VALUES LESS THAN (DATE '2025-10-01'),
  PARTITION p_2025q4 VALUES LESS THAN (DATE '2026-01-01'),
  PARTITION p_2026q1 VALUES LESS THAN (DATE '2026-04-01'),
  PARTITION p_max    VALUES LESS THAN (MAXVALUE)
);

CREATE INDEX ix_cdr_sub_date ON cdr (subscriber_id, call_date)
  LOCAL TABLESPACE dbmig_telco_ts;

-- ----------------------------------------------------------------------------
-- 7. INVOICE / INVOICE_LINE  -- billing fact pair
-- ----------------------------------------------------------------------------
CREATE TABLE invoice (
  invoice_id      NUMBER(12)    DEFAULT seq_invoice_id.NEXTVAL NOT NULL,
  subscriber_id   NUMBER(12)    NOT NULL,
  bill_period     VARCHAR2(7)   NOT NULL,
  issued_on       DATE          NOT NULL,
  due_on          DATE          NOT NULL,
  total_amount    NUMBER(12,2)  NOT NULL,
  tax_amount      NUMBER(10,2),
  invoice_status  VARCHAR2(16)  DEFAULT 'ISSUED' NOT NULL,
  CONSTRAINT pk_invoice      PRIMARY KEY (invoice_id),
  CONSTRAINT fk_invoice_sub  FOREIGN KEY (subscriber_id) REFERENCES subscriber (subscriber_id),
  CONSTRAINT ck_invoice_stat CHECK (invoice_status IN ('ISSUED','PAID','OVERDUE','VOID','DISPUTED'))
) TABLESPACE dbmig_telco_ts;

CREATE INDEX ix_invoice_sub    ON invoice (subscriber_id)          TABLESPACE dbmig_telco_ts;
CREATE INDEX ix_invoice_period ON invoice (bill_period, issued_on) TABLESPACE dbmig_telco_ts;

CREATE TABLE invoice_line (
  line_id         NUMBER(14)    NOT NULL,
  invoice_id      NUMBER(12)    NOT NULL,
  line_no         NUMBER(4)     NOT NULL,
  charge_type     VARCHAR2(20)  NOT NULL,
  description     VARCHAR2(200),
  quantity        NUMBER(10,3),
  unit_price      NUMBER(10,4),
  line_amount     NUMBER(12,2)  NOT NULL,
  CONSTRAINT pk_invoice_line   PRIMARY KEY (line_id),
  CONSTRAINT fk_line_invoice   FOREIGN KEY (invoice_id) REFERENCES invoice (invoice_id),
  CONSTRAINT ck_line_charge    CHECK (charge_type IN ('RENTAL','VOICE','DATA','SMS','ROAMING','VAS','ADJUSTMENT','TAX'))
) TABLESPACE dbmig_telco_ts;

CREATE INDEX ix_line_invoice ON invoice_line (invoice_id) TABLESPACE dbmig_telco_ts;

-- ----------------------------------------------------------------------------
-- 8. PAYMENT  -- settlement against invoices
-- ----------------------------------------------------------------------------
CREATE TABLE payment (
  payment_id      NUMBER(12)    NOT NULL,
  invoice_id      NUMBER(12)    NOT NULL,
  paid_on         DATE          NOT NULL,
  amount_paid     NUMBER(12,2)  NOT NULL,
  pay_method      VARCHAR2(20)  NOT NULL,
  reference_no    VARCHAR2(40),
  CONSTRAINT pk_payment      PRIMARY KEY (payment_id),
  CONSTRAINT fk_pay_invoice  FOREIGN KEY (invoice_id) REFERENCES invoice (invoice_id),
  CONSTRAINT ck_pay_method   CHECK (pay_method IN ('CARD','UPI','NETBANK','CASH','AUTOPAY','WALLET'))
) TABLESPACE dbmig_telco_ts;

CREATE INDEX ix_payment_invoice ON payment (invoice_id) TABLESPACE dbmig_telco_ts;

-- ----------------------------------------------------------------------------
-- 9. SUPPORT_TICKET  -- carries the CLOB. Second-largest contributor to size.
-- ----------------------------------------------------------------------------
CREATE TABLE support_ticket (
  ticket_id       NUMBER(12)    DEFAULT seq_ticket_id.NEXTVAL NOT NULL,
  subscriber_id   NUMBER(12)    NOT NULL,
  opened_on       DATE          NOT NULL,
  closed_on       DATE,
  channel         VARCHAR2(16)  NOT NULL,
  category        VARCHAR2(30),
  priority        VARCHAR2(10)  DEFAULT 'MEDIUM' NOT NULL,
  ticket_status   VARCHAR2(16)  DEFAULT 'OPEN' NOT NULL,
  transcript      CLOB,
  CONSTRAINT pk_ticket       PRIMARY KEY (ticket_id),
  CONSTRAINT fk_ticket_sub   FOREIGN KEY (subscriber_id) REFERENCES subscriber (subscriber_id),
  CONSTRAINT ck_ticket_prio  CHECK (priority IN ('LOW','MEDIUM','HIGH','URGENT')),
  CONSTRAINT ck_ticket_stat  CHECK (ticket_status IN ('OPEN','PENDING','RESOLVED','CLOSED'))
)
TABLESPACE dbmig_telco_ts
LOB (transcript) STORE AS SECUREFILE lob_ticket_transcript (
  TABLESPACE dbmig_telco_ts ENABLE STORAGE IN ROW
);

CREATE INDEX ix_ticket_sub ON support_ticket (subscriber_id) TABLESPACE dbmig_telco_ts;

-- ----------------------------------------------------------------------------
-- 10. NETWORK_CELL  -- small reference table, function-based index
-- ----------------------------------------------------------------------------
CREATE TABLE network_cell (
  cell_id         VARCHAR2(16)  NOT NULL,
  site_name       VARCHAR2(80),
  region          VARCHAR2(60),
  technology      VARCHAR2(10),
  latitude        NUMBER(9,6),
  longitude       NUMBER(9,6),
  commissioned_on DATE,
  CONSTRAINT pk_network_cell PRIMARY KEY (cell_id),
  CONSTRAINT ck_cell_tech    CHECK (technology IN ('2G','3G','4G','5G'))
) TABLESPACE dbmig_telco_ts;

CREATE INDEX ix_cell_region_upper ON network_cell (UPPER(region)) TABLESPACE dbmig_telco_ts;

-- ----------------------------------------------------------------------------
-- 11. VIEWS
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_active_subscriber AS
SELECT s.subscriber_id, s.account_no, s.full_name, s.plan_code,
       p.plan_name, p.monthly_rental, s.activated_on
FROM   subscriber s JOIN plan_catalog p ON p.plan_code = s.plan_code
WHERE  s.sub_status = 'ACTIVE';

CREATE OR REPLACE VIEW vw_invoice_balance AS
SELECT i.invoice_id, i.subscriber_id, i.bill_period, i.total_amount,
       NVL(SUM(pay.amount_paid), 0)                  AS paid_amount,
       i.total_amount - NVL(SUM(pay.amount_paid), 0) AS balance_due
FROM   invoice i LEFT JOIN payment pay ON pay.invoice_id = i.invoice_id
GROUP  BY i.invoice_id, i.subscriber_id, i.bill_period, i.total_amount;

-- ----------------------------------------------------------------------------
-- 12. MATERIALIZED VIEW + MV LOG
--     BUILD DEFERRED: invoice is empty right now. 03_generate_data.sql
--     refreshes it after the load, which is also when it is worth building.
-- ----------------------------------------------------------------------------
-- TABLESPACE must precede WITH in CREATE MATERIALIZED VIEW LOG. Putting it
-- after INCLUDING NEW VALUES -- where CREATE TABLE accepts it -- raises
-- ORA-00933.
CREATE MATERIALIZED VIEW LOG ON invoice
  TABLESPACE dbmig_telco_ts
  WITH ROWID, SEQUENCE (subscriber_id, bill_period, total_amount)
  INCLUDING NEW VALUES;

CREATE MATERIALIZED VIEW mv_revenue_by_period
  TABLESPACE dbmig_telco_ts
  BUILD DEFERRED
  REFRESH COMPLETE ON DEMAND
AS
SELECT bill_period,
       COUNT(*)          AS invoice_count,
       SUM(total_amount) AS revenue,
       AVG(total_amount) AS avg_invoice
FROM   invoice
GROUP  BY bill_period;

-- ----------------------------------------------------------------------------
-- 13. PL/SQL -- procedure, function, package, trigger
-- ----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_bar_subscriber (p_subscriber_id IN NUMBER) AS
BEGIN
  UPDATE subscriber SET sub_status = 'BARRED' WHERE subscriber_id = p_subscriber_id;
  UPDATE device SET device_status = 'RETIRED'
   WHERE subscriber_id = p_subscriber_id AND device_status = 'IN_USE';
  COMMIT;
END;
/

CREATE OR REPLACE FUNCTION fn_subscriber_balance (p_subscriber_id IN NUMBER)
RETURN NUMBER AS
  v_bal NUMBER;
BEGIN
  SELECT NVL(SUM(i.total_amount), 0) - NVL(SUM(p.amount_paid), 0)
    INTO v_bal
    FROM invoice i LEFT JOIN payment p ON p.invoice_id = i.invoice_id
   WHERE i.subscriber_id = p_subscriber_id;
  RETURN v_bal;
END;
/

CREATE OR REPLACE PACKAGE pkg_billing_ops AS
  PROCEDURE close_period (p_period IN VARCHAR2);
  FUNCTION  period_revenue (p_period IN VARCHAR2) RETURN NUMBER;
END pkg_billing_ops;
/

CREATE OR REPLACE PACKAGE BODY pkg_billing_ops AS
  PROCEDURE close_period (p_period IN VARCHAR2) AS
  BEGIN
    UPDATE invoice SET invoice_status = 'OVERDUE'
     WHERE bill_period = p_period AND invoice_status = 'ISSUED' AND due_on < SYSDATE;
    COMMIT;
  END close_period;

  FUNCTION period_revenue (p_period IN VARCHAR2) RETURN NUMBER AS
    v_rev NUMBER;
  BEGIN
    SELECT NVL(SUM(total_amount), 0) INTO v_rev FROM invoice WHERE bill_period = p_period;
    RETURN v_rev;
  END period_revenue;
END pkg_billing_ops;
/

CREATE OR REPLACE TRIGGER trg_invoice_audit
BEFORE UPDATE OF invoice_status ON invoice
FOR EACH ROW
BEGIN
  IF :NEW.invoice_status = 'PAID' AND :OLD.invoice_status <> 'PAID' THEN
    :NEW.due_on := NVL(:OLD.due_on, SYSDATE);
  END IF;
END;
/

-- ----------------------------------------------------------------------------
-- 14. SYNONYM
-- ----------------------------------------------------------------------------
CREATE OR REPLACE SYNONYM syn_active_sub FOR vw_active_subscriber;

-- ----------------------------------------------------------------------------
-- 15. Per-table SELECT to the collector (least privilege -- see 01_setup_admin)
-- ----------------------------------------------------------------------------
GRANT SELECT ON plan_catalog         TO dbmig_collector;
GRANT SELECT ON subscriber           TO dbmig_collector;
GRANT SELECT ON device               TO dbmig_collector;
GRANT SELECT ON cdr                  TO dbmig_collector;
GRANT SELECT ON invoice              TO dbmig_collector;
GRANT SELECT ON invoice_line         TO dbmig_collector;
GRANT SELECT ON payment              TO dbmig_collector;
GRANT SELECT ON support_ticket       TO dbmig_collector;
GRANT SELECT ON network_cell         TO dbmig_collector;
GRANT SELECT ON vw_active_subscriber TO dbmig_collector;
GRANT SELECT ON vw_invoice_balance   TO dbmig_collector;

-- ----------------------------------------------------------------------------
-- 16. Verify -- expect zero INVALID objects at this point
-- ----------------------------------------------------------------------------
SELECT object_type, COUNT(*) AS n FROM user_objects GROUP BY object_type ORDER BY 1;
SELECT COUNT(*) AS total_objects     FROM user_objects;
SELECT COUNT(*) AS total_constraints FROM user_constraints;
SELECT object_name, object_type, status FROM user_objects WHERE status = 'INVALID';

PROMPT ============================================================
PROMPT 02_schema_objects complete. INVALID list above must be EMPTY
PROMPT (the one deliberate invalid object is seeded in script 04).
PROMPT Next: 03_generate_data.sql
PROMPT ============================================================
