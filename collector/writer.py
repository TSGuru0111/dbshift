from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .db import sha256_text

# Bump when the envelope shape changes. The AWS ingest step keys off this.
SCHEMA_VERSION = 1


class _Encoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (bytes, bytearray)):
            return o.hex()
        return super().default(o)


def _dump(payload: dict) -> str:
    return json.dumps(payload, cls=_Encoder, indent=2, ensure_ascii=False)


def write_dataset(
    run_dir: Path,
    run_id: str,
    dataset: str,
    rows: list[dict],
    probe: str,
    source_queries: list[str],
    source: dict,
    collected_at: str,
    columns: list[str] | None = None,
) -> dict:
    """One file per dataset. The row list is what a later push step POSTs verbatim."""
    stamped = [{"collector_run_id": run_id, **row} for row in rows]
    # `columns` is the shape the query returned, which an empty dataset cannot
    # convey through `rows` alone. Consumers that build a table from a dataset
    # (assess/loader.py) need it, or an estate with no scheduler jobs produces a
    # jobs table with no columns and every rule against it fails to parse.
    declared = list(columns or [])
    if declared and "collector_run_id" not in declared:
        declared = ["collector_run_id"] + declared
    envelope = {
        "collector_run_id": run_id,
        "dataset": dataset,
        "schema_version": SCHEMA_VERSION,
        "collected_at_utc": collected_at,
        "probe": probe,
        "source_queries": source_queries,
        "source": source,
        "columns": declared,
        "row_count": len(stamped),
        "rows": stamped,
    }
    path = run_dir / f"{dataset.split('.')[-1]}.json"
    text = _dump(envelope)
    path.write_text(text, encoding="utf-8")
    return {
        "dataset": dataset,
        "file": path.name,
        "row_count": len(stamped),
        "bytes": len(text.encode("utf-8")),
        "file_sha256": sha256_text(text),
    }


def write_manifest(run_dir: Path, manifest: dict) -> Path:
    path = run_dir / "manifest.json"
    path.write_text(_dump(manifest), encoding="utf-8")
    return path


def append_run_ledger(output_dir: Path, entry: dict) -> None:
    """Append-only local ledger. Never rewritten, mirrors the metadata repo rule."""
    with (output_dir / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, cls=_Encoder, ensure_ascii=False) + "\n")
