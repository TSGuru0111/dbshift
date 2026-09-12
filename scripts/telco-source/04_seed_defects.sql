-- ============================================================================
-- 04_seed_defects.sql  --  TELCO source estate
-- Run connected AS dbmig_telco @ XEPDB1, AFTER 03_generate_data.sql.
--
-- THIS IS A BLIND TEST OF THE ASSESSMENT ENGINE.
--
-- The DBMIG_APP estate seeds 8 defects and the 50 rules were tuned while
-- looking at them. Re-running those same 8 classes here would mostly re-measure
-- the tuning. So this set deliberately targets rules the DBMIG_APP estate
-- NEVER EXERCISED -- PERF-004, PERF-008, DQ-005, DQ-008, DQ-009, OPS-004 --
-- alongside a few shared classes placed on different objects.
--
-- Rules with no seeded defect here are not "expected to be silent": several
-- (RDS-012 LOB, RDS-013 partitioning, DQ-007 stats, PERF-006, SEC-*, OPS-001/2)
-- will fire on genuine facts about this estate. Those are additional findings,
-- not false positives -- the same convention as assess/scoring.py.
--
-- The answer key that mirrors this file is scripts/telco-source/answer_key.json.
-- If you change a defect here, change it there in the same commit.
-- ============================================================================

SET SERVEROUTPUT ON
SET DEFINE OFF
WHENEVER SQLERROR CONTINUE

-- ----------------------------------------------------------------------------
-- DEFECT 1 (HIGH, RDS-001/RDS-002) -- reserved words, table AND column
--   Different reserved words from the DBMIG_APP estate's "ORDER": here the
--   table is "SESSION" and it carries columns "COMMENT", "LEVEL" and "DATE".
--   All are Oracle reserved words that survive only because they are quoted.
-- ----------------------------------------------------------------------------
CREATE TABLE "SESSION" (
  "SESSION_ID"  NUMBER(12) NOT NULL,
  "COMMENT"     VARCHAR2(200),
  "LEVEL"       NUMBER(4),
  "DATE"        DATE,
  CONSTRAINT pk_session PRIMARY KEY ("SESSION_ID")
) TABLESPACE dbmig_telco_ts;

INSERT INTO "SESSION" ("SESSION_ID", "COMMENT", "LEVEL", "DATE")
SELECT lvl, 'Session note ' || lvl, MOD(lvl, 9), SYSDATE - lvl
FROM  (SELECT LEVEL AS lvl FROM dual CONNECT BY LEVEL <= 1200);
COMMIT;

-- ----------------------------------------------------------------------------
-- DEFECT 2 (CRITICAL, DQ-001) -- no primary key on a table with real volume
--   The DBMIG_APP version (audit_scratch) had 2 rows, so a rule could pass it
--   on a technicality. This one has 250,000 rows: DMS CDC genuinely cannot
--   replicate it, and PERF-002 (no indexes at all) should fire here too.
-- ----------------------------------------------------------------------------
CREATE TABLE usage_staging (
  batch_id      NUMBER(10),
  subscriber_id NUMBER(12),
  usage_date    DATE,
  usage_units   NUMBER(12,3),
  source_system VARCHAR2(40)
) TABLESPACE dbmig_telco_ts;

INSERT /*+ APPEND */ INTO usage_staging (batch_id, subscriber_id, usage_date, usage_units, source_system)
SELECT MOD(lvl, 500),
       TRUNC(DBMS_RANDOM.VALUE(1, 800000)),
       DATE '2025-06-01' + TRUNC(DBMS_RANDOM.VALUE(0, 300)),
       ROUND(DBMS_RANDOM.VALUE(0, 5000), 3),
       CASE MOD(lvl, 3) WHEN 0 THEN 'MEDIATION' WHEN 1 THEN 'OCS' ELSE 'LEGACY_ETL' END
FROM  (SELECT LEVEL AS lvl FROM dual CONNECT BY LEVEL <= 250000);
COMMIT;

-- ----------------------------------------------------------------------------
-- DEFECT 3 (MEDIUM, PERF-001) -- unindexed foreign key
--   payment.invoice_id IS indexed, so the FK to attack is a new one on a
--   different table. device.subscriber_id is indexed too; this adds a second
--   FK column with no supporting index.
-- ----------------------------------------------------------------------------
ALTER TABLE support_ticket ADD (assigned_cell_id VARCHAR2(16));

UPDATE support_ticket
   SET assigned_cell_id = 'CELL' || LPAD(MOD(ticket_id, 20000) + 1, 8, '0')
 WHERE MOD(ticket_id, 3) = 0;
COMMIT;

ALTER TABLE support_ticket
  ADD CONSTRAINT fk_ticket_cell FOREIGN KEY (assigned_cell_id)
      REFERENCES network_cell (cell_id);

