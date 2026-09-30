-- ============================================================================
-- DBShift MySQL source estate -- 01: accounts, schemas and grants
--
-- Run as root (or any account with GRANT OPTION):
--     mysql -u root -p < 01_setup_admin.sql
--
-- Creates three schemas mirroring the Oracle estate's shape:
--     dbmig_mysql_app   the estate under migration
--     dbmig_mysql_rpt   a second schema, so cross-schema references are real
--     dbmig_rehearsal   the writable copy Phase 4 applies fixes to
--
-- and two accounts:
--     dbmig_collector   read-only; what Phases 1, 2 and 8 connect as
--     dbmig_app         owns the estate; used by the seeding scripts only
--
-- NOTE ON CASE. Schema and table names here are lower case throughout, which is
-- MySQL's own convention and what @@lower_case_table_names=0 (the Linux default)
-- stores literally. The collector does NOT fold schema names on the MySQL path
-- for exactly this reason -- see docs/19-mysql-source.md. Do not "tidy" these
-- into upper case: on Linux that creates different databases.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Schemas. utf8mb4 deliberately, because it is the correct default -- the
-- estate then seeds specific utf8mb3 columns as a *defect*, which is the point.
-- ---------------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS dbmig_mysql_app
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS dbmig_mysql_rpt
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS dbmig_rehearsal
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- The estate owner. Used by 02/03/04 and then not again.
-- ---------------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'dbmig_app'@'%' IDENTIFIED BY 'CHANGE_ME_APP';
GRANT ALL PRIVILEGES ON dbmig_mysql_app.* TO 'dbmig_app'@'%';
GRANT ALL PRIVILEGES ON dbmig_mysql_rpt.* TO 'dbmig_app'@'%';
GRANT ALL PRIVILEGES ON dbmig_rehearsal.* TO 'dbmig_app'@'%';

-- Routines with SQL DATA MODIFY need this when binary logging is on and the
-- creator is not SUPER. Granting it to the *owner* only, not the collector.
SET GLOBAL log_bin_trust_function_creators = 1;

-- ---------------------------------------------------------------------------
-- The collector. Read-only, and the four grants are not interchangeable:
--
--   SELECT             row data, for exact counts and profiling
--   SHOW VIEW          without it, information_schema.views.view_definition
--                      comes back EMPTY rather than denied -- so a missing
--                      grant looks like an estate with no views
--   PROCESS            server-wide status
--   REPLICATION CLIENT SHOW MASTER STATUS, i.e. whether CDC is possible at all
--
-- and three more that are not optional, each MEASURED on MySQL 8.0.46 rather
-- than assumed:
--
--   EXECUTE       without it information_schema.ROUTINES returns ZERO ROWS for
--                 the schema. Not an error -- zero rows. The estate looks like
--                 it has no stored code at all.
--   TRIGGER       same, for information_schema.TRIGGERS.
--   EVENT         same, for information_schema.EVENTS.
--   SHOW_ROUTINE  a DYNAMIC privilege, 8.0.20+, ON *.* only. Without it the
--                 routines are listed but routine_definition is NULL and
--                 SHOW CREATE FUNCTION returns NULL for the body. So Phase 4b
--                 would find four routines and no code to convert.
--
-- This is the MySQL analogue of the SELECT ANY DICTIONARY lesson that has cost
-- this project time twice -- and it is worse here, because Oracle at least
-- refuses the query. **MySQL returns fewer rows with no error.** A missing grant
-- is indistinguishable from a small estate unless someone checks.
--
-- Measured before and after, on the same server:
--
--   grants           routines  routine_def  triggers  events  view_def
--   SELECT+SHOW VIEW        0            0         0       0         2
--   + EXECUTE/TRIGGER/EVENT 4            0         2       1         2
--   + SHOW_ROUTINE          4            4         2       1         2
--
-- Anything reporting "0 routines" or "0 triggers" on this path is a grant
-- question before it is an estate question.
-- ---------------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'dbmig_collector'@'%' IDENTIFIED BY 'CHANGE_ME_COLL';

GRANT SELECT, SHOW VIEW, EXECUTE, TRIGGER, EVENT
  ON dbmig_mysql_app.* TO 'dbmig_collector'@'%';
GRANT SELECT, SHOW VIEW, EXECUTE, TRIGGER, EVENT
  ON dbmig_mysql_rpt.* TO 'dbmig_collector'@'%';
GRANT SELECT, SHOW VIEW, EXECUTE, TRIGGER, EVENT
  ON dbmig_rehearsal.* TO 'dbmig_collector'@'%';

