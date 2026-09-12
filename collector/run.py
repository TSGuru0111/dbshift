from __future__ import annotations

import argparse
import logging
import platform
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "collector"

from . import config, writer
from .db import connect, in_binds
from .db import Session
from .probes import PROBES

log = logging.getLogger("collector")


def _resolve_owners(session: Session, configured: tuple[str, ...]) -> dict:
    """Cross-check the configured schema list against what the database actually has.

    Oracle flags its own schemas with ORACLE_MAINTAINED='Y'. Anything marked 'N'
    that is not in the configured list gets reported rather than silently included
    or silently dropped -- schema drift should be visible, not inferred.
    """
    discovered = session.fetch(
        "config.discover_schemas",
        """SELECT username FROM dba_users
           WHERE oracle_maintained = 'N' ORDER BY username""",
    )
    discovered_names = [r["username"] for r in discovered]
    frag, binds = in_binds("o", configured)
    present = session.fetch(
        "config.verify_schemas",
        f"SELECT username FROM dba_users WHERE username IN ({frag}) ORDER BY username",
        binds,
    )
    present_names = [r["username"] for r in present]
    missing = [s for s in configured if s not in present_names]
    unconfigured = [s for s in discovered_names if s not in configured]
    if missing:
        log.warning("configured schema not present in database: %s", ", ".join(missing))
    if unconfigured:
        log.warning(
            "non-Oracle schema present but NOT collected (add to DBSHIFT_SCHEMAS to include): %s",
            ", ".join(unconfigured),
        )
    return {
        "configured": list(configured),
        "present": present_names,
        "missing": missing,
        "discovered_not_configured": unconfigured,
    }


def _dataset_columns(
    session, dataset: str, labels: list[str], rows: list[dict], probe_module=None
) -> list[str]:
    """Best-known column list for a dataset, so an empty one still has a shape.

    Rows win when there are any -- probes may add derived keys the query never
    returned. With no rows, fall back to the columns the probe's own query
    reported, matched by label: probes name their queries after the dataset
    (`programmatic.queues` -> `source_inventory.queues`), so the suffix match is
    reliable, and an unmatched dataset simply gets no declared columns, which is
    the behaviour that existed before.
    """
    if rows:
        seen: list[str] = []
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.append(key)
        return seen
    short = dataset.split(".")[-1]
    for label in labels:
        if label.split(".")[-1] == short:
            return list(session.columns_by_label.get(label, []))
    # A probe may label its query differently from the dataset it feeds --
    # "security.role_privs" produces "source_inventory.role_privileges". Let a
    # probe declare those explicitly rather than making the naming a silent
    # contract that breaks an empty dataset's schema when it drifts.
    alias = getattr(probe_module, "QUERY_LABELS", {}).get(dataset)
    if alias:
        return list(session.columns_by_label.get(alias, []))
    return []


def _externalize(probe, produced: dict[str, list[dict]], run_dir: Path) -> None:
    """Move unbounded text fields out of the dataset file.

    Keeps the row payload proportional to object count rather than code volume.
    The hash is already the identity used for skip-unchanged, so it is also the
    filename -- identical text is written once no matter how many objects share it.
    """
    for dataset, field, key_field, excerpt_chars in getattr(probe, "EXTERNALIZE", []):
        rows = produced.get(dataset)
        if not rows:
            continue
        target = run_dir / dataset.split(".")[-1]
        target.mkdir(parents=True, exist_ok=True)
        for row in rows:
            text = row.get(field)
            if text is None:
                continue
            key = row.get(key_field) or ""
            path = target / f"{key}.txt"
            if not path.exists():
                path.write_text(text, encoding="utf-8")
            row[field] = text[:excerpt_chars]
            row[f"{field}_truncated"] = max(0, len(text) - excerpt_chars)
            row[f"{field}_file"] = f"{target.name}/{path.name}"
        log.info("externalized dataset=%s rows=%d dir=%s", dataset, len(rows), target.name)