-- ----------------------------------------------------------------------------
-- DEFECT 4 (HIGH, DQ-004) -- ENABLE NOVALIDATE constraint hiding bad data
--   Same class as the DBMIG_APP estate but on the invoice->subscriber path,
--   and with 40 orphans rather than 1, so the rule cannot pass by treating a
--   single row as noise.
-- ----------------------------------------------------------------------------
-- ORDER MATTERS. ENABLE NOVALIDATE means "stop checking the rows already here,
-- keep checking new ones" -- it does NOT permit fresh violating inserts. Doing
-- the ALTER first and then inserting raises ORA-02291 and seeds nothing.
--
-- The constraint must be DISABLED for the insert, then re-enabled NOVALIDATE,
-- which is also how this state arises in the wild: someone disables a
-- constraint for a bulk load, data goes in, and it is re-enabled without
-- validation because validating it would fail.
ALTER TABLE invoice DISABLE CONSTRAINT fk_invoice_sub;

DECLARE
  v_max NUMBER;
BEGIN
  SELECT MAX(invoice_id) INTO v_max FROM invoice;
  FOR i IN 1 .. 40 LOOP
    INSERT INTO invoice (invoice_id, subscriber_id, bill_period, issued_on, due_on,
                         total_amount, tax_amount, invoice_status)
    VALUES (v_max + i, 999999999, '2026-01', DATE '2026-01-01', DATE '2026-01-21',
            1500, 120, 'ISSUED');
  END LOOP;
  COMMIT;
  DBMS_OUTPUT.PUT_LINE('orphaned invoices inserted: 40');
END;
/

ALTER TABLE invoice MODIFY CONSTRAINT fk_invoice_sub ENABLE NOVALIDATE;

-- Assert the orphans survived the re-enable. If ENABLE NOVALIDATE had actually
-- validated, this would be 0 and defect 4 would be silently absent.
DECLARE
  v_orphans NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_orphans FROM invoice i
   WHERE NOT EXISTS (SELECT 1 FROM subscriber s WHERE s.subscriber_id = i.subscriber_id);
  IF v_orphans = 0 THEN
    RAISE_APPLICATION_ERROR(-20004,
      'Defect 4 not seeded: no orphaned invoice rows survived the re-enable.');
  END IF;
  DBMS_OUTPUT.PUT_LINE('defect 4 verified: ' || v_orphans || ' orphaned invoices');
END;
/

-- ----------------------------------------------------------------------------
-- DEFECT 5 (MEDIUM, DQ-002) -- duplicates in a should-be-unique column
--   subscriber.national_id has no unique constraint. 500 rows share one value.
-- ----------------------------------------------------------------------------
UPDATE subscriber
   SET national_id = '000000001'
 WHERE subscriber_id <= 500;
COMMIT;

-- ----------------------------------------------------------------------------
-- DEFECT 6 (MEDIUM, DQ-006) -- NUMBER with no precision or scale
--   Plus a FLOAT, which carries the same mapping risk to a fixed-width target.
-- ----------------------------------------------------------------------------
ALTER TABLE subscriber ADD (
  loyalty_points  NUMBER,
  risk_factor     FLOAT
);

UPDATE subscriber
   SET loyalty_points = MOD(subscriber_id * 37, 100000),
       risk_factor    = DBMS_RANDOM.VALUE(0, 1)
 WHERE MOD(subscriber_id, 4) = 0;
COMMIT;

-- ----------------------------------------------------------------------------
-- DEFECT 7 (LOW, DQ-003) -- non-ASCII characters, SEEDED CORRECTLY
--   The DBMIG_APP estate's defect 7 was never actually seeded: it used
--   CHR(146), and in an AL32UTF8 database byte 0x92 is a bare UTF-8
--   continuation byte. CHR(146) itself is NOT NULL -- it returns a 1-byte value
--   holding 0x92 -- but the invalid byte is DROPPED during concatenation, so
--   'x' || CHR(146) is just 'x' and the UPDATE changed nothing while reporting
--   a row modified. (An earlier note in docs/04-defects.md said CHR(146)
--   returns NULL; measured, it does not. Testing "CHR(146) IS NULL" returns
--   false and would wrongly suggest the seed had worked.)
--
--   UNISTR is the fix: it takes a Unicode code point regardless of the
--   database character set. U+2019 is the RIGHT SINGLE QUOTATION MARK that
--   cp1252 0x92 actually maps to -- the real-world artifact this simulates.
--   Also seeds U+00E9 (e-acute) and U+20AC (euro sign).
-- ----------------------------------------------------------------------------
UPDATE subscriber SET full_name = full_name || UNISTR('\2019') WHERE subscriber_id = 3;
UPDATE subscriber SET full_name = UNISTR('Andr\00E9 Fran\00E7ois') WHERE subscriber_id = 7;
UPDATE subscriber SET full_name = full_name || UNISTR(' \20AC') WHERE subscriber_id = 11;
COMMIT;