-- Server-wide. PROCESS and REPLICATION CLIENT are static privileges;
-- SHOW_ROUTINE is a *dynamic* privilege (MySQL 8.0.20+) and can only be granted
-- ON *.* -- there is no schema-scoped form of it.
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'dbmig_collector'@'%';
GRANT SHOW_ROUTINE ON *.* TO 'dbmig_collector'@'%';

-- SELECT and SHOW VIEW **again, at server scope**, because AWS SCT demands them
-- there. This is not redundant with the schema grants above.
--
-- Measured 2026-09-29 against SCT 1.0.677: with the schema-scoped grants alone,
-- AddSource fails with
--
--   The specified account (dbmig_collector) does not have sufficient privileges
--   for working with the following object(s):
--   MYSQL Server : [SELECT, SHOW VIEW]
--
-- and the run then reports `Not found object(s) for path "Servers.MYSQL"`,
-- because AddSource never completed. The second error is the visible one and it
-- looks like a tree-path bug rather than a privilege problem.
--
-- The collector is satisfied by the schema-scoped grants; SCT is not. Both paths
-- use the same account, so it carries the union. This is the third time this
-- project has paid for a privilege check that looks like something else --
-- SELECT ANY DICTIONARY on Oracle, SHOW_ROUTINE above, and now this.
GRANT SELECT, SHOW VIEW ON *.* TO 'dbmig_collector'@'%';

-- NOT granted, and each for a reason:
--   SELECT on mysql.*   -- information_schema.user_privileges answers the same
--                          questions without read access to the password hashes
--   SUPER               -- does not exist on RDS; an estate that needs it does
--                          not migrate as-is, and the collector must never model
--                          a privilege the target cannot grant
--   RELOAD / FILE       -- not needed to read a catalogue

-- ---------------------------------------------------------------------------
-- The DMS account, Phase 7. Separate from the collector because their lifetimes
-- differ: the collector is read-only forever, this one is created for a
-- migration and revoked after cutover.
-- ---------------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'dbmig_dms'@'%' IDENTIFIED BY 'CHANGE_ME_DMS';
GRANT SELECT ON dbmig_mysql_app.* TO 'dbmig_dms'@'%';
GRANT SELECT ON dbmig_mysql_rpt.* TO 'dbmig_dms'@'%';
-- REPLICATION SLAVE is what lets DMS read the binlog stream itself, as opposed
-- to REPLICATION CLIENT which only reads its position.
GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'dbmig_dms'@'%';

FLUSH PRIVILEGES;

-- ---------------------------------------------------------------------------
-- Prove it, rather than assume it. A silent grant failure is exactly the class
-- of bug that capped the Oracle estate's defect recall at 7/8.
-- ---------------------------------------------------------------------------
SELECT 'schemas created' AS check_name,
       COUNT(*)          AS found,
       3                 AS expected
FROM information_schema.schemata
WHERE schema_name IN ('dbmig_mysql_app', 'dbmig_mysql_rpt', 'dbmig_rehearsal');

SELECT 'collector grants' AS check_name,
       COUNT(*)           AS found
FROM information_schema.schema_privileges
WHERE grantee LIKE '%dbmig_collector%';

SELECT 'collector server-wide' AS check_name,
       GROUP_CONCAT(privilege_type ORDER BY privilege_type) AS privileges
FROM information_schema.user_privileges
WHERE grantee LIKE '%dbmig_collector%';

-- The check that matters. SHOW_ROUTINE is easy to omit and its absence is
-- invisible until Phase 4b has nothing to convert.
-- AWS SCT refuses to connect without SELECT and SHOW VIEW at SERVER scope.
SELECT 'SCT server-scope grants' AS check_name,
       CASE WHEN COUNT(*) = 2
            THEN 'YES -- AWS SCT will connect'
            ELSE 'NO  -- AddSource WILL FAIL with "MYSQL Server : [SELECT, SHOW VIEW]"'
       END AS verdict
FROM information_schema.user_privileges
WHERE grantee LIKE '%dbmig_collector%'
  AND privilege_type IN ('SELECT', 'SHOW VIEW');

SELECT 'SHOW_ROUTINE granted' AS check_name,
       CASE WHEN COUNT(*) > 0 THEN 'YES -- routine bodies will be readable'
            ELSE 'NO  -- routine_definition WILL BE NULL; Phase 4b gets no code'
       END AS verdict
FROM information_schema.user_privileges
WHERE grantee LIKE '%dbmig_collector%'
  AND privilege_type = 'SHOW_ROUTINE';
