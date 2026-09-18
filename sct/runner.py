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
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import scenario as scenario_mod
from . import targets, toolchain as toolchain_mod
from .hosts import local as local_host

OUTPUT = Path(__file__).resolve().parent / "output"

HOSTS = {local_host.NAME: local_host}


def _cache_key(target_id: str, schemas: list[str] | None) -> str:
    """What an SCT result actually depends on: the schemas read and the target.

    **Deliberately not the collector run id.** It was, and that was wrong in
    both directions. SCT connects to Oracle and reads the live data dictionary
    itself -- it never opens a collector run -- so a fresh discovery produces a
    new run id without changing anything SCT would see. Keying on it meant every
    re-discovery invalidated a 25-minute assessment for no reason, which the
    Phases 1-5 browser drive exposed by running discovery first each time.

    It is also not *only* the target: assessing DBMIG_APP and DBMIG_TELCO
    against the same target are different reports, and sharing a directory would
    have one silently overwrite the other.

    A schema list change therefore invalidates the cache, which is correct --
    that genuinely is a different assessment.
    """
    estate = "-".join(sorted(s.upper() for s in (schemas or []))) or "noestate"
    if len(estate) > 60:
        # Keep the directory name readable and inside Windows' path limits while
        # staying unique: a hash of the full list, with the first names visible.
        digest = hashlib.sha256(estate.encode()).hexdigest()[:8]
        estate = f"{estate[:48]}-{digest}"
    return f"{estate}-{target_id}"


def _run_dir(target_id: str, schemas: list[str] | None = None) -> Path:
    return OUTPUT / _cache_key(target_id, schemas)


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
) -> dict:
    """What an assessment would do. Free, and needs no password.

    Exists so the console can show the whole shape -- target, schemas, host,
    prerequisites -- and refuse with a reason before anyone types a password.
    """
    target = targets.get(target_id)
    tc = toolchain_mod.discover()
    out = _run_dir(target_id, schemas)

    return {
        "phase": "2-sct",
        "planned_at_utc": _now(),
        "host": host,
        "target": {
            "id": target["id"],
            "label": target["label"],
            "sct_platform": target["sct_platform"],
            "in_scope": target["in_scope"],
            "scope_note": target["scope_note"],
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
                jdbc_jar=Path(tc.jdbc_jar or "ojdbc8.jar"),
            )
        ),
    }


def cached(target_id: str, schemas: list[str] | None = None) -> dict | None:
    """A stored result for this (estate, target), if SCT has already assessed it."""
    path = _run_dir(target_id, schemas) / "sct_assessment.json"
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
) -> dict:
    """Run AWS SCT for one target and record what it produced."""
    if reuse_cached:
        hit = cached(target_id, schemas)
        if hit:
            hit["from_cache"] = True
            return hit

    target = targets.get(target_id)
    tc = toolchain_mod.discover()
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

    out = _run_dir(target_id, schemas)
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
        "target": {
            "id": target["id"],
            "label": target["label"],
            "sct_platform": target["sct_platform"],
            "in_scope": target["in_scope"],
            "scope_note": target["scope_note"],
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

      - **The CSVs are nested**, under `<report>/ORACLE/<target>/`, not written
        flat into the directory given to `SaveReportCSV`. `rglob` was already
        right; the flat assumption would have been wrong.
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
