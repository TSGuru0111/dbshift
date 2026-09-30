"""Who and what we are connected to -- the MySQL answer to `probes/identity.py`.

Emits the same `database` dataset the Oracle probe does, with the same column
names, because Phase 2 rules read `database` by column and Phase 8 reads the
version from it. Columns with no MySQL meaning are present and NULL rather than
absent: a reader can then tell "this engine has no such concept" from "this
probe did not run", which an absent column cannot express.

`log_mode` and the three `supplemental_log_data_*` columns are the load-bearing
case. Oracle's CDC readiness lives there, and `collector/mode.readiness()` reads
them. MySQL's equivalent evidence is binlog configuration, which is collected
here into the same columns' MySQL analogues *and* into `mysql_binlog` for the
console panel -- see `cdc.py`.
"""

NAME = "identity"


def collect(s, owners):
    # One row, like Oracle's. @@ variables rather than a catalogue view because
    # several are session/global settings with no information_schema row.
    database = s.fetch(
        "identity.snapshot",
        """SELECT @@hostname                       AS instance_name,
                  @@hostname                       AS host_name,
                  VERSION()                        AS version,
                  VERSION()                        AS version_full,
                  NULL                             AS startup_time,
                  'OPEN'                           AS status,
                  DATABASE()                       AS name,
                  NULL                             AS dbid,
                  NULL                             AS created,
                  @@log_bin                        AS log_bin,
                  @@binlog_format                  AS binlog_format,
                  @@binlog_row_image               AS binlog_row_image,
                  @@server_id                      AS server_id,
                  @@version_comment                AS platform_name,
                  @@character_set_server           AS character_set_server,
                  @@collation_server               AS collation_server,
                  @@lower_case_table_names         AS lower_case_table_names,
                  @@sql_mode                       AS sql_mode,
                  @@default_storage_engine         AS default_storage_engine,
                  @@time_zone                      AS time_zone,
                  DATABASE()                       AS db_name,
                  CURRENT_USER()                   AS session_user,
                  NOW()                            AS source_time""",
    )
    if database:
        row = database[0]
        # The Oracle-shaped columns every downstream reader expects. Mapped
        # where MySQL has an equivalent, NULL where it does not.
        #
        # log_mode: Oracle's ARCHIVELOG/NOARCHIVELOG is "is redo retained for
        # replication". MySQL's equivalent is whether the binary log is on. The
        # value is rendered in Oracle's vocabulary so mode.readiness() and the
        # console's existing Log-mode row keep working; the native reading stays
        # in log_bin alongside it, unaltered.
        log_bin = str(row.get("log_bin") or "").strip()
        binlog_on = log_bin in ("1", "ON", "on", "True")
        row["log_mode"] = "ARCHIVELOG" if binlog_on else "NOARCHIVELOG"
        # Supplemental logging exists so redo carries enough column data to
        # rebuild an UPDATE. binlog_row_image=FULL is exactly that guarantee,
        # and ROW format is what makes it meaningful at all.
        full_image = str(row.get("binlog_row_image") or "").upper() == "FULL"
        row_format = str(row.get("binlog_format") or "").upper() == "ROW"
        supplemental = "YES" if (binlog_on and row_format and full_image) else "NO"
        row["supplemental_log_data_min"] = supplemental
        row["supplemental_log_data_pk"] = supplemental
        row["supplemental_log_data_all"] = "YES" if (supplemental == "YES" and full_image) else "NO"
        row["force_logging"] = "YES" if binlog_on else "NO"
        # No containers in MySQL. NULL, not faked: a pluggable-database question
        # has no answer here, and 'NO' would assert one.
        row["cdb"] = None
        row["con_name"] = None
    return {"source_inventory.database": database}
