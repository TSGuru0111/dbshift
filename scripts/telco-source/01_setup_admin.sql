-- ============================================================================
-- 01_setup_admin.sql  —  TELCO source estate (second, independent estate)
--
-- Run as SYSTEM connected to XEPDB1 (service name, NOT the SID XE).
--   Verify first:  SELECT sys_context('USERENV','CON_NAME') FROM dual;  -- XEPDB1
--
-- This builds a SECOND source estate alongside DBMIG_APP. It does not touch
-- DBMIG_APP, DBMIG_RPT or DBMIG_REHEARSAL in any way.
--
-- WHY A DEDICATED TABLESPACE: the USERS tablespace on this instance is a
-- smallfile datafile capped at MAXSIZE 4G, of which ~2.25 GB is already
-- allocated to the existing estates. A 5 GB schema cannot fit there. This
-- creates a BIGFILE tablespace instead — one datafile, autoextending to 20 GB,
-- which is well inside XE's 12 GB user-data limit for the data we actually load
-- and leaves the existing estates untouched in USERS.
-- ============================================================================

SET SERVEROUTPUT ON
SET DEFINE OFF
WHENEVER SQLERROR CONTINUE

-- ----------------------------------------------------------------------------
-- 1. Dedicated bigfile tablespace
--    Bigfile: a single datafile, so there is no "add another datafile" step
--    when the load grows. 8 KB blocks give a bigfile a 32 TB ceiling; the
--    MAXSIZE below is the real limit and is deliberate.
-- ----------------------------------------------------------------------------
CREATE BIGFILE TABLESPACE dbmig_telco_ts
  DATAFILE 'C:\APP\GANITADMIN\PRODUCT\21C\ORADATA\XE\XEPDB1\DBMIG_TELCO01.DBF'
  SIZE 512M AUTOEXTEND ON NEXT 256M MAXSIZE 20G
  EXTENT MANAGEMENT LOCAL AUTOALLOCATE
  SEGMENT SPACE MANAGEMENT AUTO;

-- ----------------------------------------------------------------------------
-- 2. Application owner
-- ----------------------------------------------------------------------------
CREATE USER dbmig_telco IDENTIFIED BY DbMig2026Telco
  DEFAULT TABLESPACE dbmig_telco_ts
  QUOTA UNLIMITED ON dbmig_telco_ts;

GRANT CREATE SESSION            TO dbmig_telco;
GRANT CREATE TABLE              TO dbmig_telco;
GRANT CREATE VIEW               TO dbmig_telco;
GRANT CREATE SEQUENCE           TO dbmig_telco;
GRANT CREATE PROCEDURE          TO dbmig_telco;
GRANT CREATE TRIGGER            TO dbmig_telco;
GRANT CREATE TYPE               TO dbmig_telco;
GRANT CREATE SYNONYM            TO dbmig_telco;
GRANT CREATE MATERIALIZED VIEW  TO dbmig_telco;
GRANT CREATE JOB                TO dbmig_telco;
GRANT CREATE DATABASE LINK      TO dbmig_telco;

-- ----------------------------------------------------------------------------
-- 3. Read access for the discovery collector
--    SELECT_CATALOG_ROLE is metadata only. Row-level profiling (Phase 1's
--    dataprofile probe, and several DQ rules in Phase 2) needs real SELECT on
--    the tables. This is the same lesson learned on DBMIG_APP — see
--    docs/03-source-estate.md "Two things that bite".
--
--    NOT "GRANT SELECT ANY TABLE": that is a system privilege covering every
--    schema in the database, including DBMIG_APP, and would silently widen the
--    collector's reach well beyond this estate. The collector is supposed to be
--    a least-privilege read-only account and the assessment reports on grant
--    sprawl — handing it ANY-privileges to save typing would corrupt the very
--    thing Phase 2 measures. Per-table grants are issued at the end of
--    02_schema_objects.sql, once the tables exist.
-- ----------------------------------------------------------------------------
-- (no schema-level data grant here — see 02_schema_objects.sql section 14)

-- ----------------------------------------------------------------------------
-- 4. Verify
-- ----------------------------------------------------------------------------
SELECT tablespace_name, bigfile,
       ROUND(bytes/1024/1024/1024, 3)    AS alloc_gb,
       ROUND(maxbytes/1024/1024/1024, 1) AS max_gb
FROM   dba_data_files JOIN dba_tablespaces USING (tablespace_name)
WHERE  tablespace_name = 'DBMIG_TELCO_TS';

SELECT username, default_tablespace, oracle_maintained
FROM   dba_users WHERE username = 'DBMIG_TELCO';

PROMPT ============================================================
PROMPT 01_setup_admin complete. Expect one tablespace row and one
PROMPT user row above. Next: 02_schema_objects.sql AS dbmig_telco.
PROMPT ============================================================
