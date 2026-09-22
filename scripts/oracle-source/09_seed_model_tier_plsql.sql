-- =====================================================================
-- 09_seed_model_tier_plsql.sql
-- PL/SQL that the deterministic rules cannot convert, so Phase 4b routes it
-- to the model tier.
--
-- WHY THIS EXISTS
-- Asked for on 2026-09-17: "I need model only, not just deterministic or
-- static content." The model tier was already live and wired -- Sonnet 4.6 on
-- both tiers -- but a run against DBMIG_APP and DBMIG_TELCO converted 15 of 15
-- convertible objects by rule and sent **zero** to the model. That is a true
-- statement about the estate, not a broken agent: every construct in the
-- existing stored code sits in the deterministic tier.
--
-- So the demo could not show AI conversion, because there was nothing for the
-- AI to do. These objects fix that, and they are not contrivances: each one
-- uses a construct `convert/constructs.json` already classifies as tier=model,
-- meaning a faithful translation needs judgement rather than substitution.
--
-- WHAT EACH OBJECT EXERCISES, and why a rule cannot do it
--
--   FN_CUSTOMER_HIERARCHY   CONNECT BY / START WITH / PRIOR / LEVEL
--       -> WITH RECURSIVE. The shape of the recursive CTE depends on the
--          hierarchy's direction and its termination condition; there is no
--          mechanical rewrite.
--   SP_LOAN_RISK_ROLLUP     BULK COLLECT, FORALL, INDEX BY, SAVEPOINT
--       -> a set-based statement. Whether the loop can become one UPDATE, or
--          needs an array, or must stay row-by-row, is a reading of intent.
--   FN_LOAN_SUMMARY_TEXT    LISTAGG, DECODE, NVL2, TO_CHAR format models
--       -> string_agg + CASE. Oracle and PostgreSQL date format models differ
--          in ways a substitution table gets wrong.
--   SP_AUDIT_DYNAMIC        EXECUTE IMMEDIATE with concatenated SQL
--       -> EXECUTE ... USING. Binding the values rather than concatenating
--          them is a correctness *and* injection decision.
--   FN_TXN_CURSOR           REF CURSOR, CURSOR ... IS, ROWNUM
--       -> refcursor + LIMIT. Cursor semantics differ across the engines.
--   SP_MERGE_CUSTOMER       MERGE INTO ... WHEN MATCHED / WHEN NOT MATCHED
--       -> INSERT ... ON CONFLICT, or MERGE on PostgreSQL 15+. Which one is
--          right depends on the target version and the constraint involved.
--
-- These are DBMIG_APP objects, so they enter the same discovery, assessment,
-- conversion and gate paths as everything else. **Nothing is applied to the
-- target by this script** -- Phase 4b compiles conversions on PostgreSQL inside
-- a transaction and rolls them back.
--
-- RUN AS: dbmig_app (it owns the referenced tables)
--   sqlplus dbmig_app/<password>@localhost:1521/XEPDB1 @09_seed_model_tier_plsql.sql
--
-- Re-runnable: every object is CREATE OR REPLACE.
--
-- AFTER RUNNING: re-run discovery, then Phase 4b with the model live.
--   python -m collector.run
--   python -m convert.run --model-mode live
-- Expect these six to route MODEL, and each conversion to carry
-- source="bedrock" with the model id that wrote it.
-- =====================================================================

SET DEFINE OFF
SET SERVEROUTPUT ON

PROMPT Seeding PL/SQL that needs the model tier...

-- ---------------------------------------------------------------------
-- 1. CONNECT BY -> WITH RECURSIVE
--    A referral hierarchy over CUSTOMER. Rules decline because the recursive
--    CTE's anchor, its recursive term and its stop condition are all readings
--    of what the hierarchy means, not tokens to swap.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION FN_CUSTOMER_HIERARCHY(
    p_root_customer IN NUMBER,
    p_max_depth     IN NUMBER DEFAULT 5
) RETURN VARCHAR2
IS
    v_path   VARCHAR2(4000) := '';
    v_depth  NUMBER := 0;
