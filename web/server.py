"""Local console for DBShift.

Runs on the operator's machine, next to the source database. The browser never
talks to Oracle -- this process does, using the same collector and assessment
modules the CLI uses, so the UI can never show a result the CLI would not.

    python -m web.server         then open http://127.0.0.1:8765

The database password is held in memory for the life of the process and is never
written to disk, logged, or returned to the browser.
"""

from __future__ import annotations

import csv
import io
import json
import os
import queue
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "web"

from fastapi import FastAPI, HTTPException
from fastapi.responses import (FileResponse, HTMLResponse, Response,
                               StreamingResponse)
from pydantic import BaseModel

from assess import engine as assess_engine
from assess import loader as assess_loader
from assess import scoring as assess_scoring
from collector import config as collector_config
from collector import run as collector_run
from convert import inventory as convert_inventory
from convert import plan as convert_plan
from convert import target as convert_target
from cutover import run as cutover_run
from blocker import gate as blocker_gate
from blocker import policy as blocker_policy
from botocore.exceptions import ClientError
from killswitch import run as killswitch_run
from migrate import run as migrate_run
from migrate import steps as migrate_steps
from dms import policy as dms_policy
from dms import run as dms_run
from provision import deploy as provision_deploy
from provision import overrides as provision_overrides
from provision import policy as provision_policy
from provision import run as provision_run
from provision import verify as provision_verify
from validate import context as validate_context
from validate import run as validate_run
from remediate import plan as remediate_plan
from report import build as report_build
from report import export as report_export
from report import render as report_render
from sizing import run as sizing_run
from sizing import target as sizing_target
from sizing import utilization as sizing_utilization
from collector import mode as migration_mode
from collector.db import in_binds  # noqa: F401  (kept for custom probe authors)
from collector.probes import PROBES

from . import preflight, settings

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="DBShift Console")


# --------------------------------------------------------------------------- state


@dataclass
class State:
    dsn: str | None = None
    user: str | None = None
    password: str | None = None
    schema: str = ""
    schemas: tuple = ()
    connected: bool = False
    facts: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    run_id: str | None = None
    run_dir: Path | None = None
    manifest: dict | None = None
    assessment: dict | None = None
    sizing: dict | None = None
    engine: str = sizing_target.ORACLE   # which target every later phase acts on
    engine_chosen_by: str | None = None
    # Full load or full load + CDC, asked in Phase 1. Decides whether the
    # CDC-only findings are blockers -- see collector/mode.py.
    migration_mode: str = migration_mode.DEFAULT
    mode_declared: bool = False
    mode_chosen_by: str | None = None
    utilization: dict | None = None
    remediation: dict | None = None
    rehearsal_dsn: str | None = None
    rehearsal: Any = None
    conversion: dict | None = None
    pg_target: Any = None          # convert.target.PgTarget; password in memory only
    pg_dsn: str | None = None
    gate: dict | None = None
    waivers: list = field(default_factory=list)
    provision: dict | None = None
    # A person's manual instance choice and database configuration, already
    # validated. Held per-session rather than written to disk: it belongs to
    # the render about to happen, not to the project.
    provision_overrides: dict = field(default_factory=lambda: {
        "instance_class": None, "configuration": None})
    migrate_owner_password: str | None = None   # memory only, for a fresh export


STATE = State()

PROBE_DESCRIPTIONS = {
    "identity": "Version, container, character set, log mode, supplemental logging",
    "objects": "The master object census every other count reconciles against",
    "tables": "Tables and full column structure, including virtual and hidden flags",
    "indexes": "Indexes, their columns and expressions",
    "constraints": "PK / FK / unique / check, and whether each was ever validated",
    "storage": "Real segment bytes, LOB segments, tablespaces",
    "partitions": "Partitioned tables, partitions and keys — the EE trigger",
    "plsql": "Stored code, its errors, and a SHA-256 per object",
    "programmatic": "Views, sequences, links, directories, queues, types, XML schemas",
    "security": "Users, roles, grants, profiles",
    "features": "Which licensed features were actually used — the licence evidence",
    "dataprofile": "Bounded aggregates over table data. Counts only, never rows",
}


# --------------------------------------------------------------------------- helpers


class CustomProbe:
    """Adapter making a user-supplied SELECT look like a built-in probe."""

    def __init__(self, spec: dict):
        self.NAME = spec["name"]
        self.spec = spec

    def collect(self, session, owners):
        rows = session.fetch(f"custom.{self.NAME}", self.spec["sql"])
        return {self.spec["dataset"]: rows}


def _custom_probes() -> list[CustomProbe]:
    return [CustomProbe(p) for p in settings.load()["custom_probes"]]


def _all_rules() -> list[dict]:
    builtin = assess_engine.load_rules()
    for rule in builtin:
        rule["custom"] = False
    return builtin + settings.load()["custom_rules"]


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream(work) -> StreamingResponse:
    """Run `work(emit)` on a thread and relay its events to the browser as SSE."""
    events: queue.Queue = queue.Queue()

    def emit(evt: dict) -> None:
        events.put(evt)

    def runner() -> None:
        try:
            work(emit)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the UI, not swallowed
            events.put({
                "event": "error",
                "message": str(exc),
                "detail": traceback.format_exc(limit=3),
            })
        finally:
            events.put({"event": "end"})

    threading.Thread(target=runner, daemon=True).start()

    def generate() -> Iterator[str]:
        while True:
            evt = events.get()
            yield _sse(evt)
            if evt.get("event") in ("end", "error"):
                if evt.get("event") == "error":
                    continue
                break

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- models


class ConnectRequest(BaseModel):
    dsn: str = "localhost:1521/XEPDB1"
    user: str = "dbmig_collector"
    password: str
    # Blank means discover the application schemas from the database.
    schema_name: str = ""


class ToggleRequest(BaseModel):
    kind: str
    id: str
    enabled: bool


class CustomProbeRequest(BaseModel):
    name: str
    description: str = ""
    sql: str


class CustomRuleRequest(BaseModel):
    rule_id: str
    category: str
    severity: str
    remediation_level: str
    title: str
    rationale: str = ""
    sql: str


# --------------------------------------------------------------------------- routes


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def get_state():
    return {
        "connected": STATE.connected,
        "dsn": STATE.dsn,
        "user": STATE.user,
        "schema": STATE.schema,
        "facts": STATE.facts,
        "checks": STATE.checks,
        "has_discovery": STATE.manifest is not None,
        "has_assessment": STATE.assessment is not None,
        "has_sizing": STATE.sizing is not None,
        "engine": STATE.engine,
        "engine_label": sizing_target.LABEL[STATE.engine],
        "engine_chosen_by": STATE.engine_chosen_by,
        "migration_mode": STATE.migration_mode,
        "migration_mode_label": migration_mode.LABEL[STATE.migration_mode],
        "migration_mode_declared": STATE.mode_declared,
        "migration_mode_chosen_by": STATE.mode_chosen_by,
        "migration_modes": [
            {"mode": m, "label": migration_mode.LABEL[m],
             "description": migration_mode.DESCRIPTION[m]}
            for m in migration_mode.MODES
        ],
        "cdc_readiness": migration_mode.readiness(
            STATE.facts.get("log_mode"), STATE.facts.get("supplemental_logging")
        ),
        "has_remediation": STATE.remediation is not None,
        "has_conversion": STATE.conversion is not None,
        "pg_dsn": STATE.pg_dsn,
        "has_gate": STATE.gate is not None,
        "has_provision": STATE.provision is not None,
        "waivers": STATE.waivers,
        "utilization": STATE.utilization,
        "rehearsal_dsn": STATE.rehearsal_dsn,
        "run_id": STATE.run_id,
        "network_requirements": preflight.NETWORK_REQUIREMENTS,
    }