-- Prove it landed. Unlike the DBMIG_APP script, this does not trust "1 row
-- updated" -- that is exactly what masked the original failure.
SELECT subscriber_id, full_name, DUMP(full_name, 1016) AS dump_out
FROM   subscriber WHERE subscriber_id IN (3, 7, 11);

SELECT COUNT(*) AS non_ascii_rows
FROM   subscriber
WHERE  ASCIISTR(full_name) != full_name;

-- ----------------------------------------------------------------------------
-- DEFECT 8 (HIGH, SEC-001 / OPS-004) -- invalid object with stored errors
-- ----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_rate_cdr_broken AS
  v_amount NUMBER;
BEGIN
  -- NO_SUCH_TABLE does not exist: PLS-00201 at compile time.
  SELECT rated_amount INTO v_amount FROM no_such_rating_table WHERE ROWNUM = 1;
  UPDATE cdr SET rated_amount = v_amount WHERE cdr_id = 1;
END;
/

-- ----------------------------------------------------------------------------
-- DEFECT 9 (MEDIUM, PERF-004) -- unusable index and invisible index
--   NOT PRESENT in the DBMIG_APP estate. An unusable index silently stops
--   being maintained; an invisible one is maintained but ignored by the
--   optimizer. Both survive a migration and surprise people afterwards.
-- ----------------------------------------------------------------------------
CREATE INDEX ix_payment_ref ON payment (reference_no) TABLESPACE dbmig_telco_ts;
ALTER INDEX ix_payment_ref UNUSABLE;

CREATE INDEX ix_invoice_status ON invoice (invoice_status) TABLESPACE dbmig_telco_ts;
ALTER INDEX ix_invoice_status INVISIBLE;

-- ----------------------------------------------------------------------------
-- DEFECT 10 (MEDIUM, DQ-005) -- disabled constraint
--   NOT PRESENT in the DBMIG_APP estate. A disabled check constraint does not
--   migrate as a constraint and does not protect the data in the meantime.
-- ----------------------------------------------------------------------------
ALTER TABLE device DISABLE CONSTRAINT ck_device_stat;

INSERT INTO device (subscriber_id, imei, sim_serial, make, model, activated_on, device_status)
SELECT 1, '000000000000001', '8991000000000000001', 'Unknown', 'Unknown', SYSDATE, 'ZZ_INVALID'
FROM   dual;
COMMIT;

-- ----------------------------------------------------------------------------
-- DEFECT 11 (LOW, DQ-008) -- column that is entirely NULL
--   NOT PRESENT in the DBMIG_APP estate. Migrating a column that has never
--   held a value is wasted work, and often signals a dead feature.
-- ----------------------------------------------------------------------------
ALTER TABLE invoice_line ADD (legacy_tariff_ref VARCHAR2(40));

-- ----------------------------------------------------------------------------
-- DEFECT 12 (LOW, DQ-009) -- byte-length semantics on a text column
--   NOT PRESENT in the DBMIG_APP estate. VARCHAR2(n BYTE) truncates
--   multi-byte characters mid-character; combined with defect 7's non-ASCII
--   data this is a real migration hazard, not a theoretical one.
-- ----------------------------------------------------------------------------
ALTER TABLE support_ticket ADD (
  agent_note   VARCHAR2(100 BYTE),
  agent_locale VARCHAR2(20 BYTE)
);

UPDATE support_ticket
   SET agent_note   = 'Handled by agent ' || MOD(ticket_id, 400),
       agent_locale = 'en_IN'
 WHERE MOD(ticket_id, 7) = 0;
COMMIT;

-- ----------------------------------------------------------------------------
-- DEFECT 13 (LOW, PERF-008) -- redundant index on the same leading column
--   NOT PRESENT in the DBMIG_APP estate. ix_device_sub is already
--   device(subscriber_id); this adds device(subscriber_id, device_status),
--   which makes the single-column index redundant. Both migrate and both cost
--   write throughput on the target.
-- ----------------------------------------------------------------------------
CREATE INDEX ix_device_sub_status ON device (subscriber_id, device_status)
  TABLESPACE dbmig_telco_ts;

-- ----------------------------------------------------------------------------
-- DEFECT 14 (MEDIUM, SEC-007) -- sensitive-looking columns, unencrypted
--   national_id already exists; this adds unmistakably sensitive names.
-- ----------------------------------------------------------------------------
ALTER TABLE subscriber ADD (
  bank_account_no  VARCHAR2(34),
  credit_card_hash VARCHAR2(64),
  passport_number  VARCHAR2(20)
);

