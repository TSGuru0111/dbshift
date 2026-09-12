"""What Phase 1 collected about stored code, loaded for conversion.

Reads the collector run directly -- the PL/SQL text (inline or externalised to
plsql_source/<sha256>.txt), stored compilation errors, invalid objects, and the
column, table, trigger and type catalogues the converter and the shadow schema
need. Nothing is recomputed; the SHA-256 on each object is the collector's."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_OUTPUT = ROOT / "collector" / "output"

CODE_TYPES = ("TYPE", "TYPE BODY", "FUNCTION", "PROCEDURE", "PACKAGE", "PACKAGE BODY", "TRIGGER")


def latest_run(collector_output: Path = COLLECTOR_OUTPUT) -> Path:
    runs = [d for d in collector_output.iterdir() if d.is_dir() and (d / "manifest.json").exists()]
    if not runs:
        raise SystemExit(f"no collector runs found in {collector_output}")
    return max(runs, key=lambda d: d.stat().st_mtime)


def dataset(run_dir: Path, name: str) -> dict:
    path = run_dir / f"{name}.json"
    if not path.exists():
        return {"columns": [], "rows": [], "collector_run_id": None}
    return json.loads(path.read_text(encoding="utf-8"))


def load(run_dir: Path) -> dict:
    src = dataset(run_dir, "plsql_source")
    objects = []
    for row in src["rows"]:
        text = row.get("source_text") or ""
        if row.get("source_text_truncated") and row.get("source_text_file"):
            text = (run_dir / row["source_text_file"]).read_text(encoding="utf-8")
        objects.append(
            {
                "owner": row["owner"],
                "object_type": row["object_type"],
                "object_name": row["object_name"],
                "source_sha256": row["source_sha256"],
                "line_count": row.get("line_count"),
                "char_length": row.get("char_length"),
                "source_text": text,
            }
        )

    errors: dict[tuple, list[str]] = {}
    for row in dataset(run_dir, "plsql_errors")["rows"]:
        if (row.get("attribute") or "ERROR").upper() != "ERROR":
            continue
        key = (row["owner"], row["type"], row["name"])
        errors.setdefault(key, []).append(f"line {row.get('line')}: {row.get('text')}")

    invalid = {
        (r["owner"], r["object_type"], r["object_name"]): r.get("status")
        for r in dataset(run_dir, "invalid_objects")["rows"]
    }

    columns = dataset(run_dir, "columns")["rows"]
    tables = dataset(run_dir, "tables")["rows"]
    triggers = {(r["owner"], r["trigger_name"]): r for r in dataset(run_dir, "triggers")["rows"]}
    types = {(r["owner"], r["type_name"].upper()): r for r in dataset(run_dir, "types")["rows"]}

    owners = Counter(o["owner"] for o in objects) or Counter(t["owner"] for t in tables)
    estate = owners.most_common(1)[0][0] if owners else None

    return {
        "collector_run_id": src.get("collector_run_id") or dataset(run_dir, "tables").get("collector_run_id"),
        "run_dir": run_dir,
        "estate": estate,
        "objects": objects,
        "errors_by_object": errors,
        "invalid_objects": invalid,
        "columns": columns,
        "tables": tables,
        "triggers": triggers,
        "types": types,
    }


def table_names(inv: dict, owner: str) -> set[str]:
    return {t["table_name"].upper() for t in inv["tables"] if t["owner"] == owner}


def columns_of(inv: dict, owner: str, table: str) -> list[dict]:
    rows = [
        c for c in inv["columns"]
        if c["owner"] == owner and c["table_name"].upper() == table.upper()
        and (c.get("hidden_column") or "NO") != "YES"
    ]
    return sorted(rows, key=lambda c: c.get("column_id") or 0)
