"""The four upstream records, and the check that they describe the same estate.

Phase 6 reads Phase 2's assessment, Phase 3's sizing, Phase 4's plan and Phase
5's gate. Each is written to its own output folder by whichever run touched it
last, so nothing guarantees they agree. On 2026-09-11 they did not twice: once
the sizing was another estate's entirely, once two records came from a console
run and two from the CLI. Provisioning from that mix would stand up a target
against facts nobody checked together, so a mismatch is a failure, not a warning.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATHS = {
    "assessment": ROOT / "assess" / "output" / "assessment.json",
    "sizing": ROOT / "sizing" / "output" / "sizing.json",
    "remediation": ROOT / "remediate" / "output" / "remediation_plan.json",
    "gate": ROOT / "blocker" / "output" / "gate_decision.json",
}
COLLECTOR_OUTPUT = ROOT / "collector" / "output"


class RecordError(RuntimeError):
    pass


def load(paths: dict | None = None) -> dict:
    paths = paths or PATHS
    records = {}
    for name, path in paths.items():
        path = Path(path)
        if not path.exists():
            raise RecordError(f"no {name} record at {path} -- run that phase first")
        records[name] = json.loads(path.read_text(encoding="utf-8"))
    return records


def consistency(records: dict) -> dict:
    ids = {name: r.get("collector_run_id") for name, r in records.items()}
    distinct = set(ids.values())
    if len(distinct) == 1 and None not in distinct:
        return {"name": "records_consistent", "status": "pass",
                "detail": f"all four records come from collector run {ids['assessment']}",
                "remedy": None}
    return {
        "name": "records_consistent", "status": "fail",
        "detail": "records come from different collector runs: "
                  + ", ".join(f"{k}={v}" for k, v in ids.items()),
        "remedy": "Re-run the phases whose run id differs (sizing.run --run <id>, then "
                  "blocker.run) so every record describes the same estate.",
    }


def estate_of(assessment: dict) -> str:
    """The schema the findings are about -- read from the data, never assumed."""
    owners = Counter(f["owner"] for f in assessment["findings"] if f.get("owner"))
    if not owners:
        raise RecordError("the assessment names no owning schema; cannot name the target")
    return owners.most_common(1)[0][0]


def _dataset(run_id: str, name: str, collector_output: Path = COLLECTOR_OUTPUT) -> list[dict]:
    path = collector_output / run_id / f"{name}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("rows", [])


def source_facts(run_id: str, collector_output: Path = COLLECTOR_OUTPUT) -> dict:
    nls = {r["parameter"]: r["value"] for r in _dataset(run_id, "nls_parameters", collector_output)}
    db = (_dataset(run_id, "database", collector_output) or [{}])[0]
    return {
        "nls_characterset": nls.get("NLS_CHARACTERSET"),
        "nls_nchar_characterset": nls.get("NLS_NCHAR_CHARACTERSET"),
        "version": db.get("version"),
        "version_full": db.get("version_full"),
        "banner": db.get("banner_full"),
        "cdb": db.get("cdb"),
    }
