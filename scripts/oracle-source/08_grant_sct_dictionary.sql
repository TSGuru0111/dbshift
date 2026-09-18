-- =====================================================================
-- 08_grant_sct_dictionary.sql
-- Grants SELECT ANY DICTIONARY to DBMIG_COLLECTOR, for AWS SCT.
--
-- WHY THIS EXISTS
-- AWS SCT refuses to connect without it. Proved on 2026-09-17: SCT 1.0.677
-- established the JDBC connection, ran its own 'check-privileges' query, and
-- then failed AddSource with
--
--   DbLoaderInsufficientPrivilegesException: The specified account
--   (dbmig_collector) does not have sufficient privileges for working with
--   the following object(s): ORACLE Server : [SELECT ANY DICTIONARY]
--
-- SELECT_CATALOG_ROLE is not a substitute, and this is the second time that
-- distinction has cost this project time -- see 05_grant_collector_read.sql,
-- where the same role turned out not to grant row access either. SCT checks
-- for the *privilege* explicitly, not for equivalent reach, so holding the
-- role changes nothing.
--
-- WHAT A CLIENT DBA SHOULD KNOW BEFORE RUNNING THIS
-- SELECT ANY DICTIONARY is wider than SELECT_CATALOG_ROLE in one way that
-- matters: it includes SYS-owned tables such as USER$ (which carries password
-- hashes) and LINK$ (database link credentials). It grants no access to
-- application data -- that still needs the explicit per-table grants in
-- script 05 -- but it is a privilege worth naming in a change request rather
-- than slipping into one.
--
-- It is AWS's documented requirement for running SCT against Oracle, not a
-- DBShift preference. A DBA who refuses it is refusing SCT, which is a
-- legitimate position: the DBShift 50-rule engine in `assess/` runs on
-- SELECT_CATALOG_ROLE alone and produces its own assessment.
--
-- RUN AS: a user that can grant it -- system, or the admin account
--   sqlplus system/<password>@localhost:1521/XEPDB1
--
-- Re-runnable. Granting an existing privilege is a no-op, not an error.
-- =====================================================================

SET SERVEROUTPUT ON

DECLARE
  v_user   VARCHAR2(128) := 'DBMIG_COLLECTOR';
  v_exists PLS_INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_exists FROM dba_users WHERE username = v_user;
  IF v_exists = 0 THEN
    RAISE_APPLICATION_ERROR(-20001,
      'User ' || v_user || ' does not exist. Run 01_setup_admin.sql first.');
  END IF;

  EXECUTE IMMEDIATE 'GRANT SELECT ANY DICTIONARY TO ' || v_user;
  DBMS_OUTPUT.PUT_LINE('Granted SELECT ANY DICTIONARY to ' || v_user);

  -- Report what the account now holds, so the result is visible rather than
  -- assumed. SCT's own check is the only thing that finally settles it.
  FOR p IN (
    SELECT privilege
    FROM   dba_sys_privs
    WHERE  grantee = v_user
    ORDER  BY privilege
  ) LOOP
    DBMS_OUTPUT.PUT_LINE('  privilege: ' || p.privilege);
  END LOOP;

  FOR r IN (
    SELECT granted_role
    FROM   dba_role_privs
    WHERE  grantee = v_user
    ORDER  BY granted_role
  ) LOOP
    DBMS_OUTPUT.PUT_LINE('  role     : ' || r.granted_role);
  END LOOP;
END;
/

-- Verify from SCT's side, not just Oracle's:
--   python -m sct.run --target rds-postgresql --force
-- AddSource must reach 'Connection ... was established' AND get past the
-- check-privileges query. A connection alone is not success -- on 2026-09-17
-- SCT connected fine and still refused.
