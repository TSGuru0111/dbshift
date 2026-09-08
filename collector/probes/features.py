NAME = "features"

# Drives the SE2-vs-EE verdict. Not optional -- an assumed feature list is the
# difference between a licence estimate and a licence guess.
EDITION_FORCING = (
    "Partitioning (user)",
    "Advanced Compression",
    "Transparent Data Encryption",
    "Oracle Advanced Security",
    "Real Application Clusters (RAC)",
    "Oracle Multitenant",
    "Active Data Guard",
    "Oracle Label Security",
    "Oracle Database Vault",
    "Spatial",
    "Parallel Query",
    "Diagnostic Pack",
    "Tuning Pack",
    "In-Memory Column Store",
)


def collect(s, owners):
    usage = s.fetch(
        "features.usage_statistics",
        """SELECT name, version, detected_usages, total_samples, currently_used,
                  first_usage_date, last_usage_date, last_sample_date, aux_count,
                  feature_info
           FROM dba_feature_usage_statistics
           ORDER BY name, version""",
    )
    for row in usage:
        row["edition_forcing"] = "Y" if row.get("name") in EDITION_FORCING else "N"

    options = s.fetch(
        "features.database_options",
        """SELECT parameter, value FROM v$option ORDER BY parameter""",
    )
    parameters = s.fetch(
        "features.parameters",
        """SELECT name, value, isdefault, description
           FROM v$parameter
           WHERE name IN ('compatible','cpu_count','sga_target','pga_aggregate_target',
                          'memory_target','db_block_size','processes','open_cursors',
                          'enable_ddl_logging','db_files')
           ORDER BY name""",
    )
    # XE exposes v$osstat but leaves v$sysmetric empty, and AWR is not licensable
    # here -- point-in-time only. Percentiles are a production-source concern.
    osstat = s.fetch(
        "features.osstat",
        """SELECT stat_name, value FROM v$osstat ORDER BY stat_name""",
    )
    sysmetric = s.fetch(
        "features.sysmetric",
        """SELECT metric_name, value, metric_unit, begin_time, end_time
           FROM v$sysmetric ORDER BY metric_name""",
    )
    awr = s.fetch(
        "features.awr_snapshots",
        """SELECT COUNT(*) AS snapshot_count,
                  MIN(begin_interval_time) AS earliest_snapshot,
                  MAX(end_interval_time)   AS latest_snapshot
           FROM dba_hist_snapshot""",
    )
    return {
        "source_inventory.feature_usage": usage,
        "source_inventory.database_options": options,
        "source_inventory.parameters": parameters,
        "source_inventory.os_statistics": osstat,
        "source_inventory.system_metrics": sysmetric,
        "source_inventory.awr_coverage": awr,
    }
