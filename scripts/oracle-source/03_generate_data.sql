-- ============================================================================
-- 03_generate_data.sql   (REVISED — no substitution variables)
-- Run connected AS dbmig_app. F5 (Run Script), nothing highlighted.
--
-- Row-count targets are hardcoded in each DECLARE block below (v_total).
-- Edit those numbers directly to tune total size — no & variables anywhere,
-- so this runs identically whether SET DEFINE is ON or OFF.
-- ============================================================================

SET SERVEROUTPUT ON
SET DEFINE OFF
WHENEVER SQLERROR CONTINUE

-- ----------------------------------------------------------------------------
-- 1. CUSTOMERS   (edit v_total to change the count)
-- ----------------------------------------------------------------------------
DECLARE
  TYPE t_names IS VARRAY(10) OF VARCHAR2(30);
  v_first t_names := t_names('James','Priya','Wei','Fatima','Carlos','Anna','Kenji','Olga','Sam','Lucia');
  v_last  t_names := t_names('Smith','Sharma','Chen','Khan','Garcia','Novak','Sato','Ivanov','Lee','Rossi');
  v_batch PLS_INTEGER := 10000;
  v_total PLS_INTEGER := 60000;      -- <<< EDIT HERE to change customer count
BEGIN
  FOR b IN 0 .. CEIL(v_total / v_batch) - 1 LOOP
    FOR i IN 1 .. LEAST(v_batch, v_total - b*v_batch) LOOP
      INSERT INTO customer (full_name, email, national_id, status, address, phone_numbers)
      VALUES (
        v_first(MOD(b*v_batch+i,10)+1) || ' ' || v_last(MOD((b*v_batch+i)*7,10)+1),
        'cust' || (b*v_batch+i) || '@example.com',
        LPAD(TO_CHAR(TRUNC(DBMS_RANDOM.VALUE(100000000,999999999))),9,'0'),
        CASE MOD(b*v_batch+i,25) WHEN 0 THEN 'SUSPENDED' WHEN 1 THEN 'CLOSED' ELSE 'ACTIVE' END,
        ty_address('Flat '||MOD(i,50), NULL, 'City'||MOD(i,40), 'State'||MOD(i,12), LPAD(MOD(i,99999),5,'0'), 'IN'),
        ty_phone_list('+91' || TRUNC(DBMS_RANDOM.VALUE(6000000000,9999999999)))
      );
    END LOOP;
    COMMIT;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('Customers loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 2. LOANS
-- ----------------------------------------------------------------------------
DECLARE
  v_batch   PLS_INTEGER := 10000;
  v_total   PLS_INTEGER := 120000;   -- <<< EDIT HERE
  v_maxcust NUMBER;
BEGIN
  SELECT MAX(customer_id) INTO v_maxcust FROM customer;
  FOR b IN 0 .. CEIL(v_total / v_batch) - 1 LOOP
    FOR i IN 1 .. LEAST(v_batch, v_total - b*v_batch) LOOP
      INSERT INTO loan (customer_id, principal_amt, interest_rate, loan_status, disbursed_on)
      VALUES (
        TRUNC(DBMS_RANDOM.VALUE(1, v_maxcust+1)),
        ROUND(DBMS_RANDOM.VALUE(20000, 800000), 2),
        ROUND(DBMS_RANDOM.VALUE(9, 24), 2),
        CASE MOD(b*v_batch+i,20) WHEN 0 THEN 'CLOSED' WHEN 1 THEN 'WRITTEN_OFF' ELSE 'OPEN' END,
        SYSDATE - TRUNC(DBMS_RANDOM.VALUE(30, 900))
      );
    END LOOP;
    COMMIT;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('Loans loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 3. LOAN_TXN  (the volume table — biggest driver of total size)
-- ----------------------------------------------------------------------------
DECLARE
  v_batch   PLS_INTEGER := 20000;
  v_total   PLS_INTEGER := 3000000; -- <<< EDIT HERE (lower this first if too slow/too big)
  v_maxloan NUMBER;
BEGIN
  SELECT MAX(loan_id) INTO v_maxloan FROM loan;
  FOR b IN 0 .. CEIL(v_total / v_batch) - 1 LOOP
    INSERT INTO loan_txn (loan_id, txn_date, txn_amount, txn_type)
    SELECT
      TRUNC(DBMS_RANDOM.VALUE(1, v_maxloan+1)),
      DATE '2025-01-01' + TRUNC(DBMS_RANDOM.VALUE(0, 590)),
      ROUND(DBMS_RANDOM.VALUE(500, 50000), 2),
      CASE TRUNC(DBMS_RANDOM.VALUE(0,4)) WHEN 0 THEN 'DEBIT' WHEN 1 THEN 'CREDIT' WHEN 2 THEN 'FEE' ELSE 'REVERSAL' END
    FROM DUAL
    CONNECT BY LEVEL <= LEAST(v_batch, v_total - b*v_batch);
    COMMIT;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('loan_txn loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 4. PAYMENT_HIST
-- ----------------------------------------------------------------------------
DECLARE
  v_batch   PLS_INTEGER := 20000;
  v_total   PLS_INTEGER := 1800000; -- <<< EDIT HERE
  v_maxloan NUMBER;
BEGIN
  SELECT MAX(loan_id) INTO v_maxloan FROM loan;
  FOR b IN 0 .. CEIL(v_total / v_batch) - 1 LOOP
    INSERT INTO payment_hist (loan_id, payment_date, amount_paid, method)
    SELECT
      TRUNC(DBMS_RANDOM.VALUE(1, v_maxloan+1)),
      DATE '2025-01-01' + TRUNC(DBMS_RANDOM.VALUE(0, 590)),
      ROUND(DBMS_RANDOM.VALUE(500, 30000), 2),
      CASE TRUNC(DBMS_RANDOM.VALUE(0,3)) WHEN 0 THEN 'AUTO_DEBIT' WHEN 1 THEN 'UPI' ELSE 'CHEQUE' END
    FROM DUAL
    CONNECT BY LEVEL <= LEAST(v_batch, v_total - b*v_batch);
    COMMIT;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('payment_hist loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 5. COMM_LOG  (has the CLOB — shrink this first if you go over 1 GB)
-- ----------------------------------------------------------------------------
DECLARE
  v_batch   PLS_INTEGER := 5000;
  v_total   PLS_INTEGER := 400000;  -- <<< EDIT HERE
  v_maxcust NUMBER;
  v_note    CLOB;
BEGIN
  SELECT MAX(customer_id) INTO v_maxcust FROM customer;
  FOR b IN 0 .. CEIL(v_total / v_batch) - 1 LOOP
    FOR i IN 1 .. LEAST(v_batch, v_total - b*v_batch) LOOP
      v_note := 'Customer contacted regarding upcoming EMI due date. ' ||
                'Call notes: discussed repayment options, confirmed contact ' ||
                'details, and logged a follow-up reminder for the collections ' ||
                'team. Reference batch ' || b || ' record ' || i || '. ' ||
                RPAD('x', 300, 'x');
      INSERT INTO comm_log (customer_id, comm_date, channel, notes)
      VALUES (
        TRUNC(DBMS_RANDOM.VALUE(1, v_maxcust+1)),
        SYSDATE - TRUNC(DBMS_RANDOM.VALUE(0, 590)),
        CASE MOD(i,3) WHEN 0 THEN 'EMAIL' WHEN 1 THEN 'SMS' ELSE 'CALL' END,
        v_note
      );
    END LOOP;
    COMMIT;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('comm_log loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 6. Small seeded-defect table
-- ----------------------------------------------------------------------------
INSERT INTO collateral_note (note_id, loan_id, note_text)
SELECT ROWNUM, l.loan_id, 'Collateral document on file, ref #' || ROWNUM
FROM   loan l
WHERE  ROWNUM <= 20000;
COMMIT;

-- ----------------------------------------------------------------------------
-- 7. Refresh the materialized view and gather final statistics
-- ----------------------------------------------------------------------------
BEGIN
  DBMS_MVIEW.REFRESH('MV_LOAN_SUMMARY', 'C');
  DBMS_STATS.GATHER_SCHEMA_STATS(ownname => USER, cascade => TRUE);
END;
/

-- ----------------------------------------------------------------------------
-- 8. VERIFY actual size on disk — USER_SEGMENTS, not DBA_SEGMENTS, since
--    dbmig_app is not granted SELECT_CATALOG_ROLE
-- ----------------------------------------------------------------------------
SELECT ROUND(SUM(bytes)/1024/1024/1024, 3) AS size_gb
FROM   user_segments;

SELECT segment_name, segment_type, ROUND(bytes/1024/1024,1) AS mb
FROM   user_segments
ORDER  BY bytes DESC
FETCH FIRST 15 ROWS ONLY;

SELECT table_name, num_rows
FROM   user_tables
ORDER  BY num_rows DESC NULLS LAST;

PROMPT ===========================================================
PROMPT Data load complete. Check the SIZE_GB query above.
PROMPT  - Under 1 GB?  Raise the v_total in section 3 or 5 above
PROMPT    and re-run just that section (select it, Ctrl+Enter).
PROMPT  - Over 1 GB?   Lower those two, then:
PROMPT      TRUNCATE TABLE loan_txn;  TRUNCATE TABLE comm_log;
PROMPT    and re-run sections 3 and 5 with smaller numbers.
PROMPT ===========================================================