BEGIN
    -- LEVEL and PRIOR have no PostgreSQL equivalent; the depth guard has to
    -- become a condition inside the recursive term.
    FOR r IN (
        SELECT customer_id, full_name, LEVEL AS lvl
        FROM   customer
        START WITH customer_id = p_root_customer
        CONNECT BY PRIOR customer_id = TRUNC(customer_id / 10)
        ORDER SIBLINGS BY full_name
    ) LOOP
        EXIT WHEN r.lvl > p_max_depth;
        v_path  := v_path || LPAD(' ', (r.lvl - 1) * 2) || r.full_name || CHR(10);
        v_depth := GREATEST(v_depth, r.lvl);
    END LOOP;

    RETURN 'depth=' || v_depth || CHR(10) || v_path;
END FN_CUSTOMER_HIERARCHY;
/

-- ---------------------------------------------------------------------
-- 2. BULK COLLECT + FORALL + INDEX BY + SAVEPOINT
--    Whether this becomes one set-based UPDATE, an array round-trip, or stays
--    row-by-row is a judgement about volume and locking.
-- ---------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE SP_LOAN_RISK_ROLLUP(
    p_status   IN VARCHAR2,
    p_adjust   IN NUMBER,
    p_affected OUT NUMBER
)
IS
    TYPE t_loan_ids  IS TABLE OF loan.loan_id%TYPE;
    TYPE t_score_map IS TABLE OF NUMBER INDEX BY PLS_INTEGER;

    v_ids    t_loan_ids;
    v_scores t_score_map;
BEGIN
    SAVEPOINT before_rollup;

    SELECT loan_id
    BULK   COLLECT INTO v_ids
    FROM   loan
    WHERE  loan_status = p_status;

    FOR i IN 1 .. v_ids.COUNT LOOP
        v_scores(i) := p_adjust * i;
    END LOOP;

    FORALL i IN 1 .. v_ids.COUNT
        UPDATE loan
        SET    legacy_score = NVL(legacy_score, 0) + v_scores(i)
        WHERE  loan_id = v_ids(i);

    p_affected := v_ids.COUNT;
EXCEPTION
    WHEN OTHERS THEN
        ROLLBACK TO before_rollup;
        p_affected := 0;
        RAISE;
END SP_LOAN_RISK_ROLLUP;
/

-- ---------------------------------------------------------------------
-- 3. LISTAGG + DECODE + NVL2 + TO_CHAR format models
--    The format models are the trap: Oracle's and PostgreSQL's differ, and a
--    substitution table silently produces the wrong dates.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION FN_LOAN_SUMMARY_TEXT(
    p_customer_id IN NUMBER
) RETURN VARCHAR2
IS
    v_summary VARCHAR2(4000);
BEGIN
    SELECT LISTAGG(
               DECODE(l.loan_status,
                      'ACTIVE',  'A',
                      'CLOSED',  'C',
                      'DEFAULT', 'D',
                                 '?')
               || ':' || TO_CHAR(l.principal_amt, 'FM999G999D00')
               || '@' || TO_CHAR(l.disbursed_on, 'DD-MON-YYYY HH24:MI')
               || NVL2(l.legacy_score, ' [scored]', ' [unscored]'),
               ' | ')
           WITHIN GROUP (ORDER BY l.disbursed_on DESC)
    INTO   v_summary
    FROM   loan l
    WHERE  l.customer_id = p_customer_id;

    RETURN NVL(v_summary, 'no loans');
END FN_LOAN_SUMMARY_TEXT;
/

-- ---------------------------------------------------------------------
-- 4. EXECUTE IMMEDIATE with concatenated SQL
--    Converting this correctly means binding the values, which is a security
--    decision as much as a syntax one. A rule that mechanically rewrote the
--    concatenation would carry the injection across to the target.
-- ---------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE SP_AUDIT_DYNAMIC(
    p_table_name IN VARCHAR2,
    p_since      IN DATE,
    p_rows       OUT NUMBER
)
IS
    v_sql VARCHAR2(1000);
