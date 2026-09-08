-- ============================================================================
-- 01_setup_admin.sql   (REVISED - run as SYSTEM, not SYS)
--
-- CONNECTION REQUIRED:
--   Username : system
--   Password : (the password you set when installing XE)
--   Role     : default          <-- NOT SYSDBA
--   Hostname : localhost
--   Port     : 1521
--   Service name : XEPDB1       <-- Service name, NOT SID
--
-- Verify before running:
--   SELECT sys_context('USERENV','CON_NAME') FROM dual;   -- must return XEPDB1
--
-- Run with F5 (Run Script) and NOTHING highlighted in the worksheet.
-- ============================================================================

SET SERVEROUTPUT ON
SET DEFINE OFF
WHENEVER SQLERROR CONTINUE

-- ----------------------------------------------------------------------------
-- 0. CLEANUP - safe on a fresh database; "does not exist" errors here
--    are expected and can be ignored.
-- ----------------------------------------------------------------------------
DROP USER dbmig_app CASCADE;
DROP USER dbmig_rpt CASCADE;
DROP USER dbmig_collector CASCADE;
DROP ROLE dbmig_read_role;
DROP ROLE dbmig_write_role;
DROP PROFILE dbmig_app_profile CASCADE;
DROP DIRECTORY dbmig_ext_dir;

-- ----------------------------------------------------------------------------
-- 1. TABLESPACE - using the built-in USERS tablespace.
--    No CREATE TABLESPACE, no datafile paths, nothing OS-specific to get wrong.
--    This just makes sure USERS can grow past 1 GB.
-- ----------------------------------------------------------------------------
BEGIN
  FOR r IN (SELECT file_name FROM dba_data_files WHERE tablespace_name = 'USERS')
  LOOP
    EXECUTE IMMEDIATE 'ALTER DATABASE DATAFILE ''' || r.file_name ||
                      ''' AUTOEXTEND ON NEXT 100M MAXSIZE 4G';
  END LOOP;
END;
/

-- ----------------------------------------------------------------------------
-- 2. PROFILE   ("Profile" on the object checklist)
-- ----------------------------------------------------------------------------
CREATE PROFILE dbmig_app_profile LIMIT
  SESSIONS_PER_USER        UNLIMITED
  FAILED_LOGIN_ATTEMPTS    10
  PASSWORD_LIFE_TIME       UNLIMITED
  PASSWORD_REUSE_TIME      UNLIMITED
  IDLE_TIME                UNLIMITED
  CPU_PER_SESSION          UNLIMITED;

-- ----------------------------------------------------------------------------
-- 3. USERS   ("User" / "Schema" - in Oracle a schema IS a user)
-- ----------------------------------------------------------------------------
CREATE USER dbmig_app IDENTIFIED BY DbMig2026App
  DEFAULT TABLESPACE users
  TEMPORARY TABLESPACE temp
  QUOTA UNLIMITED ON users
  PROFILE dbmig_app_profile;

CREATE USER dbmig_rpt IDENTIFIED BY DbMig2026Rpt
  DEFAULT TABLESPACE users
  TEMPORARY TABLESPACE temp
  QUOTA 200M ON users
  PROFILE dbmig_app_profile;

-- Read-only discovery account - mirrors the production collector pattern
CREATE USER dbmig_collector IDENTIFIED BY DbMig2026Coll
  DEFAULT TABLESPACE users
  TEMPORARY TABLESPACE temp
  PROFILE dbmig_app_profile;

-- ----------------------------------------------------------------------------
-- 4. ROLES + SYSTEM PRIVILEGES + GRANTS
--    ("Role", "Grant", "Privilege", "System Privileges")
-- ----------------------------------------------------------------------------
CREATE ROLE dbmig_read_role;
CREATE ROLE dbmig_write_role;

GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE SEQUENCE,
      CREATE PROCEDURE, CREATE TRIGGER, CREATE TYPE, CREATE SYNONYM,
      CREATE MATERIALIZED VIEW, CREATE JOB, CREATE DATABASE LINK
  TO dbmig_app;

GRANT EXECUTE ON DBMS_AQADM TO dbmig_app;
GRANT EXECUTE ON DBMS_XMLSCHEMA TO dbmig_app;
GRANT CTXAPP TO dbmig_app;

GRANT CREATE SESSION, CREATE DATABASE LINK TO dbmig_rpt;

GRANT CREATE SESSION TO dbmig_collector;
GRANT SELECT_CATALOG_ROLE TO dbmig_collector;

GRANT CREATE SESSION TO dbmig_read_role;
GRANT dbmig_read_role  TO dbmig_rpt;
GRANT dbmig_write_role TO dbmig_app;

-- ----------------------------------------------------------------------------
-- 5. DIRECTORY   ("Directory", and required by the External Table in script 2)
--
--    >>> BEFORE RUNNING: create this folder in Windows Explorer <<<
--        C:\oracle\ext_data
--    If you use a different path, change it on the line below.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE DIRECTORY dbmig_ext_dir AS 'C:\oracle\ext_data';

GRANT READ, WRITE ON DIRECTORY dbmig_ext_dir TO dbmig_app;
GRANT READ        ON DIRECTORY dbmig_ext_dir TO dbmig_collector;

-- ----------------------------------------------------------------------------
-- 6. VERIFY - all three should return rows
-- ----------------------------------------------------------------------------
SELECT username, default_tablespace, profile
FROM   dba_users
WHERE  username LIKE 'DBMIG%'
ORDER  BY username;

SELECT role FROM dba_roles WHERE role LIKE 'DBMIG%';

SELECT directory_name, directory_path
FROM   dba_directories
WHERE  directory_name = 'DBMIG_EXT_DIR';

PROMPT =============================================================
PROMPT Script 1 complete.
PROMPT Next: create a NEW SQL Developer connection --
PROMPT   Username : dbmig_app
PROMPT   Password : DbMig2026App
PROMPT   Role     : default
PROMPT   Service name : XEPDB1
PROMPT Then open 02_schema_objects.sql in THAT connection and press F5.
PROMPT =============================================================