def execute(
    cfg,
    on_event=None,
    enabled_probes: list[str] | None = None,
    extra_probes: list | None = None,
) -> dict:
    """Run the probes and write the run directory, returning the manifest.

    Shared by the CLI and the web app so there is one orchestration path rather
    than two that drift. `on_event` receives probe_start / probe_done dicts,
    which is what drives the live stage view in the UI.

    `enabled_probes` selects a subset by name. Skipping a probe is recorded in
    the manifest -- a dataset absent because it was switched off must not look
    the same as one that came back empty.
    """
    run_id = str(uuid.uuid4())
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    run_dir = cfg.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    log.info("collector_run_id=%s output=%s", run_id, run_dir)

    connection = connect(cfg)
    session = Session(connection=connection)

    try:
        schema_report = _resolve_owners(session, cfg.schemas)
        owners = schema_report["present"] or list(cfg.schemas)

        identity_rows = session.fetch(
            "identity.snapshot",
            """SELECT sys_context('USERENV','CON_NAME') AS con_name,
                      sys_context('USERENV','DB_NAME')  AS db_name
               FROM dual""",
        )
        source = identity_rows[0] if identity_rows else {}
        source["dsn"] = cfg.dsn

        available = list(PROBES) + list(extra_probes or [])
        selected = [p for p in available if enabled_probes is None or p.NAME in enabled_probes]
        skipped = [p.NAME for p in available if p not in selected]

        datasets: list[dict] = []
        probe_report: list[dict] = []
        total_probes = len(selected)
        for index, probe in enumerate(selected, start=1):
            if on_event:
                on_event({
                    "event": "probe_start",
                    "probe": probe.NAME,
                    "index": index,
                    "total": total_probes,
                })
            mark = len(session.query_log)
            probe_started = time.perf_counter()
            produced = probe.collect(session, owners)
            _externalize(probe, produced, run_dir)
            labels = session.labels_since(mark)
            elapsed_ms = int((time.perf_counter() - probe_started) * 1000)
            for dataset, rows in produced.items():
                datasets.append(
                    writer.write_dataset(
                        run_dir=run_dir,
                        run_id=run_id,
                        dataset=dataset,
                        rows=rows,
                        probe=probe.NAME,
                        source_queries=labels,
                        source=source,
                        collected_at=started_at,
                        columns=_dataset_columns(session, dataset, labels, rows, probe),
                    )
                )
            probe_report.append(
                {
                    "probe": probe.NAME,
                    "elapsed_ms": elapsed_ms,
                    "datasets": list(produced),
                    "queries": labels,
                }
            )
            log.info("probe=%s datasets=%d elapsed_ms=%d", probe.NAME, len(produced), elapsed_ms)
            if on_event:
                rows_returned = sum(q.row_count for q in session.query_log[mark:])
                on_event({
                    "event": "probe_done",
                    "probe": probe.NAME,
                    "index": index,
                    "total": total_probes,
                    "elapsed_ms": elapsed_ms,
                    "datasets": len(produced),
                    "queries": len(labels),
                    "rows": rows_returned,
                    "failed": [q.label for q in session.query_log[mark:] if q.error],
                })
    finally:
        connection.close()

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    failed = [q.label for q in session.query_log if q.error]
    manifest = {
        "collector_run_id": run_id,
        "schema_version": writer.SCHEMA_VERSION,
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": elapsed_ms,
        "collector": {
            "python": platform.python_version(),
            "host_os": platform.system(),
            "connection": cfg.redacted(),
        },
        "source": source,
        "schemas": schema_report,
        "probes": probe_report,
        "probes_skipped": skipped,
        "datasets": datasets,
        "total_rows": sum(d["row_count"] for d in datasets),
        "failed_queries": failed,
        "query_log": [
            {
                "label": q.label,
                "sql": q.sql,
                "sql_sha256": q.sql_sha256,
                "row_count": q.row_count,
                "elapsed_ms": q.elapsed_ms,
                "error": q.error,
            }
            for q in session.query_log
        ],
    }
    writer.write_manifest(run_dir, manifest)
    writer.append_run_ledger(
        cfg.output_dir,
        {
            "collector_run_id": run_id,
            "finished_at_utc": manifest["finished_at_utc"],
            "elapsed_ms": elapsed_ms,
            "datasets": len(datasets),
            "total_rows": manifest["total_rows"],
            "failed_queries": len(failed),
        },
    )

    manifest["run_dir"] = str(run_dir)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBShift discovery collector")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    try:
        cfg = config.load(args.output_dir)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    manifest = execute(cfg)
    run_id = manifest["collector_run_id"]
    run_dir = manifest["run_dir"]
    datasets = manifest["datasets"]
    elapsed_ms = manifest["elapsed_ms"]
    failed = manifest["failed_queries"]

    print(f"collector_run_id : {run_id}")
    print(f"output           : {run_dir}")
    print(f"datasets         : {len(datasets)}")
    print(f"total rows       : {manifest['total_rows']}")
    print(f"elapsed          : {elapsed_ms} ms")
    if failed:
        print(f"FAILED queries   : {len(failed)} -> {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
