-- ============================================================================
-- 02_schema_objects.sql
-- Oracle Database 21c XE — run this file connected AS dbmig_app
-- (In SQL Developer: create a new connection for dbmig_app, open this file
--  in that connection's worksheet, then Run Script / F5)
--
-- Covers every object type on the checklist except the pure admin objects
-- already created in 01_setup_admin.sql (tablespace, user, role, profile,
-- directory, system privileges) and the DB link (created here, by dbmig_rpt
-- separately — see section 15).
-- ============================================================================

SET SERVEROUTPUT ON
SET DEFINE OFF
WHENEVER SQLERROR CONTINUE

-- ----------------------------------------------------------------------------
-- 1. SEQUENCE
-- ----------------------------------------------------------------------------
CREATE SEQUENCE seq_customer_id START WITH 1 INCREMENT BY 1 CACHE 100;
CREATE SEQUENCE seq_loan_id      START WITH 1 INCREMENT BY 1 CACHE 100;
CREATE SEQUENCE seq_txn_id       START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE seq_payment_id   START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE seq_comm_id      START WITH 1 INCREMENT BY 1 CACHE 1000;

-- ----------------------------------------------------------------------------
-- 2. TYPE  ("Type", and "Domain" approximated — Oracle has no native DOMAIN,
--    so a domain is modeled as a subtype-style object with a CHECK constraint
--    wherever it's used, noted inline below)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TYPE ty_address AS OBJECT (
  line1      VARCHAR2(100),
  line2      VARCHAR2(100),
  city       VARCHAR2(60),
  state      VARCHAR2(60),
  postal_code VARCHAR2(20),
  country    VARCHAR2(60)
);
/

CREATE OR REPLACE TYPE ty_phone_list AS VARRAY(3) OF VARCHAR2(20);
/

-- ----------------------------------------------------------------------------
-- 3. TABLE + PRIMARY KEY + UNIQUE CONSTRAINT + CHECK CONSTRAINT
--    + DEFAULT CONSTRAINT + "Domain"-style check
-- ----------------------------------------------------------------------------
CREATE TABLE customer (
  customer_id     NUMBER(10)      DEFAULT seq_customer_id.NEXTVAL NOT NULL,
  full_name       VARCHAR2(120)   NOT NULL,
  email           VARCHAR2(150),
  national_id     VARCHAR2(30),
  status          VARCHAR2(20)    DEFAULT 'ACTIVE' NOT NULL,   -- default constraint
  address         ty_address,
  phone_numbers   ty_phone_list,
  created_at      DATE            DEFAULT SYSDATE NOT NULL,
  CONSTRAINT pk_customer            PRIMARY KEY (customer_id),
  CONSTRAINT uq_customer_email      UNIQUE (email),                 -- unique constraint
  CONSTRAINT ck_customer_status     CHECK (status IN ('ACTIVE','SUSPENDED','CLOSED'))  -- check / "domain"
) TABLESPACE users;

CREATE TABLE loan (
  loan_id         NUMBER(10)      DEFAULT seq_loan_id.NEXTVAL NOT NULL,
  customer_id     NUMBER(10)      NOT NULL,
  principal_amt   NUMBER(14,2)    NOT NULL,
  interest_rate   NUMBER(5,2)     DEFAULT 12.50 NOT NULL,           -- default constraint
  loan_status     VARCHAR2(20)    DEFAULT 'OPEN' NOT NULL,
  disbursed_on    DATE            DEFAULT SYSDATE,
  CONSTRAINT pk_loan               PRIMARY KEY (loan_id),
  CONSTRAINT fk_loan_customer      FOREIGN KEY (customer_id) REFERENCES customer(customer_id),  -- foreign key
  CONSTRAINT ck_loan_status        CHECK (loan_status IN ('OPEN','CLOSED','WRITTEN_OFF')),
  CONSTRAINT ck_loan_principal_pos CHECK (principal_amt > 0)
) TABLESPACE users;

-- PARTITIONed table (range partition by month — "Partition")
CREATE TABLE loan_txn (
  txn_id          NUMBER(14)      DEFAULT seq_txn_id.NEXTVAL NOT NULL,
  loan_id         NUMBER(10)      NOT NULL,
  txn_date        DATE            DEFAULT SYSDATE NOT NULL,
  txn_amount      NUMBER(14,2)    NOT NULL,
  txn_type        VARCHAR2(20)    DEFAULT 'DEBIT' NOT NULL,
  CONSTRAINT pk_loan_txn          PRIMARY KEY (txn_id),
  CONSTRAINT fk_txn_loan          FOREIGN KEY (loan_id) REFERENCES loan(loan_id),
  CONSTRAINT ck_txn_type          CHECK (txn_type IN ('DEBIT','CREDIT','FEE','REVERSAL'))
)
TABLESPACE users
PARTITION BY RANGE (txn_date)
(
  PARTITION p_2025h1 VALUES LESS THAN (DATE '2025-07-01'),
  PARTITION p_2025h2 VALUES LESS THAN (DATE '2026-01-01'),
  PARTITION p_2026h1 VALUES LESS THAN (DATE '2026-07-01'),
  PARTITION p_future  VALUES LESS THAN (MAXVALUE)
);

CREATE TABLE payment_hist (
  payment_id      NUMBER(14)      DEFAULT seq_payment_id.NEXTVAL NOT NULL,
  loan_id         NUMBER(10)      NOT NULL,
  payment_date    DATE            DEFAULT SYSDATE NOT NULL,
  amount_paid     NUMBER(14,2)    NOT NULL,
  method          VARCHAR2(20)    DEFAULT 'AUTO_DEBIT',
  CONSTRAINT pk_payment_hist      PRIMARY KEY (payment_id),
  CONSTRAINT fk_payment_loan      FOREIGN KEY (loan_id) REFERENCES loan(loan_id)
) TABLESPACE users;

-- Table with a CLOB, and the text column used later for the full-text index
CREATE TABLE comm_log (
  comm_id         NUMBER(14)      DEFAULT seq_comm_id.NEXTVAL NOT NULL,
  customer_id     NUMBER(10)      NOT NULL,
  comm_date       DATE            DEFAULT SYSDATE NOT NULL,
  channel         VARCHAR2(20)    DEFAULT 'EMAIL',
  notes           CLOB,
  CONSTRAINT pk_comm_log          PRIMARY KEY (comm_id),
  CONSTRAINT fk_comm_customer     FOREIGN KEY (customer_id) REFERENCES customer(customer_id)
) TABLESPACE users LOB (notes) STORE AS SECUREFILE (TABLESPACE users);

-- One deliberately unindexed FK, and one table with no PK, matching the
-- "seeded defects" pattern from the assessment engine design.
CREATE TABLE collateral_note (
  note_id         NUMBER(10),
  loan_id         NUMBER(10),               -- FK below, deliberately NOT indexed
  note_text       VARCHAR2(400),
  CONSTRAINT fk_collateral_loan  FOREIGN KEY (loan_id) REFERENCES loan(loan_id)
) TABLESPACE users;   -- <- no primary key, intentional seeded defect

-- ----------------------------------------------------------------------------
-- 4. INDEX  (regular, composite, and function-based)
-- ----------------------------------------------------------------------------
CREATE INDEX ix_loan_customer        ON loan (customer_id);
CREATE INDEX ix_txn_loan_date        ON loan_txn (loan_id, txn_date) LOCAL;  -- local index on the partitioned table
CREATE INDEX ix_payment_loan         ON payment_hist (loan_id);
CREATE INDEX ix_comm_customer_date   ON comm_log (customer_id, comm_date);
CREATE INDEX ix_customer_upper_email ON customer (UPPER(email));            -- function-based index

-- ----------------------------------------------------------------------------
-- 5. MATERIALIZED VIEW LOG  (must precede the fast-refresh MV below)
-- ----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW LOG ON loan
  WITH ROWID, SEQUENCE (customer_id, principal_amt, loan_status)
  INCLUDING NEW VALUES;

-- ----------------------------------------------------------------------------
-- 6. VIEW  and  MATERIALIZED VIEW
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_active_loans AS
  SELECT loan_id, customer_id, principal_amt, interest_rate, disbursed_on
  FROM   loan
  WHERE  loan_status = 'OPEN';

CREATE MATERIALIZED VIEW mv_loan_summary
  BUILD IMMEDIATE
  REFRESH FAST ON DEMAND
AS
  SELECT customer_id,
         COUNT(*)             AS loan_count,
         SUM(principal_amt)   AS total_principal
  FROM   loan
  GROUP  BY customer_id;

-- ----------------------------------------------------------------------------
-- 7. FUNCTION
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_customer_full_name (p_customer_id IN NUMBER)
RETURN VARCHAR2
IS
  v_name customer.full_name%TYPE;
BEGIN
  SELECT full_name INTO v_name FROM customer WHERE customer_id = p_customer_id;
  RETURN v_name;
EXCEPTION
  WHEN NO_DATA_FOUND THEN RETURN NULL;
END fn_customer_full_name;
/

-- ----------------------------------------------------------------------------
-- 8. STORED PROCEDURE
-- ----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_close_loan (p_loan_id IN NUMBER)
IS
BEGIN
  UPDATE loan SET loan_status = 'CLOSED' WHERE loan_id = p_loan_id;
  COMMIT;
END sp_close_loan;
/

-- ----------------------------------------------------------------------------
-- 9. PACKAGE  +  PACKAGE BODY
-- ----------------------------------------------------------------------------
CREATE OR REPLACE PACKAGE pkg_loan_ops AS
  PROCEDURE record_payment (p_loan_id IN NUMBER, p_amount IN NUMBER);
  FUNCTION  outstanding_balance (p_loan_id IN NUMBER) RETURN NUMBER;
END pkg_loan_ops;
/

CREATE OR REPLACE PACKAGE BODY pkg_loan_ops AS

  PROCEDURE record_payment (p_loan_id IN NUMBER, p_amount IN NUMBER) IS
  BEGIN
    INSERT INTO payment_hist (loan_id, payment_date, amount_paid)
    VALUES (p_loan_id, SYSDATE, p_amount);
    COMMIT;
  END record_payment;

  FUNCTION outstanding_balance (p_loan_id IN NUMBER) RETURN NUMBER IS
    v_principal NUMBER;
    v_paid      NUMBER;
  BEGIN
    SELECT principal_amt INTO v_principal FROM loan WHERE loan_id = p_loan_id;
    SELECT NVL(SUM(amount_paid),0) INTO v_paid FROM payment_hist WHERE loan_id = p_loan_id;
    RETURN v_principal - v_paid;
  END outstanding_balance;

END pkg_loan_ops;
/

-- ----------------------------------------------------------------------------
-- 10. TRIGGER
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TRIGGER trg_loan_status_check
BEFORE UPDATE OF loan_status ON loan
FOR EACH ROW
BEGIN
  IF :OLD.loan_status = 'WRITTEN_OFF' AND :NEW.loan_status <> 'WRITTEN_OFF' THEN
    RAISE_APPLICATION_ERROR(-20001, 'Cannot reopen a written-off loan.');
  END IF;
END trg_loan_status_check;
/

-- ----------------------------------------------------------------------------
-- 11. SYNONYM
-- ----------------------------------------------------------------------------
CREATE OR REPLACE SYNONYM syn_customer FOR customer;
CREATE OR REPLACE SYNONYM syn_active_loans FOR vw_active_loans;

-- ----------------------------------------------------------------------------
-- 12. QUEUE  +  QUEUE TABLE  (Oracle Advanced Queuing)
-- ----------------------------------------------------------------------------
BEGIN
  DBMS_AQADM.CREATE_QUEUE_TABLE(
    queue_table        => 'dbmig_app.loan_event_qtab',
    queue_payload_type => 'SYS.AQ$_JMS_TEXT_MESSAGE');

  DBMS_AQADM.CREATE_QUEUE(
    queue_name  => 'dbmig_app.loan_event_q',
    queue_table => 'dbmig_app.loan_event_qtab');

  DBMS_AQADM.START_QUEUE(queue_name => 'dbmig_app.loan_event_q');
END;
/

-- ----------------------------------------------------------------------------
-- 13. XML SCHEMA  +  a table using it
-- ----------------------------------------------------------------------------
DECLARE
  v_xsd CLOB := '<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="http://dbmig.example.com/loan_notice"
           xmlns="http://dbmig.example.com/loan_notice"
           elementFormDefault="qualified">
  <xs:element name="LoanNotice">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="LoanId"  type="xs:integer"/>
        <xs:element name="Message" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>';
BEGIN
  DBMS_XMLSCHEMA.registerSchema(
    schemaurl    => 'http://dbmig.example.com/loan_notice.xsd',
    schemadoc    => v_xsd,
    csid         => NLS_CHARSET_ID('AL32UTF8'),
    genTypes     => TRUE,
    genTables    => FALSE
  );
END;
/

CREATE TABLE loan_notice_xml (
  notice_id  NUMBER(10) PRIMARY KEY,
  notice_doc XMLTYPE
) XMLTYPE notice_doc STORE AS SECUREFILE BINARY XML
  XMLSCHEMA "http://dbmig.example.com/loan_notice.xsd"
  ELEMENT "LoanNotice";

-- ----------------------------------------------------------------------------
-- 14. FULL-TEXT / "Oracle Text" object  (Oracle's equivalent of full-text
--     objects — CTXSYS must be installed, which it is by default in XE)
-- ----------------------------------------------------------------------------
CREATE INDEX ix_comm_notes_text ON comm_log (notes)
  INDEXTYPE IS CTXSYS.CONTEXT;

-- ----------------------------------------------------------------------------
-- 15. SCHEDULER JOB  ("Scheduler Job" / SQL Server's "SQL Agent Job")
-- ----------------------------------------------------------------------------
BEGIN
  DBMS_SCHEDULER.CREATE_JOB (
    job_name        => 'dbmig_app.job_refresh_loan_summary',
    job_type        => 'PLSQL_BLOCK',
    job_action      => 'BEGIN DBMS_MVIEW.REFRESH(''MV_LOAN_SUMMARY'',''F''); END;',
    start_date      => SYSTIMESTAMP,
    repeat_interval => 'FREQ=DAILY; BYHOUR=2',
    enabled         => TRUE,
    comments        => 'Nightly fast refresh of mv_loan_summary'
  );
END;
/

-- ----------------------------------------------------------------------------
-- 16. EXTERNAL TABLE  (uses the DBMIG_EXT_DIR directory from script 1 —
--     >>> confirm that OS path exists and is writable before this runs <<<)
-- ----------------------------------------------------------------------------
-- Write a small seed CSV the external table will read.
DECLARE
  v_file UTL_FILE.FILE_TYPE;
BEGIN
  v_file := UTL_FILE.FOPEN('DBMIG_EXT_DIR', 'customer_extract.csv', 'W');
  UTL_FILE.PUT_LINE(v_file, '1,John Doe,ACTIVE');
  UTL_FILE.PUT_LINE(v_file, '2,Jane Smith,ACTIVE');
  UTL_FILE.PUT_LINE(v_file, '3,Old Account,CLOSED');
  UTL_FILE.FCLOSE(v_file);
END;
/

CREATE TABLE ext_customer_extract (
  customer_id NUMBER(10),
  full_name   VARCHAR2(120),
  status      VARCHAR2(20)
)
ORGANIZATION EXTERNAL (
  TYPE ORACLE_LOADER
  DEFAULT DIRECTORY dbmig_ext_dir
  ACCESS PARAMETERS (
    RECORDS DELIMITED BY NEWLINE
    FIELDS CSV WITH EMBEDDED
    ( customer_id, full_name, status )
  )
  LOCATION ('customer_extract.csv')
)
REJECT LIMIT UNLIMITED;

-- ----------------------------------------------------------------------------
-- 17. OBJECT PRIVILEGE  (now that the objects exist)
-- ----------------------------------------------------------------------------
GRANT SELECT ON customer          TO dbmig_read_role;
GRANT SELECT ON loan              TO dbmig_read_role;
GRANT SELECT ON vw_active_loans   TO dbmig_read_role;
GRANT INSERT, UPDATE ON payment_hist TO dbmig_write_role;
GRANT EXECUTE ON pkg_loan_ops     TO dbmig_write_role;

-- ----------------------------------------------------------------------------
-- 18. STATISTICS  (populated for real after data load in script 03 — run
--     once now so the objects exist in DBA_TAB_STATISTICS, run again after load)
-- ----------------------------------------------------------------------------
BEGIN
  DBMS_STATS.GATHER_SCHEMA_STATS(ownname => 'DBMIG_APP', cascade => TRUE);
END;
/

PROMPT ===========================================================
PROMPT Schema objects complete. Next: run 03_generate_data.sql
PROMPT (same connection, dbmig_app) to load ~1 GB of data.
PROMPT ===========================================================
