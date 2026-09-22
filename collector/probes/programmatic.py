from ..db import in_binds

NAME = "programmatic"


def collect(s, owners):
    frag, binds = in_binds("o", owners)
    return {
        "source_inventory.views": s.fetch(
            "programmatic.views",
            f"""SELECT owner, view_name, text_length, text, read_only, editioning_view
                FROM dba_views WHERE owner IN ({frag}) ORDER BY owner, view_name""",
            binds,
        ),
        "source_inventory.materialized_views": s.fetch(
            "programmatic.mviews",
            f"""SELECT owner, mview_name, container_name, query, refresh_mode,
                       refresh_method, build_mode, fast_refreshable, last_refresh_type,
                       last_refresh_date, staleness, compile_state
                FROM dba_mviews WHERE owner IN ({frag}) ORDER BY owner, mview_name""",
            binds,
        ),
        "source_inventory.mview_logs": s.fetch(
            "programmatic.mview_logs",
            f"""SELECT log_owner, master, log_table, rowids, primary_key, object_id,
                       filter_columns, sequence, include_new_values
                FROM dba_mview_logs WHERE log_owner IN ({frag}) ORDER BY log_owner, master""",
            binds,
        ),
        "source_inventory.sequences": s.fetch(
            "programmatic.sequences",
            f"""SELECT sequence_owner, sequence_name, min_value, max_value, increment_by,
                       cycle_flag, order_flag, cache_size, last_number
                FROM dba_sequences WHERE sequence_owner IN ({frag})
                ORDER BY sequence_owner, sequence_name""",
            binds,
        ),
        "source_inventory.synonyms": s.fetch(
            "programmatic.synonyms",
            f"""SELECT owner, synonym_name, table_owner, table_name, db_link
                FROM dba_synonyms WHERE owner IN ({frag}) ORDER BY owner, synonym_name""",
            binds,
        ),
        "source_inventory.triggers": s.fetch(
            "programmatic.triggers",
            f"""SELECT owner, trigger_name, trigger_type, triggering_event, table_owner,
                       base_object_type, table_name, referencing_names, when_clause,
                       status, action_type, crossedition
                FROM dba_triggers WHERE owner IN ({frag}) ORDER BY owner, trigger_name""",
            binds,
        ),
        "source_inventory.db_links": s.fetch(
            "programmatic.db_links",
            f"""SELECT owner, db_link, username, host, created
                FROM dba_db_links WHERE owner IN ({frag}) OR owner = 'PUBLIC'
                ORDER BY owner, db_link""",
            binds,
        ),
        "source_inventory.directories": s.fetch(
            "programmatic.directories",
            """SELECT owner, directory_name, directory_path
               FROM dba_directories ORDER BY directory_name""",
        ),
        "source_inventory.external_tables": s.fetch(
            "programmatic.external_tables",
            f"""SELECT owner, table_name, type_owner, type_name, default_directory_owner,
                       default_directory_name, reject_limit, access_type
                FROM dba_external_tables WHERE owner IN ({frag}) ORDER BY owner, table_name""",
            binds,
        ),
        "source_inventory.scheduler_jobs": s.fetch(
            "programmatic.scheduler_jobs",
            f"""SELECT owner, job_name, job_style, job_type, job_action, schedule_type,
                       repeat_interval, enabled, state, run_count, failure_count,
                       last_start_date, next_run_date
                FROM dba_scheduler_jobs WHERE owner IN ({frag}) ORDER BY owner, job_name""",
            binds,
        ),
        "source_inventory.queues": s.fetch(
            "programmatic.queues",
            f"""SELECT owner, name, queue_table, queue_type, max_retries, enqueue_enabled,
                       dequeue_enabled
                FROM dba_queues WHERE owner IN ({frag}) ORDER BY owner, name""",
            binds,
        ),
        "source_inventory.types": s.fetch(
            "programmatic.types",
            f"""SELECT owner, type_name, typecode, attributes, methods, predefined,
                       incomplete, final, instantiable
                FROM dba_types WHERE owner IN ({frag}) ORDER BY owner, type_name""",
            binds,
        ),
        # Oracle Text indexes, with the one fact that says whether they hold
        # anything: idx_docid_count. A CONTEXT index created before its table was
        # populated reports INDEXED and VALID while indexing nothing, and every
        # application search against it silently returns no rows -- seen on this
        # estate, where DBMIG_APP.IX_COMM_NOTES_TEXT covers 0 of 400,000 rows.
        #
        # CTXSYS.CTX_INDEXES needs its own grant (scripts/oracle-source/
        # 07_grant_collector_text.sql). Without it this query fails with
        # ORA-00942, the dataset comes back empty, and RDS-016 simply does not
        # fire -- discovery carries on, and the failure is in the query log.
        "source_inventory.text_indexes": s.fetch(
            "programmatic.text_indexes",
            f"""SELECT idx_owner AS owner, idx_name AS index_name, idx_table_owner AS table_owner,
                       idx_table AS table_name, idx_status AS status, idx_docid_count AS docid_count,
                       idx_sync_type AS sync_type, idx_sync_interval AS sync_interval
                FROM ctxsys.ctx_indexes WHERE idx_owner IN ({frag})
                ORDER BY idx_owner, idx_name""",
            binds,
        ),
        "source_inventory.xml_schemas": s.fetch(
            "programmatic.xml_schemas",
            f"""SELECT owner, schema_url, local, int_objname
                FROM dba_xml_schemas WHERE owner IN ({frag}) ORDER BY owner, schema_url""",
            binds,
        ),
    }
