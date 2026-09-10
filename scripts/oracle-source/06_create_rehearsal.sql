-- =====================================================================
-- 06_create_rehearsal.sql
-- Creates the rehearsal schema that Phase 4's dry-run gate applies fixes to.
--
-- WHY THIS EXISTS
-- Gate 4 of five applies every proposed fix AND its rollback to a copy of the
-- estate before the fix is allowed near production. A fix whose rollback fails
-- is worse than no fix, and this is the only place that can be discovered
-- safely. Without this schema the gate reports BLOCKED and nothing can be
-- applied -- which is the correct behaviour, not a bug.
--
-- RUN AS: system  (creating a user needs a privilege dbmig_app does not have)
--   sqlplus system/<password>@localhost:1521/XEPDB1 @06_create_rehearsal.sql
--
-- Re-runnable. Expect "user does not exist" on a first run.
-- =====================================================================

SET SERVEROUTPUT ON

-- Cleanup block, so a re-run starts from a known state.
BEGIN
  EXECUTE IMMEDIATE 'DROP USER dbmig_rehearsal CASCADE';
  DBMS_OUTPUT.PUT_LINE('dropped existing dbmig_rehearsal');
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE = -1918 THEN
      DBMS_OUTPUT.PUT_LINE('dbmig_rehearsal did not exist, continuing');
    ELSE
      RAISE;
    END IF;
END;
/

CREATE USER dbmig_rehearsal IDENTIFIED BY DbMig2026Reh;

-- Enough to own a copy of the estate and to have fixes applied to it.
-- Deliberately no CREATE USER, no DBA, nothing that reaches outside itself.
GRANT CREATE SESSION            TO dbmig_rehearsal;
GRANT CREATE TABLE              TO dbmig_rehearsal;
GRANT CREATE INDEX              TO dbmig_rehearsal;
GRANT CREATE VIEW               TO dbmig_rehearsal;
GRANT CREATE SEQUENCE           TO dbmig_rehearsal;
GRANT CREATE PROCEDURE          TO dbmig_rehearsal;
GRANT CREATE TRIGGER            TO dbmig_rehearsal;
GRANT CREATE TYPE               TO dbmig_rehearsal;
GRANT CREATE SYNONYM            TO dbmig_rehearsal;
GRANT CREATE MATERIALIZED VIEW  TO dbmig_rehearsal;
GRANT ANALYZE ANY               TO dbmig_rehearsal;   -- DBMS_STATS fixes
ALTER USER dbmig_rehearsal QUOTA UNLIMITED ON USERS;

-- Verify. Do not trust the output above.
SELECT username, account_status FROM dba_users WHERE username = 'DBMIG_REHEARSAL';

-- ---------------------------------------------------------------------
-- NEXT: import a copy of the estate into it.
-- Run from a Command Prompt, not from SQL Developer:
--
--   impdp system/<password>@localhost:1521/XEPDB1 ^
--     directory=DBMIG_EXT_DIR dumpfile=dbmig_golden.dmp ^
--     remap_schema=DBMIG_APP:DBMIG_REHEARSAL ^
--     remap_tablespace=USERS:USERS ^
--     logfile=rehearsal_import.log
--
-- Expect some ORA-39083 errors on objects that reference the original schema
-- by name (the database link, the Text index). They do not matter -- the
-- rehearsal copy exists to receive DDL fixes, not to be a working application.
--
-- Then verify the copy is populated:
--   SELECT COUNT(*) FROM all_objects WHERE owner = 'DBMIG_REHEARSAL';
--
-- Then point the tool at it:
--   $env:DBSHIFT_REHEARSAL_DSN='localhost:1521/XEPDB1'
--   $env:DBSHIFT_REHEARSAL_PASSWORD='DbMig2026Reh'
--   python -m remediate.run
-- ---------------------------------------------------------------------
