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
# The SCT path writes its own files beside the 50-rule ones. Since 2026-09-17
# SCT is the assessment a client reads and the gate the console leads with, so
# these are preferred wherever they exist and the 50-rule file is the fallback.
#
# Without this, a run assessed and gated through SCT alone left these three
# records untouched -- and Phase 6 compared today's sizing against whatever the
# 50-rule path had written last, reporting `records come from different
# collector runs` for a pipeline that was in fact perfectly consistent. The
# consistency check was right; it was reading the wrong three files.
SCT_PATHS = {
    "remediation": ROOT / "remediate" / "output" / "sct_remediation_plan.json",
    "gate": ROOT / "blocker" / "output" / "sct_gate_decision.json",
}
COLLECTOR_OUTPUT = ROOT / "collector" / "output"


class RecordError(RuntimeError):
    pass


def _assessment_from_gate(gate: dict) -> dict:
    """The assessment record, derived from the SCT gate that judged it.

    There is no fixed-path SCT equivalent of `assess/output/assessment.json`:
    `sct/output/<estate>-<target>/sct_assessment.json` is per estate and per
    target, and it records the *run* -- host, exit code, artefacts -- not the
    findings. The findings are on the gate, already routed, so they are taken
    from there. One source, so the two cannot drift.

    Only what downstream actually reads is synthesised: `collector_run_id`,
    and `findings` carrying `owner` (for `estate_of`) and `rule_id` (which
    `render.py` collects into a set of rule ids). An SCT item is keyed by
    `issue_code`, so that is what `rule_id` carries -- SCT's `5984` where the
    rules engine had `RDS-004`. The Oracle-only branches that test for specific
    `RDS-*` ids therefore simply do not match, which is correct: those are
    Data Pump and option-group concerns that do not arise on the PostgreSQL
    path SCT is assessing.
    """
    items = (gate.get("work") or []) + (gate.get("blockers") or []) + (gate.get("waived") or [])
    return {
        "collector_run_id": gate.get("collector_run_id"),
        "source": "aws-sct",
        "findings": [
            {"rule_id": i.get("issue_code"), "owner": i.get("owner"),
             "object_name": (i.get("objects") or [None])[0],
             "title": i.get("title"), "severity": "CRITICAL" if i.get("blocks_in_scope") else "INFO"}
            for i in items
        ],
    }


def load(paths: dict | None = None, *, prefer_sct: bool = True) -> dict:
    """The four records, SCT's where it has them.

    `prefer_sct` exists so a caller can force the 50-rule records; it is not
    exposed on any CLI, because the choice is made by which files exist rather
    than by a flag someone has to remember.
    """
    paths = dict(paths or PATHS)
    records = {}
    sct_used = []
    if prefer_sct and paths is not None:
        for name, path in SCT_PATHS.items():
            if name in paths and Path(path).exists():
                paths[name] = path
                sct_used.append(name)
    for name, path in paths.items():
        path = Path(path)
        if name == "assessment" and "gate" in sct_used and not path.exists():
            continue        # derived below, from the SCT gate
        if not path.exists():
            raise RecordError(f"no {name} record at {path} -- run that phase first")
        records[name] = json.loads(path.read_text(encoding="utf-8"))
    # The assessment comes from the SCT gate when the gate is SCT's, so the
    # four records cannot describe two different runs: a stale 50-rule
    # assessment beside a fresh SCT gate is exactly the mismatch that made
    # Phase 6 refuse a pipeline that was actually consistent.
    if "gate" in sct_used and records.get("gate", {}).get("source_of_findings") == "aws-sct":
        records["assessment"] = _assessment_from_gate(records["gate"])
    records["_source"] = {"sct": sorted(sct_used)}
    return records


def consistency(records: dict) -> dict:
    # `_source` records which files were read, not an estate, so it is not one
    # of the records being compared. Leading-underscore keys are metadata.
    ids = {name: r.get("collector_run_id")
           for name, r in records.items() if not name.startswith("_")}
    sct = (records.get("_source") or {}).get("sct") or []
    via = f" (via AWS SCT: {', '.join(sct)})" if sct else ""
    distinct = set(ids.values())
    if len(distinct) == 1 and None not in distinct:
        return {"name": "records_consistent", "status": "pass",
                "detail": f"all four records come from collector run {ids['assessment']}{via}",
                "remedy": None}
    return {
        "name": "records_consistent", "status": "fail",
        "detail": "records come from different collector runs: "
                  + ", ".join(f"{k}={v}" for k, v in ids.items()) + via,
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
