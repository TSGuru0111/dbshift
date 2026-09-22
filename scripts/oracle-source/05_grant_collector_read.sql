-- =====================================================================
-- 05_grant_collector_read.sql
-- Grants row-level SELECT on application tables to DBMIG_COLLECTOR.
--
-- WHY THIS EXISTS
-- SELECT_CATALOG_ROLE grants dictionary access, not data access. The
-- collector can read metadata ABOUT dbmig_app.customer but cannot SELECT
-- from it (ORA-00942). Data-quality profiling -- duplicate counts on
-- near-unique columns, non-ASCII character scans -- needs row access.
--
-- Deliberately NOT `GRANT SELECT ANY TABLE`. That is a database-wide
-- privilege a client DBA is right to refuse. Explicit per-table grants are
-- narrow, auditable, and revocable table by table.
--
-- RUN AS: dbmig_app  (it owns the tables, so it can grant on them)
--   sqlplus dbmig_app/DbMig2026App@localhost:1521/XEPDB1
-- Or SQL Developer with F5 and nothing highlighted.
--
-- Re-runnable. Granting an existing privilege is a no-op, not an error.
-- =====================================================================

SET SERVEROUTPUT ON

DECLARE
  v_granted PLS_INTEGER := 0;
  v_skipped PLS_INTEGER := 0;
BEGIN
  FOR t IN (
    SELECT table_name
    FROM   user_tables
    WHERE  table_name NOT LIKE 'DR$%'      -- Oracle Text index internals
    AND    table_name NOT LIKE 'AQ$%'      -- Advanced Queuing internals
    AND    table_name NOT LIKE 'MLOG$%'    -- materialized view log
    AND    table_name NOT LIKE 'RUPD$%'
    AND    table_name NOT LIKE 'SYS_IOT%'
    ORDER  BY table_name
  ) LOOP
    BEGIN
      EXECUTE IMMEDIATE 'GRANT SELECT ON "' || t.table_name || '" TO dbmig_collector';
      v_granted := v_granted + 1;
    EXCEPTION
      WHEN OTHERS THEN
        v_skipped := v_skipped + 1;
        DBMS_OUTPUT.PUT_LINE('  skipped ' || t.table_name || ' -> ' || SQLERRM);
    END;
  END LOOP;

  DBMS_OUTPUT.PUT_LINE('granted SELECT on ' || v_granted || ' tables, skipped ' || v_skipped);
END;
/

-- Verify. Do not trust the output above -- count the actual privileges.
SELECT COUNT(*) AS granted_tables
FROM   user_tab_privs
WHERE  grantee = 'DBMIG_COLLECTOR'
AND    privilege = 'SELECT';

-- ---------------------------------------------------------------------
-- To revoke everything this script granted, run as dbmig_app:
--
--   BEGIN
--     FOR p IN (SELECT table_name FROM user_tab_privs
--               WHERE grantee='DBMIG_COLLECTOR' AND privilege='SELECT') LOOP
--       EXECUTE IMMEDIATE 'REVOKE SELECT ON "'||p.table_name||'" FROM dbmig_collector';
--     END LOOP;
--   END;
--   /
-- ---------------------------------------------------------------------
