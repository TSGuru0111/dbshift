"""Local console for DBShift.

Runs on the operator's machine, next to the source database. The browser never
talks to Oracle -- this process does, using the same collector and assessment
modules the CLI uses, so the UI can never show a result the CLI would not.

    python -m web.server         then open http://127.0.0.1:8765

The database password is held in memory for the life of the process and is never
written to disk, logged, or returned to the browser.
"""

from __future__ import annotations

import json
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
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from assess import engine as assess_engine
from assess import loader as assess_loader
from assess import scoring as assess_scoring
from collector import config as collector_config
from collector import run as collector_run
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


@app.delete("/api/{kind}/{identifier}")
def delete_custom(kind: str, identifier: str):
    singular = {"probes": "probe", "rules": "rule"}.get(kind)
    if not singular:
        raise HTTPException(404, "unknown collection")
    try:
        settings.delete_custom(singular, identifier)
    except settings.SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


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
            "truncated": max(0, entry["row_count"] - 200),
        })

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


def _discovery_summary(datasets: list[dict], facts: dict) -> list[dict]:
    by_name = {d["name"]: d for d in datasets}

    def count(name: str) -> int:
        return by_name.get(name, {}).get("row_count", 0)

    return [
        {"label": "Objects", "value": count("objects"), "hint": "every schema object found"},
        {"label": "Tables", "value": count("tables"), "hint": "including Oracle internals"},
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


def main() -> int:
    import uvicorn

    print("DBShift console -> http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
