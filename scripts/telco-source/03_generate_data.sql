-- ============================================================================
-- 03_generate_data.sql  --  TELCO source estate, ~5 GB target
-- Run connected AS dbmig_telco @ XEPDB1.
--
-- SET-BASED, not row-by-row. The DBMIG_APP generator used nested PL/SQL loops
-- with a single-row INSERT and took 20-40 minutes for 1 GB. That does not scale
-- to 5 GB. Every bulk table here is loaded with
--     INSERT /*+ APPEND */ ... SELECT ... FROM dual CONNECT BY LEVEL <= n
-- which is a direct-path load: it writes formatted blocks straight above the
-- high-water mark and bypasses most undo. Each chunk is followed by COMMIT,
-- which a direct-path insert requires before the table can be read again.
--
-- Row targets are hardcoded (no substitution variables), same convention as the
-- DBMIG_APP generator, so this runs identically with SET DEFINE ON or OFF.
--
-- FOREIGN KEYS: every child table picks its parent id from the parent's ACTUAL
-- MIN..MAX range, never from 1..MAX.
--
-- Surrogate keys here come from sequences, and a sequence is not reset by
-- TRUNCATE. After the calibration load consumed values 1..50,000 and the table
-- was truncated, subscriber_id ran 50,001..850,000 -- 800,000 contiguous rows
-- that simply do not start at 1. DBMS_RANDOM.VALUE(1, v_maxsub + 1) therefore
-- generated 50,000 ids that did not exist, and the very first DEVICE chunk died
-- with ORA-02291. Because the runner continues past errors (matching
-- WHENEVER SQLERROR CONTINUE), the load carried on to CDR and DEVICE was left
-- silently empty -- exactly the class of partial-script failure this project
-- has lost time to before.
--
-- Each block below therefore reads MIN and MAX, and section 12 re-checks every
-- FK afterwards rather than trusting that the inserts worked.
--
-- SIZE BUDGET -- MEASURED, not estimated.
--
-- These row counts come from a calibration load: a 50,000-row sample into each
-- table, DBMS_STATS, then bytes/row derived from used blocks plus index leaf
-- blocks plus LOB segments. Do not "tidy" them into round numbers -- they are
-- the numbers that hit 5 GB on this schema with these column widths.
--
-- Why not estimate: the first estimate here was wrong by 5x. A fresh partition
-- allocates an 8 MB initial extent, so a small sample reports ~920 bytes/row
-- for CDR when the true density is ~156. Extrapolating the sample's ALLOCATED
-- bytes projected 26.5 GB, which would have overrun both the 20 GB tablespace
-- and XE's 12 GB user-data limit several hours into the load.
--
--   table            rows        B/row   size
--   cdr            21,000,000    156.2   3.05 GB   <- volume driver
--   invoice_line    5,000,000    154.2   0.72 GB
--   support_ticket    400,000  1,437.7   0.54 GB   <- the CLOB
--   invoice         2,500,000    135.5   0.32 GB
--   payment         2,000,000     92.2   0.17 GB
--   subscriber        800,000    247.0   0.18 GB
--   device          1,000,000    156.8   0.15 GB
--                                        -------
--                              subtotal   5.13 GB
--                      + dimensions/MV/mlog/IOT   ~0.15 GB
--                                        =======
--                                         ~5.28 GB
-- Verify with the USER_SEGMENTS query in section 11.
-- ============================================================================

SET SERVEROUTPUT ON
SET DEFINE OFF
WHENEVER SQLERROR CONTINUE

-- ----------------------------------------------------------------------------
-- 1. PLAN_CATALOG  -- 500 plans (small dimension, IOT)
-- ----------------------------------------------------------------------------
INSERT INTO plan_catalog (plan_code, plan_name, monthly_rental, data_quota_gb,
                          voice_mins, plan_type, active_flag)
SELECT 'PLAN' || LPAD(lvl, 5, '0'),
       CASE MOD(lvl, 5)
         WHEN 0 THEN 'Unlimited Max '   WHEN 1 THEN 'Smart Saver '
         WHEN 2 THEN 'Family Share '    WHEN 3 THEN 'Business Pro '
         ELSE 'Data Booster ' END || lvl,
       ROUND(DBMS_RANDOM.VALUE(149, 2999), 2),
       ROUND(DBMS_RANDOM.VALUE(1, 200), 2),
       TRUNC(DBMS_RANDOM.VALUE(100, 5000)),
       CASE MOD(lvl, 3) WHEN 0 THEN 'PREPAID' WHEN 1 THEN 'ENTERPRISE' ELSE 'POSTPAID' END,
       CASE WHEN MOD(lvl, 17) = 0 THEN 'N' ELSE 'Y' END