-- STANDARD_HASH, not DBMS_CRYPTO: the latter needs an explicit EXECUTE grant
-- that dbmig_telco does not hold, and granting it just to fabricate test data
-- would widen this account's privileges for no reason.
UPDATE subscriber
   SET bank_account_no  = 'IN' || LPAD(MOD(subscriber_id * 991, 10000000000), 20, '0'),
       credit_card_hash = STANDARD_HASH(TO_CHAR(subscriber_id), 'SHA256')
 WHERE MOD(subscriber_id, 10) = 0;
COMMIT;

-- ----------------------------------------------------------------------------
-- Grant the collector SELECT on the two tables this script created.
--
-- Easy to forget, and the failure is not obvious: without these the discovery
-- collector cannot profile them, and -- until the fix in collector/probes/
-- dataprofile.py -- a single ORA-00942 made it skip every alphabetically-later
-- table in the schema too. SUBSCRIBER, SUPPORT_TICKET and USAGE_STAGING were all
-- silently unprofiled because "SESSION" sorts first, and defects 5 and 7 were
-- scored as engine misses as a result.
-- ----------------------------------------------------------------------------
GRANT SELECT ON "SESSION"     TO dbmig_collector;
GRANT SELECT ON usage_staging TO dbmig_collector;

-- ----------------------------------------------------------------------------
-- Clear incidental invalidation, then confirm exactly ONE invalid object.
-- Adding columns to subscriber invalidates its dependents. That is normal
-- Oracle dependency behaviour, not a defect -- but it must be cleared, or the
-- regression check below cannot distinguish it from a real problem.
-- ----------------------------------------------------------------------------
ALTER VIEW vw_active_subscriber COMPILE;
ALTER VIEW vw_invoice_balance COMPILE;
ALTER PROCEDURE sp_bar_subscriber COMPILE;
ALTER FUNCTION fn_subscriber_balance COMPILE;
ALTER PACKAGE pkg_billing_ops COMPILE BODY;
ALTER MATERIALIZED VIEW mv_revenue_by_period COMPILE;

-- The MV goes NEEDS_COMPILE again once the ALTER TABLE ... ADD statements above
-- have all run, so it is recompiled last. Without this the regression check
-- below finds two INVALID objects and cannot tell the deliberate one from the
-- incidental one.

-- Re-gather stats: several defects changed row shapes and added columns, and
-- Phase 3 sizing reads these numbers.
BEGIN
  DBMS_STATS.GATHER_SCHEMA_STATS(
    ownname          => USER,
    estimate_percent => DBMS_STATS.AUTO_SAMPLE_SIZE,
    method_opt       => 'FOR ALL COLUMNS SIZE AUTO',
    degree           => 2,
    cascade          => TRUE);
END;
/

-- ----------------------------------------------------------------------------
-- VERIFY -- every assertion below is checked, none assumed
-- ----------------------------------------------------------------------------
PROMPT === must be exactly one row: SP_RATE_CDR_BROKEN ===
SELECT object_name, object_type, status FROM user_objects WHERE status = 'INVALID';

PROMPT === defect 9: one UNUSABLE, one INVISIBLE ===
SELECT index_name, status, visibility FROM user_indexes
WHERE  status = 'UNUSABLE' OR visibility = 'INVISIBLE';

PROMPT === defect 10: disabled constraint ===
SELECT constraint_name, table_name, status, validated FROM user_constraints
WHERE  status = 'DISABLED' OR validated = 'NOT VALIDATED';

PROMPT === defect 11: legacy_tariff_ref must be 100% NULL ===
SELECT COUNT(*) AS total, COUNT(legacy_tariff_ref) AS non_null FROM invoice_line;

PROMPT === defect 12: byte-semantics columns ===
SELECT table_name, column_name, data_type, char_used FROM user_tab_columns
WHERE  char_used = 'B' AND data_type LIKE '%CHAR%';

PROMPT === defect 5: duplicate national_id ===
SELECT national_id, COUNT(*) FROM subscriber
GROUP BY national_id HAVING COUNT(*) > 1 ORDER BY 2 DESC FETCH FIRST 3 ROWS ONLY;

PROMPT === defect 4: orphaned invoices ===
SELECT COUNT(*) AS orphans FROM invoice i
WHERE NOT EXISTS (SELECT 1 FROM subscriber s WHERE s.subscriber_id = i.subscriber_id);

PROMPT === final size ===
SELECT ROUND(SUM(bytes)/1024/1024/1024, 3) AS size_gb FROM user_segments;
SELECT COUNT(*) AS objects FROM user_objects;
SELECT COUNT(*) AS constraints FROM user_constraints;

PROMPT ============================================================
PROMPT Defect seeding complete. 14 defects; see answer_key.json.
PROMPT Every VERIFY block above must match the key.
PROMPT ============================================================