@app.post("/api/connect")
def connect(req: ConnectRequest):
    result = preflight.run(req.dsn, req.user, req.password, req.schema_name)
    STATE.dsn, STATE.user, STATE.schema = req.dsn, req.user, req.schema_name
    STATE.checks, STATE.facts = result["checks"], result["facts"]
    # Whatever preflight resolved -- the schemas typed, or the ones discovered
    # when the field was left blank. Discovery must use these, never a default.
    STATE.schemas = tuple(result["facts"].get("schemas_selected", ()))
    STATE.connected = result["ok"]
    STATE.password = req.password if result["ok"] else None
    return {"ok": result["ok"], "checks": result["checks"], "facts": result["facts"]}


@app.post("/api/disconnect")
def disconnect():
    STATE.password = None
    STATE.connected = False
    STATE.checks, STATE.facts = [], {}
    return {"ok": True}


@app.get("/api/catalogue")
def catalogue():
    cfg = settings.load()
    disabled_probes = set(cfg["disabled_probes"])
    disabled_rules = set(cfg["disabled_rules"])

    probes = [
        {
            "name": p.NAME,
            "description": PROBE_DESCRIPTIONS.get(p.NAME, ""),
            "enabled": p.NAME not in disabled_probes,
            "custom": False,
        }
        for p in PROBES
    ] + [
        {
            "name": p["name"],
            "description": p["description"],
            "sql": p["sql"],
            "enabled": p["name"] not in disabled_probes,
            "custom": True,
        }
        for p in cfg["custom_probes"]
    ]

    rules = []
    for r in _all_rules():
        rules.append({
            "rule_id": r["rule_id"],
            "category": r["category"],
            "severity": r["severity"],
            "remediation_level": r["remediation_level"],
            "title": r["title"],
            "rationale": r["rationale"],
            "sql": r["sql"],
            "custom": bool(r.get("custom")),
            "enabled": r["rule_id"] not in disabled_rules,
        })

    return {"probes": probes, "rules": rules}


@app.post("/api/toggle")
def toggle(req: ToggleRequest):
    if req.kind not in ("probe", "rule"):
        raise HTTPException(400, "kind must be 'probe' or 'rule'")
    settings.set_enabled(req.kind, req.id, req.enabled)
    return {"ok": True}


@app.post("/api/probes")
def add_probe(req: CustomProbeRequest):
    try:
        return {"ok": True, "probe": settings.add_custom_probe(req.name, req.description, req.sql)}
    except settings.SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/rules")
def add_rule(req: CustomRuleRequest):
    try:
        return {"ok": True, "rule": settings.add_custom_rule(req.model_dump())}
    except settings.SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc


# Explicit paths, not /api/{kind}/{identifier}. A catch-all here silently
# swallowed DELETE /api/waivers/... and answered 404 from the wrong handler.
def _delete_custom(kind: str, identifier: str):
    try:
        settings.delete_custom(kind, identifier)
    except settings.SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.delete("/api/probes/{name}")
def delete_probe(name: str):
    return _delete_custom("probe", name)


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: str):
    return _delete_custom("rule", rule_id)


@app.get("/api/discover")
def discover():
    if not STATE.connected or not STATE.password:
        raise HTTPException(409, "not connected")

    cfg_settings = settings.load()
    disabled = set(cfg_settings["disabled_probes"])
    extra = [p for p in _custom_probes() if p.NAME not in disabled]
    enabled = [p.NAME for p in PROBES if p.NAME not in disabled] + [p.NAME for p in extra]

    def work(emit):
        cfg = collector_config.Config(
            user=STATE.user,
            password=STATE.password,
            dsn=STATE.dsn,
            schemas=STATE.schemas or collector_config.DEFAULT_SCHEMAS,
            output_dir=Path(__file__).resolve().parent.parent / "collector" / "output",
            migration_mode=STATE.migration_mode,
            mode_declared=STATE.mode_declared,
            mode_chosen_by=STATE.mode_chosen_by,
        )
        emit({"event": "start", "total": len(enabled), "probes": enabled})
        manifest = collector_run.execute(
            cfg, on_event=emit, enabled_probes=enabled, extra_probes=extra
        )
        STATE.manifest = manifest
        STATE.run_id = manifest["collector_run_id"]
        STATE.run_dir = Path(manifest["run_dir"])
        emit({
            "event": "complete",
            "run_id": manifest["collector_run_id"],
            "datasets": len(manifest["datasets"]),
            "total_rows": manifest["total_rows"],
            "elapsed_ms": manifest["elapsed_ms"],
            "failed": manifest["failed_queries"],
            "migration_mode": manifest["migration_mode"],
        })

    return _stream(work)


@app.get("/api/discovery")
def discovery():
    if not STATE.manifest:
        raise HTTPException(409, "no discovery run yet")
    m = STATE.manifest
    run_dir = STATE.run_dir

    datasets = []
    for entry in m["datasets"]:
        payload = json.loads((run_dir / entry["file"]).read_text(encoding="utf-8"))
        rows = payload["rows"]
        columns = list(rows[0].keys()) if rows else []
        datasets.append({
            "dataset": entry["dataset"],
            "name": entry["dataset"].split(".")[-1],
            "probe": payload.get("probe"),
            "row_count": entry["row_count"],
            "columns": [c for c in columns if c != "collector_run_id"][:9],
            "rows": [
                {k: v for k, v in r.items() if k != "collector_run_id"} for r in rows[:200]
            ],
            "all_rows": rows,
            "truncated": max(0, entry["row_count"] - 200),
        })

    # Classify after every dataset is read: "is this table someone's container?"
    # cannot be answered until the mview, queue and external lists are loaded.
    # Counting over `all_rows` rather than the 200 sent for display keeps the
    # tile correct on an estate larger than the display cap.
    # `owned` applies to the tables dataset only. Phase 2's `v_user_objects`
    # filters on name prefixes alone, so a materialized view is a user object
    # while the table backing it is not -- passing `owned` here too would
    # exclude the mview itself and under-report by 4 on DBMIG_APP.
    owned = _owned_table_names(datasets)
    for d in datasets:
        key = {"tables": "table_name", "objects": "object_name"}.get(d["name"])
        rows = d.pop("all_rows")
        scope = owned if d["name"] == "tables" else frozenset()
        d["internal_count"] = (
            sum(1 for r in rows if _is_internal(r.get(key), scope))
            if key and rows and key in rows[0] else 0
        )

    facts = dict(STATE.facts)
    return {
        "run_id": m["collector_run_id"],
        "elapsed_ms": m["elapsed_ms"],
        "total_rows": m["total_rows"],
        "datasets": sorted(datasets, key=lambda d: d["name"]),
        "probes": m["probes"],
        "probes_skipped": m.get("probes_skipped", []),
        "schemas": m["schemas"],
        "failed_queries": m["failed_queries"],
        "facts": facts,
        "summary": _discovery_summary(datasets, facts),
    }


