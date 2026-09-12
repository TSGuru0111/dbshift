-- =====================================================================
-- 07_grant_collector_text.sql
-- One grant: lets the read-only collector see whether Oracle Text indexes
-- actually contain anything.
--
-- WHY THIS EXISTS
-- A CONTEXT index created before its table was populated reports INDEXED
-- and VALID, has nothing pending sync, and indexes nothing. Every
-- application search against it silently returns no rows. That is exactly
-- what DBMIG_APP.IX_COMM_NOTES_TEXT does on this estate: 0 documents
-- against 400,000 rows, found in Phase 8 on 2026-09-12.
--
-- The proof is one column, CTXSYS.CTX_INDEXES.IDX_DOCID_COUNT, and none of
-- it is visible to the collector today:
--   ctxsys.ctx_indexes                  -> ORA-00942
--   count of DR$<index>$I (the tokens)  -> ORA-00942
--   dba_tables.num_rows for DR$ tables  -> NULL, never analysed
-- Guessing from a NULL statistic would fire on every estate whose internal
-- tables are simply unanalysed, so the rule needs the real number.
--
-- Scope: SELECT on one CTXSYS view. It exposes index names, their tables and
-- how many documents each has indexed -- no application data. Not
-- SELECT ANY DICTIONARY, which a client DBA is right to refuse.
--
-- 05_grant_collector_read.sql cannot do this: it runs as dbmig_app, which
-- can only grant on its own tables. This view belongs to CTXSYS.
--
-- RUN AS: system (or ctxsys)
--   sqlplus system/<password>@localhost:1521/XEPDB1 @07_grant_collector_text.sql
--
-- Re-runnable. Granting an existing privilege is a no-op.
-- Optional: without it, discovery still succeeds, the text_indexes dataset is
-- empty, and RDS-016 does not fire.
-- =====================================================================

SET SERVEROUTPUT ON

GRANT SELECT ON ctxsys.ctx_indexes TO dbmig_collector;

-- Verify by reading it the way the collector will.
SELECT idx_owner, idx_name, idx_table, idx_status, idx_docid_count
FROM   ctxsys.ctx_indexes
WHERE  idx_owner NOT IN ('CTXSYS')
ORDER  BY idx_owner, idx_name;

-- ---------------------------------------------------------------------
-- To revoke:
--   REVOKE SELECT ON ctxsys.ctx_indexes FROM dbmig_collector;
-- ---------------------------------------------------------------------
