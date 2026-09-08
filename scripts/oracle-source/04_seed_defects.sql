-- ============================================================================
-- 04_seed_defects.sql
-- Run connected AS dbmig_app. F5, nothing highlighted.
-- Deliberately introduces problems for the assessment engine to detect later.
-- Each one is logged in defects.md with its expected severity/finding.
-- ============================================================================

SET SERVEROUTPUT ON
WHENEVER SQLERROR CONTINUE

-- DEFECT 1: reserved-word / awkward identifier
CREATE TABLE "ORDER" (
  "ORDER" NUMBER(10) PRIMARY KEY,
  description VARCHAR2(100)
);

-- DEFECT 2: table with no primary key (breaks DMS CDC on this table)
CREATE TABLE audit_scratch (
  event_id   NUMBER(10),
  event_text VARCHAR2(200),
  logged_at  DATE DEFAULT SYSDATE
);
INSERT INTO audit_scratch VALUES (1, 'system check', SYSDATE);
INSERT INTO audit_scratch VALUES (2, 'system check', SYSDATE);
COMMIT;

-- DEFECT 3: unindexed foreign key (collateral_note.loan_id already qualifies --
-- confirm it has no index)
SELECT index_name FROM user_indexes WHERE table_name = 'COLLATERAL_NOTE';
-- (expect: no rows — confirms the defect from script 2 is still in place)

-- DEFECT 4: orphaned foreign key reference (disable constraint, insert bad
-- data, re-enable NOVALIDATE so existing bad rows are tolerated)
ALTER TABLE payment_hist DISABLE CONSTRAINT fk_payment_loan;
INSERT INTO payment_hist (loan_id, payment_date, amount_paid, method)
VALUES (999999999, SYSDATE, 500, 'UPI');   -- loan_id does not exist
COMMIT;
ALTER TABLE payment_hist ENABLE NOVALIDATE CONSTRAINT fk_payment_loan;

-- DEFECT 5: duplicate value in what should be a unique-ish business key
-- (email is UNIQUE already -- instead seed a duplicate national_id, which
-- has no constraint at all, matching "should be unique but isn't enforced")
UPDATE customer SET national_id = '000000001'
WHERE customer_id IN (SELECT customer_id FROM customer WHERE ROWNUM <= 2);
COMMIT;

-- DEFECT 6: NUMBER with unconstrained/oversized precision (mapping risk
-- to a target with a smaller safe integer range)
ALTER TABLE loan ADD (legacy_score NUMBER);   -- no precision at all

-- DEFECT 7: a column holding non-standard characters (data-quality finding)
UPDATE customer SET full_name = full_name || CHR(146)   -- smart-quote / cp1252 artifact
WHERE customer_id = 3;
COMMIT;

-- DEFECT 8: object left invalid on purpose (broken procedure body)
CREATE OR REPLACE PROCEDURE sp_broken_demo IS
BEGIN
  SELECT COUNT(*) INTO :dummy_missing_bind FROM nonexistent_table;
END;
/

-- Verify what's now invalid
SELECT object_name, object_type, status
FROM user_objects
WHERE status = 'INVALID';

PROMPT ===========================================================
PROMPT Defects seeded. See defects.md for the answer key.
PROMPT ===========================================================