# What counts as a *user* table, kept identical to Phase 2's `v_user_tables`
# (assess/loader.py :: _create_views). Two parts, and the second is easy to miss:
#
#   1. name prefixes -- assess.loader.INTERNAL_PATTERNS
#   2. tables owned by another object -- a materialized view's container, a
#      queue table, an external table. None of these match a `$` prefix, so a
#      prefix-only check reports three of DBMIG_APP's tables as the client's.
#
# The console used to report the raw count, so a client whose schema has 9
# tables saw "Tables 21" and had no way to reconcile it. The hint said
# "including Oracle internals", which explained the number without making it
# usable. Count what a client would count, and show the rest separately.
#
# This duplicates the view's definition because the view lives in SQLite, which
# Phase 2 builds and Phase 1 has not yet run. If one changes, change both --
# `web.selftest_counts` fails when they disagree on a collected run.
_INTERNAL_PREFIXES = tuple(p.rstrip("%") for p in assess_loader.INTERNAL_PATTERNS)


def _owned_table_names(datasets: list[dict]) -> set[str]:
    """Tables that exist only to back another object, by name."""
    owned: set[str] = set()
    for name, key in (("materialized_views", "container_name"),
                      ("queues", "queue_table"),
                      ("external_tables", "table_name")):
        ds = next((d for d in datasets if d["name"] == name), None)
        for r in (ds or {}).get("all_rows") or []:
            if r.get(key):
                owned.add(str(r[key]).upper())
    return owned


def _is_internal(name: str, owned: set[str] = frozenset()) -> bool:
    n = str(name or "").upper()
    return n.startswith(_INTERNAL_PREFIXES) or n in owned


def _split_internal(datasets: list[dict], name: str) -> tuple[int, int]:
    """(user, internal) counts for a dataset.

    `internal_count` is computed in the discovery endpoint over every collected
    row, so this stays correct on an estate larger than the display cap. A
    dataset that was not collected reports zero of both.
    """
    ds = next((d for d in datasets if d["name"] == name), None)
    if not ds:
        return 0, 0
    internal = ds.get("internal_count", 0)
    return ds.get("row_count", 0) - internal, internal


def _discovery_summary(datasets: list[dict], facts: dict) -> list[dict]:
    by_name = {d["name"]: d for d in datasets}

    def count(name: str) -> int:
        return by_name.get(name, {}).get("row_count", 0)

    user_tables, internal_tables = _split_internal(datasets, "tables")
    user_objects, internal_objects = _split_internal(datasets, "objects")

    def hint(internal: int, noun: str) -> str:
        return (f"{internal} Oracle-managed {noun} excluded" if internal
                else f"every {noun.rstrip('s')} found")

    return [
        {"label": "Objects", "value": user_objects, "hint": hint(internal_objects, "objects")},
        {"label": "Tables", "value": user_tables, "hint": hint(internal_tables, "tables")},
        {"label": "Columns", "value": count("columns"), "hint": "full structure captured"},
        {"label": "Indexes", "value": count("indexes"), "hint": "with columns and expressions"},
        {"label": "Constraints", "value": count("constraints"), "hint": "PK, FK, unique, check"},
        {"label": "PL/SQL objects", "value": count("plsql_source"), "hint": "hashed for change detection"},
        {"label": "Size", "value": facts.get("size_gb", 0), "unit": "GB", "hint": "real segment bytes"},
        {"label": "Feature records", "value": count("feature_usage"), "hint": "the licence evidence"},
    ]