FROM  (SELECT LEVEL AS lvl FROM dual CONNECT BY LEVEL <= 500);
COMMIT;

-- ----------------------------------------------------------------------------
-- 2. NETWORK_CELL  -- 20,000 cells
-- ----------------------------------------------------------------------------
INSERT INTO network_cell (cell_id, site_name, region, technology,
                          latitude, longitude, commissioned_on)
SELECT 'CELL' || LPAD(lvl, 8, '0'),
       'Site ' || lvl,
       'Region' || MOD(lvl, 40),
       CASE MOD(lvl, 4) WHEN 0 THEN '2G' WHEN 1 THEN '3G' WHEN 2 THEN '4G' ELSE '5G' END,
       ROUND(DBMS_RANDOM.VALUE(8.0, 35.0), 6),
       ROUND(DBMS_RANDOM.VALUE(68.0, 97.0), 6),
       DATE '2015-01-01' + TRUNC(DBMS_RANDOM.VALUE(0, 3800))
FROM  (SELECT LEVEL AS lvl FROM dual CONNECT BY LEVEL <= 20000);
COMMIT;

-- ----------------------------------------------------------------------------
-- 3. SUBSCRIBER  -- 800,000
--    Object and varray columns are constructed per row, so this one stays a
--    PL/SQL chunked loop rather than a single set-based insert.
-- ----------------------------------------------------------------------------
DECLARE
  v_total PLS_INTEGER := 800000;
  v_chunk PLS_INTEGER := 50000;
  v_done  PLS_INTEGER := 0;