BEGIN
    v_sql := 'SELECT COUNT(*) FROM ' || p_table_name ||
             ' WHERE comm_date >= TO_DATE(''' ||
             TO_CHAR(p_since, 'YYYY-MM-DD') || ''', ''YYYY-MM-DD'')';

    EXECUTE IMMEDIATE v_sql INTO p_rows;

    EXECUTE IMMEDIATE
        'INSERT INTO audit_scratch (note) VALUES (:1)'
        USING 'counted ' || p_rows || ' rows in ' || p_table_name;
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE = -942 THEN
            p_rows := -1;
        ELSE
            RAISE;
        END IF;
END SP_AUDIT_DYNAMIC;
/

-- ---------------------------------------------------------------------
-- 5. REF CURSOR + explicit cursor + ROWNUM
--    Cursor lifetime and ROWNUM-vs-LIMIT both need a decision about whether
--    the caller or the function owns the result set.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION FN_TXN_CURSOR(
    p_loan_id IN NUMBER,
    p_limit   IN NUMBER DEFAULT 10
) RETURN SYS_REFCURSOR
IS
    v_cur SYS_REFCURSOR;

    CURSOR c_recent IS
        SELECT txn_id, txn_amount
        FROM   loan_txn
        WHERE  loan_id = p_loan_id
        ORDER  BY txn_date DESC;

    v_first_id NUMBER;
BEGIN
    OPEN c_recent;
    FETCH c_recent INTO v_first_id, v_first_id;
    CLOSE c_recent;

    OPEN v_cur FOR
        SELECT txn_id, txn_date, txn_amount, txn_type
        FROM   (SELECT txn_id, txn_date, txn_amount, txn_type
                FROM   loan_txn
                WHERE  loan_id = p_loan_id
                ORDER  BY txn_date DESC)
        WHERE  ROWNUM <= p_limit;

    RETURN v_cur;
END FN_TXN_CURSOR;
/

-- ---------------------------------------------------------------------
-- 6. MERGE INTO
--    INSERT ... ON CONFLICT needs to know which constraint arbitrates, and
--    PostgreSQL 15+ MERGE is a different rewrite again. The right answer
--    depends on the target version, which is Phase 3's decision.
-- ---------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE SP_MERGE_CUSTOMER(
    p_customer_id IN NUMBER,
    p_full_name   IN VARCHAR2,
    p_email       IN VARCHAR2
)
IS
BEGIN
    MERGE INTO customer c
    USING (SELECT p_customer_id AS customer_id,
                  p_full_name   AS full_name,
                  p_email       AS email
           FROM   dual) src
    ON (c.customer_id = src.customer_id)
    WHEN MATCHED THEN
        UPDATE SET c.full_name = src.full_name,
                   c.email     = src.email
        WHERE  c.status <> 'LOCKED'
    WHEN NOT MATCHED THEN
        INSERT (customer_id, full_name, email, status, created_at)
        VALUES (src.customer_id, src.full_name, src.email, 'ACTIVE', SYSDATE);
END SP_MERGE_CUSTOMER;
/

-- ---------------------------------------------------------------------
-- Report what compiled, so a silent failure cannot look like success. This
-- project has lost time to exactly that -- see docs/04-defects.md, where a
-- seed script appeared to run and changed nothing.
-- ---------------------------------------------------------------------
DECLARE
    v_ok   PLS_INTEGER := 0;
    v_bad  PLS_INTEGER := 0;
BEGIN
    FOR o IN (
        SELECT object_name, object_type, status
        FROM   user_objects
        WHERE  object_name IN ('FN_CUSTOMER_HIERARCHY', 'SP_LOAN_RISK_ROLLUP',
                               'FN_LOAN_SUMMARY_TEXT', 'SP_AUDIT_DYNAMIC',
                               'FN_TXN_CURSOR', 'SP_MERGE_CUSTOMER')
        ORDER  BY object_name
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(RPAD(o.object_type, 10) || RPAD(o.object_name, 26) || o.status);
        IF o.status = 'VALID' THEN v_ok := v_ok + 1; ELSE v_bad := v_bad + 1; END IF;
    END LOOP;

    DBMS_OUTPUT.PUT_LINE('---');
    DBMS_OUTPUT.PUT_LINE(v_ok || ' valid, ' || v_bad || ' invalid');
    IF v_ok < 6 THEN
        DBMS_OUTPUT.PUT_LINE(
            'Fewer than 6 valid objects. Run SHOW ERRORS for the failures -- an '
            || 'object that did not compile will not appear in discovery at all.');
    END IF;
END;
/