@app.get("/api/assess")
def assess():
    if not STATE.manifest:
        raise HTTPException(409, "no discovery run yet")

    cfg_settings = settings.load()
    disabled = set(cfg_settings["disabled_rules"])
    rules = [r for r in _all_rules() if r["rule_id"] not in disabled]

    def work(emit):
        out_dir = Path(__file__).resolve().parent.parent / "assess" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = out_dir / "assessment.sqlite"

        emit({"event": "stage", "stage": "load", "detail": "loading discovery into SQLite"})
        loaded = assess_loader.load_run(STATE.run_dir, db_path)
        emit({
            "event": "stage",
            "stage": "loaded",
            "detail": f"{loaded['total_rows']} rows across {len(loaded['tables'])} tables",
        })

        import sqlite3

        conn = sqlite3.connect(db_path)
        try:
            assess_engine.install_rules(conn, rules)
            conn.row_factory = sqlite3.Row
            findings, errors = [], []
            total = len(rules)
            for i, rule in enumerate(rules, start=1):
                emit({
                    "event": "rule",
                    "index": i,
                    "total": total,
                    "rule_id": rule["rule_id"],
                    "title": rule["title"],
                })
                try:
                    hits = [dict(r) for r in conn.execute(rule["sql"])]
                except sqlite3.Error as exc:
                    errors.append({"rule_id": rule["rule_id"], "error": str(exc)})
                    emit({"event": "rule_error", "rule_id": rule["rule_id"], "error": str(exc)})
                    continue
                for row in hits:
                    findings.append({
                        "finding_id": assess_engine._finding_id(
                            rule["rule_id"], row.get("owner"), row.get("object_name")
                        ),
                        "rule_id": rule["rule_id"],
                        "category": rule["category"],
                        "severity": rule["severity"],
                        "remediation_level": rule["remediation_level"],
                        "title": rule["title"],
                        "rationale": rule["rationale"],
                        "owner": row.get("owner"),
                        "object_name": row.get("object_name"),
                        "object_type": row.get("object_type"),
                        "detail": row.get("detail"),
                    })
                if hits:
                    emit({"event": "rule_hits", "rule_id": rule["rule_id"], "count": len(hits)})
        finally:
            conn.close()

        findings.sort(key=lambda f: (f["rule_id"], f["owner"] or "", f["object_name"] or ""))
        scores = assess_scoring.score_findings(findings)
        owners = {f["owner"] for f in findings if f["owner"]}
        recall = assess_scoring.score_against_answer_key(findings, owners=owners)
        issues = assess_engine.group_findings(findings)

        STATE.assessment = {
            "collector_run_id": STATE.run_id,
            # The report generator reads this; omitting it made assess.report
            # fail on a web-produced assessment.json.
            "source": (STATE.manifest or {}).get("source", {}),
            "assessed_at_utc": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
            "rules_evaluated": len(rules),
            "rule_errors": errors,
            "scores": scores,
            "answer_key": recall,
            "issues": issues,
            "findings": findings,
        }
        (out_dir / "assessment.json").write_text(
            json.dumps(STATE.assessment, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        emit({
            "event": "complete",
            "issues": len(issues),
            "findings": len(findings),
            "overall": scores["overall_score"],
            "recall": recall["recall"],
        })

    return _stream(work)


@app.get("/api/assessment")
def assessment():
    if not STATE.assessment:
        raise HTTPException(409, "no assessment run yet")
    return STATE.assessment


# One row per finding, not per issue. An issue groups a rule's hits for reading;
# a client filtering or pivoting the export wants the object name on its own row,
# and the grouped view is one pivot away from this while the reverse is not.
_EXPORT_COLUMNS = [
    ("rule_id", "Rule"),
    ("severity", "Severity"),
    ("applicable", "Applies to this migration"),
    ("severity_if_applicable", "Severity if applicable"),
    ("not_applicable_reason", "Why not applicable"),
    ("category", "Category"),
    ("title", "Finding"),
    ("owner", "Schema"),
    ("object_name", "Object"),
    ("object_type", "Object type"),
    ("remediation_level", "Remediation"),
    ("detail", "Detail"),
    ("rationale", "Why it matters"),
]


def _export_rows() -> list[dict]:
    """Findings flattened for export, severity-ordered.

    A not-applicable finding is exported with its reason and its original
    severity, never dropped: Phase 1 is explicit that "does not block the
    migration you chose" must not read as "clean", and a spreadsheet that
    silently omitted them would say exactly that.
    """
    order = {s: n for n, s in enumerate(["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"])}
    rows = []
    for f in STATE.assessment.get("findings", []):
        row = {}
        for key, label in _EXPORT_COLUMNS:
            v = f.get(key)
            if key == "applicable":
                v = "no" if v is False else "yes"
            row[label] = "" if v is None else str(v)
        rows.append(row)
    rows.sort(key=lambda r: (order.get(r["Severity"], 9), r["Rule"], r["Object"]))
    return rows


@app.get("/api/assessment.csv")
def assessment_csv():
    """The findings as a spreadsheet. Excel-compatible: UTF-8 with a BOM, so a
    non-ASCII object name does not arrive mojibake in the client's copy."""
    if not STATE.assessment:
        raise HTTPException(409, "no assessment run yet")
    rows = _export_rows()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[label for _, label in _EXPORT_COLUMNS],
                       lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    run = (STATE.assessment.get("collector_run_id") or "run")[:8]
    name = f"dbshift-assessment-{run}.csv"
    return Response(
        content="\ufeff" + buf.getvalue(),  # BOM: Excel reads UTF-8 only with it
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/assessment.json/download")
def assessment_json_download():
    """The whole record, for a client who wants the evidence rather than a table."""
    if not STATE.assessment:
        raise HTTPException(409, "no assessment run yet")
    run = (STATE.assessment.get("collector_run_id") or "run")[:8]
    return Response(
        content=json.dumps(STATE.assessment, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="dbshift-assessment-{run}.json"'},
    )


class UtilizationRequest(BaseModel):
    filename: str = "utilization.csv"
    content: str


@app.post("/api/utilization")
def upload_utilization(req: UtilizationRequest):
    """Accept a measured-utilization feed as CSV text.

    A rejected feed is recorded rather than discarded, so the sizing trail can
    say why it fell back to a capacity floor instead of silently doing so.
    """
    out_dir = Path(__file__).resolve().parent.parent / "sizing" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "utilization_uploaded.csv"
    path.write_text(req.content, encoding="utf-8")

    measured = sizing_run.load_utilization(path)
    measured["filename"] = req.filename
    STATE.utilization = measured
    if not measured.get("usable_for_sizing"):
        raise HTTPException(400, measured.get("reason", "unusable utilization feed"))
    return {"ok": True, "utilization": measured}


@app.delete("/api/utilization")
def clear_utilization():
    STATE.utilization = None
    return {"ok": True}


@app.get("/api/utilization/sample")
def utilization_sample():
    sample = Path(__file__).resolve().parent.parent / "sizing" / "samples" / "utilization_example.csv"
    return {
        "content": sample.read_text(encoding="utf-8") if sample.exists() else "",
        "columns": list(sizing_utilization.REQUIRED_COLUMNS),
        "sizing_metrics": list(sizing_utilization.SIZING_METRICS),
        "min_window_days": sizing_utilization.MIN_WINDOW_DAYS,
        "percentile": sizing_utilization.SIZING_PERCENTILE,
        "headroom": sizing_utilization.HEADROOM,
    }


class MigrationModeRequest(BaseModel):
    mode: str
    chosen_by: str | None = None


@app.post("/api/migration-mode")
def set_migration_mode(req: MigrationModeRequest):
    """Phase 1. Declare whether this migration is full load, or full load + CDC.

    Asked in Discovery rather than at Phase 7, because it decides whether
    OPS-001, OPS-002 and DQ-001 are blockers. Declaring a full load against a
    NOARCHIVELOG source is the normal case, not an override: there is nothing
    to override, because a full load never reads redo.

    CDC against a source that is not ready is allowed and warned about. That is
    a remediation task with a restart window attached -- refusing the
    declaration would just move the conversation somewhere this tool cannot
    record it.
    """
    try:
        mode = migration_mode.normalize(req.mode)
    except migration_mode.ModeError as exc:
        raise HTTPException(400, str(exc)) from exc

    STATE.migration_mode = mode
    STATE.mode_declared = True
    # Typed, not taken from AWS: unlike the target engine this is a statement
    # about the client's outage tolerance, and the person making it need not
    # hold credentials in this account.
    STATE.mode_chosen_by = (req.chosen_by or "").strip() or None

    record = migration_mode.decide(
        mode,
        chosen_by=STATE.mode_chosen_by,
        log_mode=STATE.facts.get("log_mode"),
        supplemental_min=STATE.facts.get("supplemental_logging"),
        declared=True,
    )
    return {"ok": True, **record}


class EngineRequest(BaseModel):
    engine: str


@app.post("/api/engine")
def set_engine(req: EngineRequest):
    """Choose the migration target.

    Validated against the assessment when one exists, so a path the estate
    blocks cannot be selected. Before sizing has run there is nothing to
    validate against; the choice is still refused at sizing time, because
    `sizing.target.choose` checks again there.
    """
    engine = req.engine.upper()
    if engine not in sizing_target.TARGETS:
        raise HTTPException(400, f"unknown target {req.engine!r}")
    if STATE.sizing and STATE.sizing.get("target_assessment"):
        try:
            sizing_target.choose(engine, STATE.sizing["target_assessment"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    STATE.engine = engine
    # Taken from the AWS identity where credentials exist, never typed into the
    # browser. Unlike a cutover approval this is not an authorisation, so it
    # does not *require* credentials -- a target may be chosen while offline,
    # and the record then says the choice was unattributed rather than naming
    # someone who did not make it.
    STATE.engine_chosen_by = None
    try:
        ident = _aws_session().client("sts").get_caller_identity()
        STATE.engine_chosen_by = provision_deploy.identity_name(ident["Arn"])
    except Exception:  # noqa: BLE001 -- no credentials is an ordinary state here
        pass
    return {"ok": True, "engine": engine, "label": sizing_target.LABEL[engine],
            "chosen_by": STATE.engine_chosen_by}


@app.get("/api/size")
def size():
    if not STATE.manifest:
        raise HTTPException(409, "no discovery run yet")

    def work(emit):
        measured = STATE.utilization if (STATE.utilization or {}).get("usable_for_sizing") else None
        out = sizing_run.execute(
            STATE.run_dir,
            Path(__file__).resolve().parent.parent / "sizing" / "output",
            measured=measured,
            on_event=emit,
            engine=STATE.engine,
            chosen_by=STATE.engine_chosen_by,
            # Phase 4b measures the real cost of the heterogeneous path. Where it
            # has run, the assessment uses those compile results instead of
            # reporting the effort as unknown.
            conversion=STATE.conversion,
        )
        STATE.sizing = out
        d = out["decision"]
        emit({
            "event": "complete",
            "engine": d["engine"],
            "engine_label": d["engine_label"],
            "edition": d["edition"],
            "licence_model": d["licence_model"],
            "instance_class": d["instance_class"],
            "storage_gb": d["storage_gb"],
            "overrides": d["override_count"],
            "warnings": d["warning_count"],
            "basis": out["facts"]["utilization"]["basis"],
        })

    return _stream(work)


@app.get("/api/sizing")
def sizing():
    if not STATE.sizing:
        raise HTTPException(409, "no sizing run yet")
    return STATE.sizing


class RehearsalRequest(BaseModel):
    dsn: str = ""
    user: str = "dbmig_rehearsal"
    password: str = ""
    schema_name: str = "DBMIG_REHEARSAL"


def _model_mode() -> str:
    """Whether the model tier can answer right now.

    Checked rather than assumed: the console used to hardcode "static" because
    Bedrock invoke was blocked on the old account, and that claim then outlived
    the account. DBSHIFT_MODEL_MODE forces it either way for a demo.
    """
    forced = os.environ.get("DBSHIFT_MODEL_MODE")
    if forced in ("off", "static", "live"):
        return forced
    # `verified` is set only from a passing `python -m bedrock.verify`, i.e. an
    # actual invocation. Trusting it here avoids billing for a model call just to
    # decide a label on every page load. A tier that is bound but unverified is
    # treated as unavailable rather than tried and failed mid-phase.
    try:
        from bedrock.client import load_config
        tiers = load_config().get("tiers") or {}
        return "live" if tiers.get("reasoning", {}).get("verified") else "static"
    except Exception:  # noqa: BLE001
        return "static"


def _rehearsal_target():
    """The registered rehearsal copy, or None. Held in memory like every other
    credential in this process -- never written to disk."""
    return STATE.rehearsal


@app.post("/api/rehearsal")
def set_rehearsal(req: RehearsalRequest):
    """Register a rehearsal copy, and prove it is usable before accepting it.

    Registering something unreachable would let the dry-run gate look configured
    while every fix silently failed at connect.
    """
    from remediate import rehearsal as rehearsal_mod

    if not req.dsn.strip():
        STATE.rehearsal = None
        STATE.rehearsal_dsn = None
        return {"ok": True, "rehearsal_dsn": None}

    target = rehearsal_mod.RehearsalTarget(
        dsn=req.dsn.strip(),
        user=req.user.strip() or "dbmig_rehearsal",
        password=req.password,
        schema=(req.schema_name.strip() or "DBMIG_REHEARSAL").upper(),
        source_schema=(STATE.schemas[0] if STATE.schemas else "DBMIG_APP"),
    )
    check = rehearsal_mod.check_target(target)
    if not check["ok"]:
        raise HTTPException(400, check["detail"])

    STATE.rehearsal = target
    STATE.rehearsal_dsn = target.dsn
    return {"ok": True, "rehearsal_dsn": target.dsn, "detail": check["detail"]}


@app.get("/api/remediate")
def remediate():
    if not STATE.assessment:
        raise HTTPException(409, "no assessment run yet")

    def work(emit):
        result = remediate_plan.build(
            STATE.assessment,
            # The model proposes; the gates still decide. Nothing reaches the
            # database because a model wrote it -- a model-authored fix goes
            # through the same screens as any other, and is labelled with the
            # model id so a reviewer knows what produced the text.
            model_mode=_model_mode(),
            rehearsal_target=_rehearsal_target(),
            on_event=emit,
        )
        STATE.remediation = result
        out_dir = Path(__file__).resolve().parent.parent / "remediate" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "remediation_plan.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        emit({
            "event": "complete",
            "totals": result["totals"],
            "fixes_with_sql": result["fixes_with_sql"],
            "blockers": result["what_is_in_the_way"],
            "rehearsal": result["rehearsal_configured"],
        })

    return _stream(work)


@app.get("/api/remediation")
def remediation():
    if not STATE.remediation:
        raise HTTPException(409, "no remediation plan yet")
    return STATE.remediation


class PgTargetRequest(BaseModel):
    dsn: str = "localhost:5432/dbshift"
    user: str = "dbshift"
    password: str = ""


@app.post("/api/pgtarget")
def set_pg_target(req: PgTargetRequest):
    """Register the PostgreSQL the compile gate creates into (and rolls back
    out of). Proven reachable before it is accepted, for the same reason the
    rehearsal copy is: a registered-but-unreachable target would let the gate
    look configured while every conversion silently blocked."""
    if not req.dsn.strip():
        STATE.pg_target = None
        STATE.pg_dsn = None
        return {"ok": True, "pg_dsn": None}
    try:
        target = convert_target.PgTarget.parse(req.dsn, req.user.strip(), req.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    check = convert_target.check_target(target)
    if not check["ok"]:
        raise HTTPException(400, check["detail"])
    STATE.pg_target = target
    STATE.pg_dsn = target.dsn
    return {"ok": True, "pg_dsn": target.dsn, "detail": check["detail"]}


@app.get("/api/convert")
def convert():
    """Phase 4b. Needs only the discovery run -- it reads the PL/SQL text and the
    catalogues Phase 1 collected. Compiles on the registered PostgreSQL, if any,
    and applies nothing anywhere."""
    if not STATE.run_dir:
        raise HTTPException(409, "no discovery run yet")

    def work(emit):
        inv = convert_inventory.load(STATE.run_dir)
        result = convert_plan.build(
            inv,
            # Live for the same reason as Phase 4, and with the same gates.
            model_mode=_model_mode(),
            pg_target=STATE.pg_target,
            on_event=emit,
        )
        STATE.conversion = result
        convert_plan.write(result)
        emit({
            "event": "complete",
            "totals": result["totals"],
            "blockers": result["what_is_in_the_way"],
            "pg": result["pg_target"],
        })

    return _stream(work)


@app.get("/api/conversion")
def conversion():
    if not STATE.conversion:
        raise HTTPException(409, "no conversion plan yet")
    return STATE.conversion


def _report_data() -> dict:
    """Phase 10. The migration assessment report -- SCT-style conversion
    assessment plus DMS pre-migration assessment -- built from the records this
    console holds, on every request, so it can never drift from them."""
    if not STATE.assessment:
        raise HTTPException(409, "run the assessment first; the report is built from its findings")
    objects = []
    if STATE.run_dir and (STATE.run_dir / "objects.json").exists():
        objects = json.loads((STATE.run_dir / "objects.json").read_text(encoding="utf-8"))["rows"]
    return report_build.build(
        assessment=STATE.assessment, conversion=STATE.conversion, sizing=STATE.sizing, gate=STATE.gate,
        objects=objects, provision=STATE.provision,
        validation=report_build._load("validate/output/validation_report.json"),
        certificate=report_build._load("cutover/output/certificate.json"),
        migration=report_build._load("migrate/output/migration_run.json"),
        ddl=report_build._load("convert/output/schema_ddl.json"),
        dms_record=report_build._load("dms/output/dms_run.json"),
    )


@app.get("/api/report", response_class=HTMLResponse)
def report():
    """The printable report, as a page of its own."""
    return report_render.render(_report_data())


@app.get("/api/report.json")
def report_json():
    """The same report as data, for the Phase 10 screen in the console. One
    builder feeds both, so the screen can never disagree with the page."""
    return _report_data()


@app.get("/api/assessment.xlsx")
def assessment_xlsx():
    """The SCT-shaped assessment as a spreadsheet — the artefact a client
    filters and forwards. Four sheets: summary, action items, every finding,
    stored code by type.

    Built from the same Phase 10 record the HTML report renders, so the
    spreadsheet cannot say something the report does not. It carries the Phase 2
    findings alongside, because those live in the assessment record rather than
    the report's rolled-up counts.
    """
    if not STATE.assessment:
        raise HTTPException(409, "no assessment run yet")
    rec = _report_data()
    data = report_export.workbook(rec, STATE.assessment)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{report_export.filename(rec, "xlsx")}"'})


@app.get("/api/assessment.pdf")
def assessment_pdf():
    """The same assessment as a PDF — the artefact that gets attached to an
    email. Paginated rather than a print of the HTML: a PDF cannot be expanded,
    so the action items are a table with their complexity spelled out."""
    if not STATE.assessment:
        raise HTTPException(409, "no assessment run yet")
    rec = _report_data()
    data = report_export.pdf(rec, STATE.assessment)
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="{report_export.filename(rec, "pdf")}"'})


class WaiverRequest(BaseModel):
    rule_id: str
    approved_by: str
    reason: str


@app.post("/api/waivers")
def add_waiver(req: WaiverRequest):
    """Accept a blocker, on the record.

    Validation lives in blocker.policy so the console and the CLI cannot drift
    on what counts as a real waiver. A waiver may never be anonymous.
    """
    waiver = req.model_dump()
    problems = blocker_policy.validate_waiver(waiver)
    if problems:
        raise HTTPException(400, "; ".join(problems))
    STATE.waivers = [w for w in STATE.waivers if w["rule_id"] != waiver["rule_id"]]
    STATE.waivers.append(waiver)
    return {"ok": True, "waivers": STATE.waivers}


@app.delete("/api/waivers/{rule_id}")
def remove_waiver(rule_id: str):
    STATE.waivers = [w for w in STATE.waivers if w["rule_id"] != rule_id]
    return {"ok": True, "waivers": STATE.waivers}


@app.get("/api/gate")
def gate():
    """Phase 5. Fast and deterministic -- no stream, nothing to watch."""
    if not STATE.assessment:
        raise HTTPException(409, "no assessment run yet")
    decision = blocker_gate.evaluate(STATE.assessment, STATE.remediation, STATE.waivers)
    STATE.gate = decision

    out_dir = Path(__file__).resolve().parent.parent / "blocker" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "gate_decision.json").write_text(
        json.dumps(decision, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return decision


# --------------------------------------------------------------------------- phase 6


def _aws_session():
    import boto3
    return boto3.Session(profile_name=provision_run.DEFAULT_PROFILE)


def _provision_plan() -> dict | None:
    if STATE.provision:
        return STATE.provision
    path = provision_run.OUTPUT / "provision_plan.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


@app.get("/api/provision/options")
def provision_options():
    """What the Provision screen needs to draw the instance picker and the
    configuration form: the classes on offer, the fields with their defaults and
    help, and the properties that are deliberately not overridable, each with the
    reason it is locked."""
    opts = provision_overrides.describe()
    d = ((STATE.sizing or {}).get("decision") or {})
    # What Phase 3 derived, so the form can open on it and show it as the value a
    # person is choosing to depart from.
    opts["derived"] = {
        "instance_class": d.get("instance_class"),
        "vcpu": d.get("vcpu"),
        "memory_gib": d.get("memory_gib"),
        "basis": (((STATE.sizing or {}).get("facts") or {}).get("utilization") or {}).get("basis"),
    }
    opts["current"] = STATE.provision_overrides
    return opts


class OverrideRequest(BaseModel):
    instance_class: str | None = None
    configuration: dict | None = None
    reason: str = ""


@app.post("/api/provision/options")
def set_provision_options(req: OverrideRequest):
    """Validate and hold a person's choices for the next render.

    Validated here rather than at render time so a bad value is refused while the
    person is still looking at the form, with a message naming the field. The
    render receives only records that have already passed.
    """
    d = ((STATE.sizing or {}).get("decision") or {})
    derived = d.get("instance_class")
    if not derived:
        raise HTTPException(409, "no sizing decision yet -- run Phase 3 first")

    instance = config = None
    try:
        if req.instance_class and req.instance_class != derived:
            instance = provision_overrides.validate_instance_class(
                req.instance_class, derived, req.reason)
        if req.configuration is not None:
            config = provision_overrides.validate_config(req.configuration, req.reason)
            if not config["changed"]:
                config = None
    except provision_overrides.OverrideError as exc:
        raise HTTPException(400, str(exc))

    STATE.provision_overrides = {"instance_class": instance, "configuration": config}
    return {"ok": True, "overrides": STATE.provision_overrides,
            "any": bool(instance or config)}


@app.delete("/api/provision/options")
def clear_provision_options():
    """Back to the derived values. Kept explicit rather than posting an empty
    form, so 'I changed my mind' is a different action from 'I submitted nothing'."""
    STATE.provision_overrides = {"instance_class": None, "configuration": None}
    return {"ok": True}


@app.get("/api/provision")
def provision():
    """Phase 6 render + read-only preflight. Streams its stages; creates nothing."""
    def work(emit):
        ov = STATE.provision_overrides or {}
        plan = provision_run.execute(session=_aws_session(),
                                     price_file=provision_run.default_price_file(), on_event=emit,
                                     instance_override=ov.get("instance_class"),
                                     config_override=ov.get("configuration"))
        STATE.provision = plan
        est = (plan.get("cost") or {}).get("estimate") or {}
        emit({"event": "complete", "ready": plan["ready"], "stack": plan.get("stack_name"),
              "hourly": est.get("instance_per_hour"),
              "warnings": sum(c["status"] == "warn" for c in plan["checks"]),
              "failures": sum(c["status"] in ("fail", "blocked") for c in plan["checks"])})

    return _stream(work)


@app.get("/api/provision/plan")
def provision_plan():
    plan = _provision_plan()
    if not plan:
        raise HTTPException(409, "not rendered yet")
    return plan


class DeployRequest(BaseModel):
    confirm_account: str
    accept_hourly: float
    halt_reason: str = ""


_DEPLOY: dict = {"thread": None, "error": None}


@app.post("/api/provision/deploy")
def provision_deploy_route(req: DeployRequest):
    """Start a deploy -- the one console action that bills.

    Every refusal in provision.deploy is answered here, synchronously, as a 400
    with its reason. Only a deploy that passed all of them carries on in the
    background; progress is read from /api/provision/status, the same view that
    follows a deploy started from the CLI.
    """
    running = _DEPLOY["thread"]
    if running is not None and running.is_alive():
        raise HTTPException(409, "a deploy started from this console is already running")

    passed = threading.Event()
    outcome: dict = {}
    _DEPLOY["error"] = None

    def on_event(e):
        # The password step is the first side effect, so reaching it means every
        # refusal is behind us.
        if e.get("stage") == "password":
            passed.set()

    def runner():
        try:
            outcome["record"] = provision_deploy.deploy(
                _aws_session(), confirm_account=req.confirm_account,
                accept_hourly=req.accept_hourly, halt_reason=req.halt_reason or None,
                price_file=provision_run.default_price_file(), on_event=on_event)
        except provision_deploy.DeployRefused as exc:
            outcome["refused"] = str(exc)
        except Exception as exc:  # noqa: BLE001 -- shown in status, never swallowed
            outcome["error"] = _DEPLOY["error"] = str(exc).splitlines()[0]
        finally:
            passed.set()

    thread = threading.Thread(target=runner, daemon=True)
    _DEPLOY["thread"] = thread
    thread.start()
    passed.wait(timeout=240)
    if "refused" in outcome:
        raise HTTPException(400, outcome["refused"])
    if "error" in outcome:
        raise HTTPException(500, outcome["error"])
    return {"started": True}


@app.get("/api/provision/status")
def provision_status():
    """Where the target stands, read live from CloudFormation. Read-only."""
    plan = _provision_plan()
    if not plan or not plan.get("stack_name"):
        raise HTTPException(409, "not rendered yet")
    stack = plan["stack_name"]
    history_path = provision_run.OUTPUT / "deployments.jsonl"
    history = ([json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()
                if line.strip()] if history_path.exists() else [])
    running = _DEPLOY["thread"]
    out = {"stack_name": stack, "history": history[-6:], "console_error": _DEPLOY["error"],
           "console_deploy_running": bool(running and running.is_alive())}
    try:
        cfn = _aws_session().client("cloudformation", region_name=provision_policy.REGION)
        try:
            s = cfn.describe_stacks(StackName=stack)["Stacks"][0]
        except ClientError as exc:
            if "does not exist" not in str(exc):
                raise
            out.update(exists=False, status="NOT_DEPLOYED", events=[], outputs={})
            return out
        events = cfn.describe_stack_events(StackName=stack)["StackEvents"][:60]
        out.update(
            exists=True, status=s["StackStatus"], created=str(s["CreationTime"]),
            stack_id=s["StackId"], region=provision_policy.REGION,
            outputs={o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])},
            events=[{"at": str(e["Timestamp"]), "resource": e["LogicalResourceId"],
                     "status": e["ResourceStatus"], "reason": e.get("ResourceStatusReason") or ""}
                    for e in reversed(events)],
        )
    except Exception as exc:  # noqa: BLE001 -- expired credentials, most often
        out["aws_error"] = str(exc).splitlines()[0]
    return out


@app.get("/api/provision/template")
def provision_template():
    """The template CloudFormation actually ran -- read back from the live stack,
    not the local render, which may have been re-rendered since. Read-only."""
    plan = _provision_plan()
    if not plan or not plan.get("stack_name"):
        raise HTTPException(409, "not rendered yet")
    cfn = _aws_session().client("cloudformation", region_name=provision_policy.REGION)
    try:
        body = cfn.get_template(StackName=plan["stack_name"], TemplateStage="Original")["TemplateBody"]
    except ClientError as exc:
        raise HTTPException(409, str(exc).splitlines()[0])
    return {"stack_name": plan["stack_name"],
            "template": body if isinstance(body, dict) else json.loads(body)}


@app.post("/api/provision/verify")
def provision_verify_route():
    """Log in to the created target and compare it with the render. Read-only."""
    plan = _provision_plan()
    if not plan or not plan.get("rendered"):
        raise HTTPException(409, "not rendered yet")
    session = _aws_session()
    cfn = session.client("cloudformation", region_name=provision_policy.REGION)
    try:
        s = cfn.describe_stacks(StackName=plan["stack_name"])["Stacks"][0]
    except ClientError as exc:
        raise HTTPException(409, str(exc).splitlines()[0])
    if s["StackStatus"] != "CREATE_COMPLETE":
        raise HTTPException(409, f"the stack is {s['StackStatus']}; verify runs on CREATE_COMPLETE")
    deployed = {"stack_name": plan["stack_name"],
                "outputs": {o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])}}
    checks = provision_verify.verify(session, deployed, plan)
    (provision_run.OUTPUT / "verify.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    return {"checks": checks}


class KillRequest(BaseModel):
    mode: str
    confirm: str


@app.get("/api/killswitch")
def killswitch_scan():
    """What is billing right now. Read-only."""
    try:
        return killswitch_run.execute(_aws_session(), mode=None, confirm=None,
                                      regions=[provision_policy.REGION])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc).splitlines()[0])


@app.post("/api/killswitch")
def killswitch_act(req: KillRequest):
    """Stop or destroy what this project owns. The account id must be typed."""
    if req.mode not in ("stop", "destroy"):
        raise HTTPException(400, "mode must be stop or destroy")
    try:
        return killswitch_run.execute(_aws_session(), mode=req.mode, confirm=req.confirm,
                                      regions=[provision_policy.REGION])
    except PermissionError as exc:
        raise HTTPException(400, str(exc))


# --------------------------------------------------------------------------- phase 7


class MigrateCredentials(BaseModel):
    source_owner_password: str = ""


@app.post("/api/migrate/credentials")
def migrate_credentials(req: MigrateCredentials):
    """Held in process memory only, like every password in this console, and never
    put in a URL -- the stream below is a GET, so it cannot carry one."""
    STATE.migrate_owner_password = req.source_owner_password or None
    return {"ok": True, "held": bool(STATE.migrate_owner_password)}


@app.get("/api/migrate/plan")
def migrate_plan():
    return {"steps": migrate_run.plan(), "last_run": migrate_run.last_run()}


# --- Phase 7, heterogeneous path: AWS DMS ------------------------------------
# Data Pump writes an Oracle-only format, so a PostgreSQL target cannot use it.
# DMS is not an alternative engine for the same job -- it is the only route, and
# it is the one that makes change data capture (and so a short cutover) possible.

@app.get("/api/dms/plan")
def dms_plan(migration_type: str = dms_policy.FULL_LOAD):
    """Everything knowable without creating anything. Free, and creates nothing."""
    if migration_type not in dms_policy.MIGRATION_TYPES:
        raise HTTPException(400, f"unknown migration type {migration_type!r}")
    try:
        session = _aws_session()
    except Exception:  # noqa: BLE001 -- planning works offline; only the billing check needs AWS
        session = None
    try:
        return dms_run.plan(session, migration_type=migration_type)
    except FileNotFoundError as exc:
        raise HTTPException(409, f"a record this phase needs is missing: {exc}") from exc


@app.get("/api/dms/status")
def dms_status():
    """Where a running task stands. Read-only."""
    out = dms_run.status(_aws_session())
    if out is None:
        raise HTTPException(409, "no DMS task has been started from this console")
    return out


class DmsExecute(BaseModel):
    confirm_account: str
    migration_type: str = dms_policy.FULL_LOAD
    source_password: str = ""
    target_password: str = ""
    source_host: str = "localhost"
    source_port: int = 1521
    source_database: str = "XEPDB1"


@app.post("/api/dms/execute")
def dms_execute(req: DmsExecute):
    """Create the replication instance, endpoints and task, then run it.

    **This bills**, and unlike an RDS instance a replication instance bills for
    as long as it exists whether or not a task is running. The account id is
    typed back for the same reason a deploy asks for it.
    """
    if req.migration_type not in dms_policy.MIGRATION_TYPES:
        raise HTTPException(400, f"unknown migration type {req.migration_type!r}")
    plan = dms_run.plan(None, migration_type=req.migration_type)
    estate = plan["estate"]

    source = {"engine": "oracle", "host": req.source_host, "port": req.source_port,
              "user": estate,
              "password": req.source_password
              or os.environ.get("DBSHIFT_SOURCE_OWNER_PASSWORD", ""),
              "database": req.source_database}
    target = {"engine": "postgres" if plan["heterogeneous"] else "oracle",
              "host": "", "port": 5432 if plan["heterogeneous"] else 1521,
              "user": "dbshiftadm",
              "password": req.target_password or os.environ.get("DBSHIFT_PG_PASSWORD", ""),
              "database": "dbshift"}
    if not source["password"] or not target["password"]:
        raise HTTPException(400, "both the source owner and target passwords are needed; "
                                 "they are held in memory only and never written down")

    def work(emit):
        try:
            rec = dms_run.execute(_aws_session(), confirm_account=req.confirm_account,
                                  migration_type=req.migration_type, source=source,
                                  target=target, on_event=emit)
        except (PermissionError, ValueError) as exc:
            emit({"event": "refused", "message": str(exc)})
            return
        emit({"event": "complete", "status": rec["status"],
              "tables": len(rec.get("tables") or []),
              "errored": rec.get("tables_errored", 0)})

    return _stream(work)


@app.get("/api/migrate")
def migrate(resolve_external: bool = False, fresh_export: bool = False, from_step: str = ""):
    """Phase 7. Streams every step's start, backend log lines and result."""
    import os

    opts = migrate_steps.Options(
        source_owner_password=STATE.migrate_owner_password or os.environ.get("DBSHIFT_SOURCE_OWNER_PASSWORD"),
        collector_password=(STATE.password if STATE.connected else None)
        or os.environ.get("DBSHIFT_COLLECTOR_PASSWORD"),
        collector_user=STATE.user or "dbmig_collector",
        source_dsn=STATE.dsn or collector_config.DEFAULT_DSN,
        resolve_external_via_s3=resolve_external, fresh_export=fresh_export,
        resume_from=from_step or None)

    def work(emit):
        rec = migrate_run.execute(_aws_session(), opts, on_event=emit)
        emit({"event": "complete", "status": rec["status"], "stopped_at": rec.get("stopped_at"),
              "reason": rec.get("reason")})

    return _stream(work)


# --------------------------------------------------------------------------- phase 8


@app.get("/api/validate/plan")
def validate_plan():
    from validate.levels import LEVELS
    return {"levels": [{"level": n, "title": t, "why": w} for n, t, w, _ in LEVELS],
            "last": validate_run.last_report(), "migration_runs": migrate_run.runs()}


@app.get("/api/validate")
def validate(checksum: bool = True):
    """Phase 8. Read-only on both databases: every statement is a SELECT."""
    import os

    opts = validate_context.Options(
        collector_password=(STATE.password if STATE.connected else None)
        or os.environ.get("DBSHIFT_COLLECTOR_PASSWORD"),
        collector_user=STATE.user or "dbmig_collector",
        source_dsn=STATE.dsn or collector_config.DEFAULT_DSN,
        checksum=checksum,
        # The engine comes from the Phase 3 decision, never a guess: it decides
        # how every comparison is built, and Oracle rules against a PostgreSQL
        # target would report differences that are not there.
        target_engine=STATE.engine,
        # No password is passed: STATE.pg_target is the *local Docker* compile
        # container from Phase 4b, not the provisioned RDS instance, and reusing
        # its password here would try the wrong credential against the wrong
        # database. The validator reads the real one from SSM, the same place
        # the deploy put it.
        target_password=None)

    def work(emit):
        report = validate_run.execute(_aws_session(), opts, on_event=emit)
        emit({"event": "complete", "status": report.get("status"), "reason": report.get("reason"),
              "mismatches": report.get("mismatches", 0)})

    return _stream(work)


@app.get("/api/cutover")
def cutover():
    """Phase 9's certificate. Read-only: it reads records, the report and the
    instance's tag, and changes nothing. Safe to open at any time."""
    try:
        return cutover_run.certificate(_aws_session())
    except Exception as exc:  # noqa: BLE001 -- the reason belongs on screen
        raise HTTPException(400, str(exc).splitlines()[0] or type(exc).__name__)


class CutoverApproval(BaseModel):
    reason: str


@app.post("/api/cutover/approve")
def cutover_approve(req: CutoverApproval):
    """Record who accepts the cutover. The approver is taken from the AWS
    identity, never from the browser -- a name typed into a form is not an
    approval."""
    try:
        return cutover_run.approve(_aws_session(), req.reason)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(400, str(exc))


class CutoverExecute(BaseModel):
    confirm_account: str


@app.post("/api/cutover/execute")
def cutover_execute(req: CutoverExecute):
    """Run the target-side cutover steps. Every refusal in cutover.run answers
    here as a 400 with its reason, before anything is touched."""
    def work(emit):
        try:
            record = cutover_run.execute(_aws_session(), confirm_account=req.confirm_account,
                                         on_event=emit)
        except (PermissionError, ValueError) as exc:
            emit({"event": "refused", "message": str(exc)})
            return
        emit({"event": "complete", **record})

    return _stream(work)


def main() -> int:
    import uvicorn

    print("DBShift console -> http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
