"""Drive one SCT assessment end to end, for one target, and keep what it produced.

    plan(...)   what would run -- free, touches nothing, no password needed
    assess(...) run SCT and collect its own PDF and CSV

**The password problem, handled rather than hidden.** SCT's batch CLI takes the
source password only inside the scenario file, so it must be written to disk.
This module writes it to a per-run directory, runs SCT, and deletes the scenario
in a `finally` -- and the record it keeps holds the *redacted* scenario, so a
recorded run can be reproduced without the secret travelling with it.

**Results are cached per (estate, target).** SCT assesses one target platform at
a time because it is a project setting, not a report filter, so the console's
dropdown means "run SCT for that target". Re-selecting a target already assessed
reads the stored result instead of re-running a job that takes 25 minutes or
more on the demo estate.

The key is the schemas plus the target, **not the collector run id** -- SCT
reads Oracle's data dictionary itself and never opens a collector run, so a
fresh discovery changes nothing SCT would see. See `_cache_key`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import engines
from engines import spec as engine_spec

from . import scenario as scenario_mod
from . import targets, toolchain as toolchain_mod
from .hosts import local as local_host

OUTPUT = Path(__file__).resolve().parent / "output"

HOSTS = {local_host.NAME: local_host}


def _host_tag(dsn: str | None) -> str:
    """A short, stable tag for the SERVER a DSN names -- not the whole DSN.

    Added 2026-09-29, after a real collision: the same estate seeded on a local
    Docker MySQL and on an EC2 host produced the same `(engine, schemas, target)`
    key, so the EC2 run served the container's cached report. It looked right --
    identical action items, because it is the same estate -- which is exactly what
    makes it dangerous. A report is evidence about a SERVER, and two servers are
    two reports.

    The host, not the port or the database: a tunnel or a port change does not
    make it a different estate, and including the database would split the cache
    for a DSN difference that the schema filter already governs. `localhost` and
    `127.0.0.1` deliberately collapse to one tag, because they are the same
    machine and re-running locally should hit the cache.
    """
    host = str(dsn or "").split("/")[0].split(":")[0].strip().lower()
    if host in ("", "localhost", "127.0.0.1", "::1"):
        return "local"
    # Dots and colons are legal in a hostname and awkward in a directory name.
    return re.sub(r"[^a-z0-9]+", "-", host).strip("-")[:32] or "local"


def _cache_key(target_id: str, schemas: list[str] | None,
               source_engine: str | None = None, dsn: str | None = None) -> str:
    """What an SCT result actually depends on: source engine, schemas, target.

    **Deliberately not the collector run id.** It was, and that was wrong in
    both directions. SCT connects to the source and reads the live data
    dictionary itself -- it never opens a collector run -- so a fresh discovery
    produces a new run id without changing anything SCT would see. Keying on it
    meant every re-discovery invalidated a 25-minute assessment for no reason,
    which the Phases 1-5 browser drive exposed by running discovery first each
    time.

    It is also not *only* the target: assessing DBMIG_APP and DBMIG_TELCO
    against the same target are different reports, and sharing a directory would
    have one silently overwrite the other.

    **The source engine joined the key on 2026-09-29**, because `rds-postgresql`
    is a target id from both Oracle and MySQL: without it, two estates that
    happen to share a schema name would share a directory and one assessment
    would silently overwrite the other.

    **Oracle's key is unchanged, and that is deliberate.** Prefixing every engine
    would rename the directories holding the five real SCT assessments already on
    disk -- each a ~25-minute run -- orphaning them for no benefit, since Oracle
    was the only source when they were produced. So Oracle keeps the historical
    shape and MySQL carries the prefix. Any third engine should carry one too.

    A schema list change therefore invalidates the cache, which is correct --
    that genuinely is a different assessment.
    """
    engine = engines.normalize(source_engine)
    # Case is folded on Oracle and preserved on MySQL, matching how each engine
    # treats a schema name. Folding a MySQL name here would make `Sales` and
    # `SALES` -- two genuinely different databases on Linux -- share a directory.
    fold = engine_spec.spec(engine)["fold_schema_names"]
    names = [(s.upper() if fold else s) for s in (schemas or [])]
    estate = "-".join(sorted(names)) or "noestate"
    if len(estate) > 60:
        # Keep the directory name readable and inside Windows' path limits while
        # staying unique: a hash of the full list, with the first names visible.
        digest = hashlib.sha256(estate.encode()).hexdigest()[:8]
        estate = f"{estate[:48]}-{digest}"
    prefix = "" if engine == engines.ORACLE else f"{engine.lower()}-"
    # The host joins the key only when it is not local, for the same reason
    # Oracle keeps its historical prefix-free key: every existing directory on
    # disk was produced from a local source, and renaming them would orphan five
    # real 25-minute assessments.
    host = _host_tag(dsn)
    host_part = "" if host == "local" else f"{host}-"
    return f"{prefix}{host_part}{estate}-{target_id}"


def _run_dir(target_id: str, schemas: list[str] | None = None,
             source_engine: str | None = None, dsn: str | None = None) -> Path:
    return OUTPUT / _cache_key(target_id, schemas, source_engine, dsn)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plan(
    *,
    target_id: str,
    dsn: str,
    user: str,
    schemas: list[str],
    collector_run_id: str = "",
    host: str = local_host.NAME,
    source_engine: str | None = None,
) -> dict:
    """What an assessment would do. Free, and needs no password.

    Exists so the console can show the whole shape -- target, schemas, host,
    prerequisites -- and refuse with a reason before anyone types a password.
    """
    engine = engines.normalize(source_engine)
    target = targets.get(target_id, engine)
    tc = toolchain_mod.discover(engine)
    out = _run_dir(target_id, schemas, engine, dsn)

    return {
        "phase": "2-sct",
        "planned_at_utc": _now(),
        "host": host,
        "source_engine": engine,
        "target": {
            "id": target["id"],
            "label": target["label"],
            "sct_platform": target["sct_platform"],
            "in_scope": target["in_scope"],
            "scope_note": target["scope_note"],
            # Whether SCT reports conversion work for this pair at all. False on a
            # homogeneous pair, where zero action items is a complete result rather
            # than evidence not yet collected -- Phase 5 reads this.
            "sct_conversion": target["sct_conversion"],
        },
        "source": {"dsn": dsn, "user": user, "schemas": list(schemas)},
        "collector_run_id": collector_run_id or None,
        "toolchain": tc.as_dict(),
        "ready": tc.ready,
        "output_dir": str(out),
        "cached": (out / "sct_assessment.json").exists(),
        "commands": scenario_mod.command_names(
            scenario_mod.build_script(
                project_name="plan", project_dir=out / "project", report_dir=out / "report",
                log_dir=out / "log", target_id=target_id, dsn=dsn, user=user, password="",
                schemas=list(schemas) or ["PLACEHOLDER"],
                jdbc_jar=Path(tc.jdbc_jar or "driver.jar"),
                source_engine=engine,
            )
        ),
    }


def cached(target_id: str, schemas: list[str] | None = None,
           source_engine: str | None = None, dsn: str | None = None) -> dict | None:
    """A stored result for this (engine, host, estate, target), if already assessed."""
    path = _run_dir(target_id, schemas, source_engine, dsn) / "sct_assessment.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def assess(
    *,
    target_id: str,
    dsn: str,
    user: str,
    password: str,
    schemas: list[str],
    collector_run_id: str = "",
    host: str = local_host.NAME,
    on_line: Callable[[str], None] | None = None,
    reuse_cached: bool = True,
    timeout: int | None = None,
    source_engine: str | None = None,
) -> dict:
    """Run AWS SCT for one target and record what it produced."""
    engine = engines.normalize(source_engine)
    if reuse_cached:
        hit = cached(target_id, schemas, engine, dsn)
        if hit:
            hit["from_cache"] = True
            return hit

    target = targets.get(target_id, engine)
    tc = toolchain_mod.discover(engine)
    if not tc.ready:
        return {
            "ok": False,
            "reason": "toolchain not ready",
            "toolchain": tc.as_dict(),
            "target": target["id"],
            "assessed_at_utc": _now(),
        }

    runner_host = HOSTS.get(host)
    if runner_host is None:
        return {
            "ok": False,
            "reason": f"unknown host {host!r}; available: " + ", ".join(sorted(HOSTS)),
            "target": target["id"],
            "assessed_at_utc": _now(),
        }

    out = _run_dir(target_id, schemas, engine, dsn)
    project_dir = out / "project"
    report_dir = out / "report"
    # A stale report directory is worse than none: SCT writing no file would
    # leave the previous run's PDF looking like this run's output.
    if report_dir.exists():
        shutil.rmtree(report_dir, ignore_errors=True)
    for d in (project_dir, report_dir):
        d.mkdir(parents=True, exist_ok=True)

    log_dir = out / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    # SCT derives the PDF's filename from the project name, so it must be a
    # valid filename as well as a project label.
    project_name = f"dbshift-{'-'.join(schemas)[:40]}-{target_id}".replace(" ", "_")
    script = scenario_mod.build_script(
        project_name=project_name,
        project_dir=project_dir,
        report_dir=report_dir,
        log_dir=log_dir,
        target_id=target_id,
        dsn=dsn,
        user=user,
        password=password,
        schemas=list(schemas),
        jdbc_jar=tc.jdbc_jar,
        source_engine=engine,
    )
    scenario_path = out / "scenario.scts"

    started = _now()
    try:
        scenario_mod.write(script, scenario_path)
        result = runner_host.run(scenario_path, tc, on_line=on_line, timeout=timeout)
    finally:
        # The scenario holds the source password in cleartext. It does not
        # outlive the run, whatever happened during it.
        scenario_path.unlink(missing_ok=True)

    produced = sorted(p.name for p in report_dir.rglob("*") if p.is_file())

    # **SCT exits 0 even when a command failed.** Proved on 2026-09-17: AddSource
    # raised DbLoaderInsufficientPrivilegesException, every later command ran
    # against an empty tree and "succeeded", SaveReportPDF wrote nothing, and the
    # process still returned 0. Trusting the exit code reported a clean run that
    # had produced no report -- the worst possible failure mode here, because an
    # empty assessment reads as an estate with nothing wrong with it.
    #
    # So success requires an artefact. Two independent signals, and the reason
    # is recorded rather than inferred.
    sct_errors = [ln for ln in result.lines if " ERROR   " in ln]
    ok = bool(result.ok and produced)
    if result.ok and not produced:
        failure = "SCT exited 0 but wrote no report. First error: " + (
            sct_errors[0].split(" ERROR   ", 1)[-1].strip() if sct_errors
            else "none logged"
        )
    else:
        failure = None if ok else (result.detail or "SCT did not complete")

    record = {
        "ok": ok,
        "reason": failure,
        "sct_error_count": len(sct_errors),
        # The errors SCT itself logged, whatever the exit code said.
        "sct_errors": [e.split(" ERROR   ", 1)[-1].strip() for e in sct_errors[:10]],
        "exit_code_said_ok": result.ok,
        "phase": "2-sct",
        "assessed_at_utc": started,
        "finished_at_utc": _now(),
        "collector_run_id": collector_run_id or None,
        "host": result.as_dict(),
        # On the record, because a report read six months later must say which
        # engine produced it without anyone inferring it from the CSV path.
        "source_engine": engine,
        "target": {
            "id": target["id"],
            "label": target["label"],
            "sct_platform": target["sct_platform"],
            "in_scope": target["in_scope"],
            "scope_note": target["scope_note"],
            # Whether SCT publishes conversion work for this pair. Phase 5 reads
            # it: on a homogeneous pair zero action items is a complete result,
            # not evidence still to be collected.
            "sct_conversion": target["sct_conversion"],
        },
        "source": {"dsn": dsn, "user": user, "schemas": list(schemas)},
        "toolchain": tc.as_dict(),
        # Redacted, so a recorded run is reproducible without the secret.
        "scenario": scenario_mod.redacted(script),
        "output_dir": str(out),
        "report_dir": str(report_dir),
        "artefacts": produced,
        "log_tail": result.lines[-50:],
        "from_cache": False,
    }

    (out / "sct_assessment.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return record


def artefact_paths(record: dict) -> dict:
    """SCT's own PDF and CSV files from a run, by kind.

    These are served verbatim. Re-rendering them would forfeit the one thing
    this path has that `report/export.py` does not: the documents are AWS SCT's,
    not a reproduction of their shape.

    Two things learned from SCT 1.0.677's real output on 2026-09-17:

      - **The CSVs are nested**, under `<report>/<SOURCE>/<target>/` -- e.g.
        `ORACLE/` or `MYSQL/`, named for the AddSource name -- not written flat
        into the directory given to `SaveReportCSV`. `rglob` was already right,
        which is why adding MySQL needed no change here; the flat assumption
        would have been wrong and the hardcoded `ORACLE/` would have been wrong
        again a year later.
      - **SCT writes three CSVs**, and only one holds the action items. The
        other two are rollups (`_Summary`, `_Action_Items_Summary`). The
        detail file is listed first so `artefact_paths(...)["csv"][0]` is the
        one to parse -- taking whatever sorted first would have parsed a
        summary and reported a handful of rows as the whole assessment.
    """
    report_dir = Path(record.get("report_dir") or "")
    out = {"pdf": [], "csv": []}
    if not report_dir.is_dir():
        return out

    detail, summaries = [], []
    for p in sorted(report_dir.rglob("*")):
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix == ".pdf":
            out["pdf"].append(str(p))
        elif suffix == ".csv":
            (summaries if "summary" in p.stem.lower() else detail).append(str(p))

    out["csv"] = detail + summaries
    return out
