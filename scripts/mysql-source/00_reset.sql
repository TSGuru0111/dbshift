-- ============================================================================
-- DBShift MySQL source estate -- 00: reset
--
--     mysql -u root -p < 00_reset.sql
--
-- Drops and recreates all three schemas, so 02/03/04 can be re-run from a known
-- state. Separate from 01 because 01 also creates ACCOUNTS, and dropping those
-- would invalidate credentials a running console still holds.
--
-- Deliberately NOT part of 02: a seeding script that silently drops an estate is
-- a footgun, and this project has already lost data to a script that did more
-- than its name suggested.
-- ============================================================================

DROP DATABASE IF EXISTS dbmig_mysql_app;
DROP DATABASE IF EXISTS dbmig_mysql_rpt;
DROP DATABASE IF EXISTS dbmig_rehearsal;

CREATE DATABASE dbmig_mysql_app
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE dbmig_mysql_rpt
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE dbmig_rehearsal
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

SELECT 'schemas reset' AS check_name, COUNT(*) AS found, 3 AS expected
FROM information_schema.schemata
WHERE schema_name IN ('dbmig_mysql_app','dbmig_mysql_rpt','dbmig_rehearsal');