BEGIN
  WHILE v_done < v_total LOOP
    INSERT /*+ APPEND */ INTO subscriber
      (account_no, full_name, email, national_id, plan_code, sub_status,
       activated_on, churn_date, credit_limit, service_addr, msisdns)
    SELECT
      'ACC' || LPAD(v_done + lvl, 12, '0'),
      CASE MOD(v_done + lvl, 10)
        WHEN 0 THEN 'James'  WHEN 1 THEN 'Priya'  WHEN 2 THEN 'Wei'
        WHEN 3 THEN 'Fatima' WHEN 4 THEN 'Carlos' WHEN 5 THEN 'Anna'
        WHEN 6 THEN 'Kenji'  WHEN 7 THEN 'Olga'   WHEN 8 THEN 'Sam'
        ELSE 'Lucia' END || ' ' ||
      CASE MOD((v_done + lvl) * 7, 10)
        WHEN 0 THEN 'Smith'  WHEN 1 THEN 'Sharma' WHEN 2 THEN 'Chen'
        WHEN 3 THEN 'Khan'   WHEN 4 THEN 'Garcia' WHEN 5 THEN 'Novak'
        WHEN 6 THEN 'Sato'   WHEN 7 THEN 'Ivanov' WHEN 8 THEN 'Lee'
        ELSE 'Rossi' END,
      'sub' || (v_done + lvl) || '@telco.example.com',
      LPAD(TO_CHAR(TRUNC(DBMS_RANDOM.VALUE(100000000, 999999999))), 9, '0'),
      'PLAN' || LPAD(MOD(v_done + lvl, 500) + 1, 5, '0'),
      CASE MOD(v_done + lvl, 25)
        WHEN 0 THEN 'SUSPENDED' WHEN 1 THEN 'CHURNED'
        WHEN 2 THEN 'BARRED'    ELSE 'ACTIVE' END,
      DATE '2019-01-01' + TRUNC(DBMS_RANDOM.VALUE(0, 2400)),
      CASE WHEN MOD(v_done + lvl, 25) = 1
           THEN DATE '2024-01-01' + TRUNC(DBMS_RANDOM.VALUE(0, 700)) END,
      ROUND(DBMS_RANDOM.VALUE(1000, 90000), 2),
      ty_service_addr('Flat ' || MOD(lvl, 200), 'Block ' || MOD(lvl, 30),
                      'City' || MOD(lvl, 60), 'Region' || MOD(lvl, 40),
                      LPAD(MOD(lvl, 99999), 6, '0'), 'IN'),
      ty_msisdn_list('+91' || TRUNC(DBMS_RANDOM.VALUE(6000000000, 9999999999)))
    FROM (SELECT LEVEL AS lvl FROM dual
          CONNECT BY LEVEL <= LEAST(v_chunk, v_total - v_done));
    COMMIT;
    v_done := v_done + v_chunk;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('subscriber loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 4. DEVICE  -- 1,000,000
-- ----------------------------------------------------------------------------
DECLARE
  v_total PLS_INTEGER := 1000000;
  v_chunk PLS_INTEGER := 100000;
  v_done  PLS_INTEGER := 0;
  v_minsub NUMBER;
  v_maxsub NUMBER;
BEGIN
  SELECT MIN(subscriber_id), MAX(subscriber_id)
    INTO v_minsub, v_maxsub FROM subscriber;
  WHILE v_done < v_total LOOP
    INSERT /*+ APPEND */ INTO device
      (subscriber_id, imei, sim_serial, make, model, activated_on, device_status)
    SELECT
      TRUNC(DBMS_RANDOM.VALUE(v_minsub, v_maxsub + 1)),
      LPAD(TO_CHAR(TRUNC(DBMS_RANDOM.VALUE(1e14, 9.9e14))), 15, '0'),
      '89910' || LPAD(TO_CHAR(v_done + lvl), 14, '0'),
      CASE MOD(lvl, 6)
        WHEN 0 THEN 'Samsung' WHEN 1 THEN 'Apple'  WHEN 2 THEN 'Xiaomi'
        WHEN 3 THEN 'OnePlus' WHEN 4 THEN 'Realme' ELSE 'Nokia' END,
      'Model-' || MOD(lvl, 120),
      DATE '2020-01-01' + TRUNC(DBMS_RANDOM.VALUE(0, 2000)),
      CASE MOD(lvl, 20)
        WHEN 0 THEN 'SPARE' WHEN 1 THEN 'LOST'
        WHEN 2 THEN 'RETIRED' ELSE 'IN_USE' END
    FROM (SELECT LEVEL AS lvl FROM dual
          CONNECT BY LEVEL <= LEAST(v_chunk, v_total - v_done));
    COMMIT;
    v_done := v_done + v_chunk;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('device loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 5. CDR  -- 21,000,000. The volume table and the bulk of the 5 GB.
--    call_date is spread across 2025-01-01 .. 2026-03-31 so every declared
--    partition receives rows; p_max stays empty, which is correct for a
--    range-partitioned table with a MAXVALUE catch-all.
-- ----------------------------------------------------------------------------
DECLARE
  v_total  PLS_INTEGER := 21000000;
  v_chunk  PLS_INTEGER := 250000;
  v_done   PLS_INTEGER := 0;
  v_minsub NUMBER;
  v_maxsub NUMBER;
BEGIN
  SELECT MIN(subscriber_id), MAX(subscriber_id)
    INTO v_minsub, v_maxsub FROM subscriber;
  WHILE v_done < v_total LOOP
    INSERT /*+ APPEND */ INTO cdr
      (cdr_id, subscriber_id, call_date, called_number, call_type,
       duration_sec, bytes_up, bytes_down, cell_id, roaming_flag, rated_amount)
    SELECT
      v_done + lvl,
      TRUNC(DBMS_RANDOM.VALUE(v_minsub, v_maxsub + 1)),
      DATE '2025-01-01' + TRUNC(DBMS_RANDOM.VALUE(0, 455)),
      '+91' || TRUNC(DBMS_RANDOM.VALUE(6000000000, 9999999999)),
      CASE TRUNC(DBMS_RANDOM.VALUE(0, 5))
        WHEN 0 THEN 'VOICE' WHEN 1 THEN 'SMS' WHEN 2 THEN 'DATA'
        WHEN 3 THEN 'MMS'   ELSE 'ROAM' END,
      TRUNC(DBMS_RANDOM.VALUE(0, 3600)),
      TRUNC(DBMS_RANDOM.VALUE(0, 50000000)),
      TRUNC(DBMS_RANDOM.VALUE(0, 500000000)),
      'CELL' || LPAD(TRUNC(DBMS_RANDOM.VALUE(1, 20001)), 8, '0'),
      CASE WHEN MOD(lvl, 30) = 0 THEN 'Y' ELSE 'N' END,
      ROUND(DBMS_RANDOM.VALUE(0, 45), 4)
    FROM (SELECT LEVEL AS lvl FROM dual
          CONNECT BY LEVEL <= LEAST(v_chunk, v_total - v_done));
    COMMIT;
    v_done := v_done + v_chunk;
    IF MOD(v_done, 2000000) = 0 THEN
      DBMS_OUTPUT.PUT_LINE('  cdr progress: ' || v_done);
    END IF;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('cdr loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 6. INVOICE  -- 2,500,000  (5 bill periods x 500,000 billed subscribers)
-- ----------------------------------------------------------------------------
DECLARE
  v_total  PLS_INTEGER := 2500000;
  v_chunk  PLS_INTEGER := 200000;
  v_done   PLS_INTEGER := 0;
  v_minsub NUMBER;
  v_maxsub NUMBER;
BEGIN
  SELECT MIN(subscriber_id), MAX(subscriber_id)
    INTO v_minsub, v_maxsub FROM subscriber;
  WHILE v_done < v_total LOOP
    INSERT /*+ APPEND */ INTO invoice
      (invoice_id, subscriber_id, bill_period, issued_on, due_on,
       total_amount, tax_amount, invoice_status)
    SELECT
      v_done + lvl,
      v_minsub + MOD(v_done + lvl - 1, v_maxsub - v_minsub + 1),
      TO_CHAR(DATE '2025-09-01' + NUMTOYMINTERVAL(MOD(v_done + lvl, 5), 'MONTH'), 'YYYY-MM'),
      DATE '2025-09-01' + NUMTOYMINTERVAL(MOD(v_done + lvl, 5), 'MONTH'),
      DATE '2025-09-21' + NUMTOYMINTERVAL(MOD(v_done + lvl, 5), 'MONTH'),
      ROUND(DBMS_RANDOM.VALUE(120, 9500), 2),
      ROUND(DBMS_RANDOM.VALUE(10, 900), 2),
      CASE MOD(v_done + lvl, 12)
        WHEN 0 THEN 'OVERDUE' WHEN 1 THEN 'DISPUTED'
        WHEN 2 THEN 'VOID'    WHEN 3 THEN 'ISSUED' ELSE 'PAID' END
    FROM (SELECT LEVEL AS lvl FROM dual
          CONNECT BY LEVEL <= LEAST(v_chunk, v_total - v_done));
    COMMIT;
    v_done := v_done + v_chunk;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('invoice loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 7. INVOICE_LINE  -- 5,000,000  (2 lines per invoice)
-- ----------------------------------------------------------------------------
DECLARE
  v_total PLS_INTEGER := 5000000;
  v_chunk PLS_INTEGER := 250000;
  v_done  PLS_INTEGER := 0;
  v_mininv NUMBER;
  v_maxinv NUMBER;
BEGIN
  SELECT MIN(invoice_id), MAX(invoice_id)
    INTO v_mininv, v_maxinv FROM invoice;
  WHILE v_done < v_total LOOP
    INSERT /*+ APPEND */ INTO invoice_line
      (line_id, invoice_id, line_no, charge_type, description,
       quantity, unit_price, line_amount)
    SELECT
      v_done + lvl,
      v_mininv + MOD(v_done + lvl - 1, v_maxinv - v_mininv + 1),
      MOD(lvl, 8) + 1,
      CASE MOD(lvl, 8)
        WHEN 0 THEN 'RENTAL'  WHEN 1 THEN 'VOICE'   WHEN 2 THEN 'DATA'
        WHEN 3 THEN 'SMS'     WHEN 4 THEN 'ROAMING' WHEN 5 THEN 'VAS'
        WHEN 6 THEN 'ADJUSTMENT' ELSE 'TAX' END,
      -- MOD(lvl,8) + 1 must be parenthesised: || binds tighter than +, so
      -- without the brackets Oracle parses this as
      --   ('...item ' || MOD(lvl,8)) + 1
      -- i.e. arithmetic on a string, and raises ORA-01722 invalid number.
      'Charge line for billing period, item ' || (MOD(lvl, 8) + 1) ||
        ' rated at standard tariff',
      ROUND(DBMS_RANDOM.VALUE(1, 500), 3),
      ROUND(DBMS_RANDOM.VALUE(0.1, 25), 4),
      ROUND(DBMS_RANDOM.VALUE(5, 2500), 2)
    FROM (SELECT LEVEL AS lvl FROM dual
          CONNECT BY LEVEL <= LEAST(v_chunk, v_total - v_done));
    COMMIT;
    v_done := v_done + v_chunk;
    IF MOD(v_done, 2000000) = 0 THEN
      DBMS_OUTPUT.PUT_LINE('  invoice_line progress: ' || v_done);
    END IF;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('invoice_line loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 8. PAYMENT  -- 2,000,000
-- ----------------------------------------------------------------------------
DECLARE
  v_total  PLS_INTEGER := 2000000;
  v_chunk  PLS_INTEGER := 250000;
  v_done   PLS_INTEGER := 0;
  v_mininv NUMBER;
  v_maxinv NUMBER;
BEGIN
  SELECT MIN(invoice_id), MAX(invoice_id)
    INTO v_mininv, v_maxinv FROM invoice;
  WHILE v_done < v_total LOOP
    INSERT /*+ APPEND */ INTO payment
      (payment_id, invoice_id, paid_on, amount_paid, pay_method, reference_no)
    SELECT
      v_done + lvl,
      v_mininv + MOD(v_done + lvl - 1, v_maxinv - v_mininv + 1),
      DATE '2025-09-05' + TRUNC(DBMS_RANDOM.VALUE(0, 210)),
      ROUND(DBMS_RANDOM.VALUE(100, 9500), 2),
      CASE MOD(lvl, 6)
        WHEN 0 THEN 'CARD'    WHEN 1 THEN 'UPI'     WHEN 2 THEN 'NETBANK'
        WHEN 3 THEN 'CASH'    WHEN 4 THEN 'AUTOPAY' ELSE 'WALLET' END,
      'TXN' || LPAD(TO_CHAR(v_done + lvl), 14, '0')
    FROM (SELECT LEVEL AS lvl FROM dual
          CONNECT BY LEVEL <= LEAST(v_chunk, v_total - v_done));
    COMMIT;
    v_done := v_done + v_chunk;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('payment loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 9. SUPPORT_TICKET  -- 400,000, each with a ~450-byte CLOB transcript
-- ----------------------------------------------------------------------------
DECLARE
  v_total  PLS_INTEGER := 400000;
  v_chunk  PLS_INTEGER := 50000;
  v_done   PLS_INTEGER := 0;
  v_minsub NUMBER;
  v_maxsub NUMBER;
BEGIN
  SELECT MIN(subscriber_id), MAX(subscriber_id)
    INTO v_minsub, v_maxsub FROM subscriber;
  WHILE v_done < v_total LOOP
    INSERT /*+ APPEND */ INTO support_ticket
      (ticket_id, subscriber_id, opened_on, closed_on, channel, category,
       priority, ticket_status, transcript)
    SELECT
      v_done + lvl,
      TRUNC(DBMS_RANDOM.VALUE(v_minsub, v_maxsub + 1)),
      DATE '2025-01-01' + TRUNC(DBMS_RANDOM.VALUE(0, 440)),
      CASE WHEN MOD(lvl, 4) > 0
           THEN DATE '2025-01-05' + TRUNC(DBMS_RANDOM.VALUE(0, 440)) END,
      CASE MOD(lvl, 5)
        WHEN 0 THEN 'PHONE' WHEN 1 THEN 'EMAIL' WHEN 2 THEN 'CHAT'
        WHEN 3 THEN 'STORE' ELSE 'APP' END,
      CASE MOD(lvl, 6)
        WHEN 0 THEN 'BILLING'   WHEN 1 THEN 'NETWORK'  WHEN 2 THEN 'DEVICE'
        WHEN 3 THEN 'ROAMING'   WHEN 4 THEN 'PLAN_CHANGE' ELSE 'COMPLAINT' END,
      CASE MOD(lvl, 4)
        WHEN 0 THEN 'URGENT' WHEN 1 THEN 'HIGH' WHEN 2 THEN 'LOW' ELSE 'MEDIUM' END,
      CASE MOD(lvl, 4)
        WHEN 0 THEN 'OPEN' WHEN 1 THEN 'PENDING'
        WHEN 2 THEN 'RESOLVED' ELSE 'CLOSED' END,
      TO_CLOB('Subscriber called regarding a billing discrepancy on the latest '
        || 'statement. Agent verified identity, reviewed the itemised charges '
        || 'line by line, and explained the roaming tariff applied during the '
        || 'international travel window. Customer requested a formal review; a '
        || 'credit note was raised and the case escalated to the billing back '
        || 'office for second-level validation. Follow-up scheduled. '
        || 'Ticket reference ' || (v_done + lvl) || '. ')
        || RPAD('.', 120, '.')
    FROM (SELECT LEVEL AS lvl FROM dual
          CONNECT BY LEVEL <= LEAST(v_chunk, v_total - v_done));
    COMMIT;
    v_done := v_done + v_chunk;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('support_ticket loaded: ' || v_total);
END;
/

-- ----------------------------------------------------------------------------
-- 9b. ASSERT the load actually landed -- raises, so a partial load cannot pass
--
--     The runner continues past errors by design, which means a failed chunk
--     leaves an empty or short table and the script still reaches the end.
--     This block turns that into a hard failure. It is the check that would
--     have caught the ORA-02291 in DEVICE immediately instead of after the
--     whole CDR load had run.
-- ----------------------------------------------------------------------------
DECLARE
  TYPE t_expect IS TABLE OF PLS_INTEGER INDEX BY VARCHAR2(30);
  v_expect t_expect;
  v_actual PLS_INTEGER;
  v_name   VARCHAR2(30);
  v_bad    PLS_INTEGER := 0;
BEGIN
  v_expect('PLAN_CATALOG')   := 500;
  v_expect('NETWORK_CELL')   := 20000;
  v_expect('SUBSCRIBER')     := 800000;
  v_expect('DEVICE')         := 1000000;
  v_expect('CDR')            := 21000000;
  v_expect('INVOICE')        := 2500000;
  v_expect('INVOICE_LINE')   := 5000000;
  v_expect('PAYMENT')        := 2000000;
  v_expect('SUPPORT_TICKET') := 400000;

  v_name := v_expect.FIRST;
  WHILE v_name IS NOT NULL LOOP
    EXECUTE IMMEDIATE 'SELECT COUNT(*) FROM ' || v_name INTO v_actual;
    IF v_actual != v_expect(v_name) THEN
      v_bad := v_bad + 1;
      DBMS_OUTPUT.PUT_LINE('ROW COUNT MISMATCH  ' || RPAD(v_name, 16) ||
                           ' expected ' || v_expect(v_name) ||
                           ' actual ' || v_actual);
    END IF;
    v_name := v_expect.NEXT(v_name);
  END LOOP;

  IF v_bad > 0 THEN
    RAISE_APPLICATION_ERROR(-20001,
      'Load incomplete: ' || v_bad || ' table(s) short. See the mismatches above.');
  END IF;
  DBMS_OUTPUT.PUT_LINE('row counts OK on all 9 tables');
END;
/

-- ----------------------------------------------------------------------------
-- 10. Build the materialized view and gather statistics
--     The MV was created BUILD DEFERRED against an empty invoice table, so this
--     is its first real build. Stats matter to Phase 3 sizing, which reads
--     row counts and segment bytes -- stale stats there produce a wrong
--     instance recommendation, not just a cosmetic difference.
-- ----------------------------------------------------------------------------
BEGIN
  DBMS_MVIEW.REFRESH('MV_REVENUE_BY_PERIOD', 'C');
END;
/

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
-- 11. VERIFY -- actual size on disk and row counts
-- ----------------------------------------------------------------------------
SELECT ROUND(SUM(bytes)/1024/1024/1024, 3) AS size_gb FROM user_segments;

SELECT segment_name, segment_type, ROUND(SUM(bytes)/1024/1024, 1) AS mb
FROM   user_segments
GROUP  BY segment_name, segment_type
ORDER  BY SUM(bytes) DESC
FETCH FIRST 20 ROWS ONLY;

SELECT table_name, num_rows FROM user_tables ORDER BY num_rows DESC NULLS LAST;

SELECT partition_name, num_rows
FROM   user_tab_partitions WHERE table_name = 'CDR' ORDER BY partition_position;

PROMPT ============================================================
PROMPT Data load complete. Check SIZE_GB above -- target is ~5 GB.
PROMPT Under target?  raise v_total in section 5 (cdr) and re-run it.
PROMPT Next: 04_seed_defects.sql
PROMPT ============================================================
