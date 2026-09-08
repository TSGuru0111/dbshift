NAME = "identity"


def collect(s, owners):
    banner = s.fetch("identity.version", "SELECT banner_full FROM v$version")
    instance = s.fetch(
        "identity.instance",
        """SELECT instance_name, host_name, version, version_full, startup_time, status
           FROM v$instance""",
    )
    database = s.fetch(
        "identity.database",
        """SELECT name, dbid, created, log_mode, platform_name, cdb,
                  supplemental_log_data_min, supplemental_log_data_pk,
                  supplemental_log_data_all, force_logging
           FROM v$database""",
    )
    context = s.fetch(
        "identity.context",
        """SELECT sys_context('USERENV','CON_NAME')      AS con_name,
                  sys_context('USERENV','DB_NAME')       AS db_name,
                  sys_context('USERENV','SESSION_USER')  AS session_user,
                  SYSTIMESTAMP                           AS source_time
           FROM dual""",
    )
    nls = s.fetch(
        "identity.nls",
        """SELECT parameter, value FROM nls_database_parameters
           WHERE parameter IN ('NLS_CHARACTERSET','NLS_NCHAR_CHARACTERSET','NLS_LENGTH_SEMANTICS')
           ORDER BY parameter""",
    )

    merged = {}
    for part in (instance, database, context):
        if part:
            merged.update(part[0])
    if banner:
        merged["banner_full"] = banner[0]["banner_full"]

    return {
        "source_inventory.database": [merged] if merged else [],
        "source_inventory.nls_parameters": nls,
    }
