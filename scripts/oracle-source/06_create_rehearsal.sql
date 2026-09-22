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
--
-- Two things learned the hard way, 2026-09-10:
--   * There is no CREATE INDEX system privilege. Only CREATE ANY INDEX exists,
--     and it is not needed: an owner may index its own tables. Granting it
--     would let the rehearsal schema index someone else's.
--   * Never put a trailing -- comment on the same line as a GRANT. SQL*Plus
--     folds it into the statement and it fails with ORA-00990, which reads
--     exactly like an invalid privilege name and sends you the wrong way.
GRANT CREATE SESSION            TO dbmig_rehearsal;
GRANT CREATE TABLE              TO dbmig_rehearsal;
GRANT CREATE VIEW               TO dbmig_rehearsal;
GRANT CREATE SEQUENCE           TO dbmig_rehearsal;
GRANT CREATE PROCEDURE          TO dbmig_rehearsal;
GRANT CREATE TRIGGER            TO dbmig_rehearsal;
GRANT CREATE TYPE               TO dbmig_rehearsal;
GRANT CREATE SYNONYM            TO dbmig_rehearsal;
GRANT CREATE MATERIALIZED VIEW  TO dbmig_rehearsal;
-- DBMS_STATS on its own tables needs no grant; this covers stats fixes aimed
-- at objects the import could not remap into the rehearsal schema.
GRANT ANALYZE ANY               TO dbmig_rehearsal;
ALTER USER dbmig_rehearsal QUOTA UNLIMITED ON USERS;

-- Verify. Do not trust the output above.
--
-- The quota check is not decoration. On 2026-09-10 the ALTER USER above was
-- silently swallowed by SQL*Plus and the script still printed a healthy-looking
-- OPEN account -- then the 602 MB import failed with ORA-01950 on every table,
-- 39 errors deep, before anyone knew the quota was missing. A verify block that
-- only reports what is easy to report is how that happens.
SELECT username, account_status FROM dba_users WHERE username = 'DBMIG_REHEARSAL';

DECLARE
  n_privs  PLS_INTEGER;
  n_quota  PLS_INTEGER;
BEGIN
  SELECT COUNT(*) INTO n_privs FROM dba_sys_privs WHERE grantee = 'DBMIG_REHEARSAL';
  SELECT COUNT(*) INTO n_quota FROM dba_ts_quotas WHERE username = 'DBMIG_REHEARSAL';

  DBMS_OUTPUT.PUT_LINE('system privileges : ' || n_privs || ' (expected 10)');
  DBMS_OUTPUT.PUT_LINE('tablespace quotas : ' || n_quota || ' (expected 1)');

  IF n_quota = 0 THEN
    RAISE_APPLICATION_ERROR(-20001,
      'NO TABLESPACE QUOTA. The import would fail with ORA-01950 on every '
      || 'table. Fix the ALTER USER above before running impdp.');
  ELSIF n_privs < 10 THEN
    RAISE_APPLICATION_ERROR(-20002,
      'Only ' || n_privs || ' of 10 privileges granted. Check for errors above.');
  END IF;

  DBMS_OUTPUT.PUT_LINE('rehearsal schema is ready for impdp');
END;
/

-- ---------------------------------------------------------------------
-- NEXT: import a copy of the estate into it.
--
-- impdp is an operating-system program, NOT a SQL command. Type EXIT to leave
-- SQL*Plus first, or you get SP2-0734: unknown command beginning "impdp syst".
-- The dump already lives in C:\oracle\ext_data, which is what DBMIG_EXT_DIR
-- points at, so dumpfile= takes the bare name.
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
