"""Phase 10 -- the migration assessment report, as data.

Two documents a client expects from an AWS migration are the SCT assessment
report (what share of the schema converts automatically, and the action items
by complexity) and the DMS pre-migration assessment (whether the tables can
actually replicate). DBShift produces both shapes here from its own records:
Phase 2's findings, Phase 4b's conversion plan, Phase 3's decision and Phase
5's gate. Nothing is recomputed and nothing is judged -- every number is
traced to a record, and the report says which.

It mirrors the *structure* of the AWS documents so a reader who knows them
finds their way. It is not produced by those tools and never claims to be."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# SCT vocabulary: complexity of the action a person must take. Ours is the
# remediation level the assessment rule assigned -- never re-judged here.
COMPLEXITY = {"L1": "simple", "L2": "medium", "L3": "complex", "L4": "decision"}
COMPLEXITY_ORDER = ["simple", "medium", "complex", "decision"]

# Object types Oracle manages on the user's behalf. Never counted as estate.
INTERNAL_PREFIXES = ("DR$", "AQ$", "MLOG$", "RUPD$", "SYS_")
STORAGE_TYPES = ["TABLE", "INDEX", "VIEW", "MATERIALIZED VIEW", "SEQUENCE", "SYNONYM",
                 "DATABASE LINK", "JOB", "QUEUE"]
CODE_TYPES = ["TYPE", "TYPE BODY", "FUNCTION", "PROCEDURE", "PACKAGE", "PACKAGE BODY", "TRIGGER"]

# The DMS pre-migration assessment, in DMS's own terms, each backed by the
# assessment rules that decide it. A check with no matching rule is "pass";
# a CRITICAL rule firing is "fail"; anything else firing is "warning".
DMS_CHECKS = [
    {"id": "no_primary_key", "name": "Tables without a primary key or unique index",
     "rules": ["DQ-001"], "why": "Change data capture identifies rows by key; without one, updates and "
     "deletes cannot be applied reliably and no error is raised."},
    {"id": "archivelog", "name": "Source database in ARCHIVELOG mode",
     "rules": ["OPS-001"], "why": "CDC reads the redo log; NOARCHIVELOG leaves nothing to read."},
    {"id": "supplemental_logging", "name": "Supplemental logging enabled",
     "rules": ["OPS-002"], "why": "Without it the redo carries too little to reconstruct a changed row."},
    {"id": "external_tables", "name": "External tables",
     "rules": ["RDS-004"], "why": "DMS reads rows, and an external table's rows live in a file on the source host."},
    {"id": "lob_settings", "name": "LOB columns and LOB mode",
     "rules": ["RDS-012"], "why": "Limited LOB mode truncates; full LOB mode is slow. Each LOB table needs a decision."},
    {"id": "unsupported_types", "name": "Data types DMS does not support",
     "rules": ["RDS-010"], "why": "User-defined and object types are skipped or mapped to BLOB."},
    {"id": "not_migrated", "name": "Objects DMS does not migrate",
     "rules": ["RDS-005", "RDS-006", "RDS-007", "RDS-008", "RDS-009", "RDS-011", "RDS-014"],
     "why": "Links, jobs, queues, text indexes, XML schemas, materialized views and synonyms are "
     "recreated by other means or not at all."},
    {"id": "sequences", "name": "Sequences", "rules": [], "special": "sequences",
     "why": "DMS does not migrate sequences. Every one must be resynchronised above the source's last value after the load."},
    {"id": "reserved_words", "name": "Reserved-word identifiers",
     "rules": ["RDS-001", "RDS-002"], "why": "Quoted on the target; every application query that names them must quote too."},
    {"id": "novalidate", "name": "Constraints enabled but not validated",
     "rules": ["DQ-004"], "why": "Recreating the constraint after the load validates every row and fails on the ones the source never checked."},
    {"id": "charset", "name": "Character set alignment",
     "rules": ["RDS-015"], "why": "A target created with a different character set cannot be changed afterwards."},
    {"id": "window", "name": "Estate size against the migration window",
     "rules": ["OPS-008"], "why": "Full-load time is a function of bytes, not rows."},
]


def _load(rel: str) -> dict | None:
    p = ROOT / rel
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _dataset(run_dir: Path | None, name: str) -> list[dict]:
    if not run_dir:
        return []
    p = run_dir / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8"))["rows"] if p.exists() else []


def build(*, assessment: dict, conversion: dict | None = None, sizing: dict | None = None,
          gate: dict | None = None, objects: list[dict] | None = None, provision: dict | None = None,
          validation: dict | None = None, certificate: dict | None = None,
          migration: dict | None = None) -> dict:
    run_id = assessment["collector_run_id"]
    findings = assessment.get("findings") or []
    issues = assessment.get("issues") or []
    owners = Counter(f.get("owner") for f in findings if f.get("owner"))
    estate = (conversion or {}).get("estate") or (owners.most_common(1)[0][0] if owners else None)
    objects = [o for o in (objects or []) if o.get("owner") == estate
               and not o["object_name"].upper().startswith(INTERNAL_PREFIXES)]

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "collector_run_id": run_id,
        "estate": estate,
        "provenance": _provenance(assessment, conversion, sizing, gate),
        "target_decision": _target(sizing),
        "schema_conversion": _schema_conversion(objects, findings, issues, conversion),
        "dms_assessment": _dms(issues, findings, gate, objects, migration),
        "status": _status(gate, provision, validation, certificate, run_id),
        "scores": assessment.get("scores"),
        "recall": assessment.get("answer_key"),
    }


def _record(root: Path, phase: str, name: str) -> dict | None:
    """A phase record under either layout: <root>/<phase>/output/<name> (the
    repo's default) or <root>/<phase>/<name> (the second-estate layout that
    scripts/telco-source/run_phases.ps1 writes)."""
    for p in (root / phase / "output" / name, root / phase / name):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def build_from_disk(run_dir: Path | None = None, root: Path | None = None) -> dict:
    root = root or ROOT
    assessment = _record(root, "assess", "assessment.json")
    if not assessment:
        raise SystemExit(f"no assessment under {root} -- run 'python -m assess.run' first")
    if run_dir is None:
        candidates = [root / "collector" / "output" / assessment["collector_run_id"],
                      root / "collector" / assessment["collector_run_id"]]
        run_dir = next((c for c in candidates if c.exists()), None)
    return build(
        assessment=assessment,
        conversion=_record(root, "convert", "conversion_plan.json"),
        sizing=_record(root, "sizing", "sizing.json"),
        gate=_record(root, "blocker", "gate_decision.json"),
        objects=_dataset(run_dir, "objects"),
        provision=_record(root, "provision", "provision_plan.json"),
        validation=_record(root, "validate", "validation_report.json"),
        certificate=_record(root, "cutover", "certificate.json"),
        migration=_record(root, "migrate", "migration_run.json"),
    )


# ---------------------------------------------------------------- sections

def _provenance(assessment, conversion, sizing, gate) -> dict:
    ids = {"assessment": assessment.get("collector_run_id")}
    for name, rec in (("conversion", conversion), ("sizing", sizing), ("gate", gate)):
        if rec:
            ids[name] = rec.get("collector_run_id")
    present = {v for v in ids.values() if v}
    return {
        "records": ids,
        "consistent": len(present) == 1,
        "assessed_at_utc": assessment.get("assessed_at_utc"),
        "rules_evaluated": assessment.get("rules_evaluated"),
        "conversion_rule_version": (conversion or {}).get("rule_version"),
        "model_mode": (conversion or {}).get("model_mode"),
        "model_generation_enabled": (conversion or {}).get("model_generation_enabled", False),
        "static_outputs_used": (conversion or {}).get("static_outputs_used", 0),
        "compile_target": ((conversion or {}).get("pg_target") or {}).get("detail"),
    }


def _target(sizing) -> dict | None:
    if not sizing:
        return None
    d = sizing.get("decision") or {}
    return {
        "edition": d.get("edition"), "licence_model": d.get("licence_model"),
        "instance_class": d.get("instance_class"), "storage_gb": d.get("storage_gb"),
        "processor_licences": d.get("processor_licences"),
        "forced_by": [f.get("feature") or f.get("name") or str(f) for f in (d.get("forced_by") or d.get("edition_forced_by") or [])],
        "character_set": d.get("character_set"),
        "utilization": d.get("utilization_basis") or d.get("sizing_basis"),
    }


def _schema_conversion(objects, findings, issues, conversion) -> dict:
    by_type = Counter(o["object_type"] for o in objects)
    fcount = defaultdict(Counter)   # object_type -> rule_id -> n
    for f in findings:
        if f.get("object_type"):
            fcount[f["object_type"].upper()][f["rule_id"]] += 1

    storage = []
    for t in STORAGE_TYPES:
        if by_type.get(t):
            rules = fcount.get(t, Counter())
            storage.append({"type": t, "count": by_type[t], "action_items": sum(rules.values()),
                            "rules": sorted(rules)})
    partitions = by_type.get("TABLE PARTITION", 0)

    # Code objects: what Phase 4b actually did, per type.
    code = []
    entries = (conversion or {}).get("entries") or []
    summary = Counter()
    for t in CODE_TYPES:
        es = [e for e in entries if e["object_type"] == t]
        if not es and not by_type.get(t):
            continue
        row = {"type": t, "total": len(es) or by_type.get(t, 0), "automatic": 0, "model_converted": 0,
               "uncompiled": 0, "needs_model": 0, "manual": 0, "excluded": 0, "rejected": 0, "absorbed": 0}
        for e in es:
            s, src = e["status"], e.get("source")
            if s in ("READY_FOR_APPROVAL", "APPROVED"):
                row["automatic" if src == "rule" else "model_converted"] += 1
            elif s == "BLOCKED":
                row["uncompiled"] += 1
            elif s == "REJECTED":
                row["rejected"] += 1
            elif s == "MODEL_REQUIRED":
                row["needs_model"] += 1
            elif s == "MANUAL":
                row["manual"] += 1
            elif s == "EXCLUDED_BROKEN_ON_SOURCE":
                row["excluded"] += 1
            elif s == "ABSORBED_INTO_BODY":
                row["absorbed"] += 1
        code.append(row)
        for k in ("total", "automatic", "model_converted", "uncompiled", "needs_model", "manual",
                  "excluded", "rejected", "absorbed"):
            summary[k] += row[k]
    convertible = summary["total"] - summary["absorbed"]
    summary = dict(summary)
    summary["convertible"] = convertible
    summary["pct_automatic"] = round(100 * summary["automatic"] / convertible) if convertible else None
    summary["ran"] = bool(entries)

    # Action items, SCT-style: one per assessment issue, plus the conversion's own.
    items = []
    for i in issues:
        items.append({
            "complexity": COMPLEXITY.get(i["remediation_level"], "complex"),
            "level": i["remediation_level"], "severity": i["severity"], "rule_id": i["rule_id"],
            "title": i["title"], "occurrences": i["occurrences"],
            "objects": (i.get("objects") or [])[:6], "more": max(0, i["occurrences"] - 6),
            "recommendation": i.get("rationale"), "source": "assessment",
        })
    definer = [e for e in entries if e.get("conversion") and any(
        c.get("oracle") == "AUTHID_DEFINER" and c.get("handling") == "not_translated"
        for c in e["conversion"].get("constructs") or [])]
    if definer:
        items.append({
            "complexity": "medium", "level": "L2", "severity": "MEDIUM", "rule_id": "CONVERT",
            "title": "Routines run with definer's rights on Oracle; decide SECURITY DEFINER per routine",
            "occurrences": len(definer), "objects": [e["object_name"] for e in definer][:6],
            "more": max(0, len(definer) - 6), "source": "conversion",
            "recommendation": "PostgreSQL defaults to the caller's rights. The converter never adds SECURITY "
                              "DEFINER; a person grants it where the application relied on definer's rights.",
        })
    for e in entries:
        if e["status"] in ("MODEL_REQUIRED", "MANUAL", "REJECTED"):
            items.append({
                "complexity": {"MODEL_REQUIRED": "medium", "MANUAL": "decision", "REJECTED": "complex"}[e["status"]],
                "level": None, "severity": None, "rule_id": "CONVERT",
                "title": f"{e['object_type'].title()} {e['object_name']}: {e.get('reason') or e.get('route_reason')}",
                "occurrences": 1, "objects": [e["object_name"]], "more": 0, "source": "conversion",
                "recommendation": None,
            })
    items.sort(key=lambda x: (COMPLEXITY_ORDER.index(x["complexity"]), -(x["occurrences"])))
    by_complexity = Counter(x["complexity"] for x in items)
    return {
        "storage_objects": storage, "partitions": partitions,
        "code_objects": code, "code_summary": summary,
        "action_items": items, "by_complexity": {k: by_complexity.get(k, 0) for k in COMPLEXITY_ORDER},
        "storage_note": ("Table, index and constraint DDL is not converted by DBShift: on a heterogeneous "
                         "path DMS Schema Conversion produces it. The action items against these objects "
                         "are what a person must handle either way."),
    }


def _dms(issues, findings, gate, objects, migration) -> dict:
    by_rule = {i["rule_id"]: i for i in issues}
    blocks_by_rule = defaultdict(list)
    for name, ph in ((gate or {}).get("by_phase") or {}).items():
        for r in ph.get("blocked_by") or []:
            blocks_by_rule[r].append(name)
    checks = []
    for c in DMS_CHECKS:
        hit = [by_rule[r] for r in c["rules"] if r in by_rule]
        objs, blocks, count = [], set(), 0
        for i in hit:
            count += i["occurrences"]
            objs += (i.get("objects") or [])[:4]
            blocks |= set(blocks_by_rule.get(i["rule_id"], []))
        if c.get("special") == "sequences":
            seqs = [o["object_name"] for o in objects if o["object_type"] == "SEQUENCE"]
            count, objs = len(seqs), seqs[:6]
            result = "warning" if seqs else "pass"
        elif not hit:
            result = "pass"
        elif any(i["severity"] == "CRITICAL" for i in hit):
            result = "fail"
        elif all(i["severity"] == "INFO" for i in hit):
            result = "info"
        else:
            result = "warning"
        if c.get("special") == "sequences":
            detail = f"{count} sequence(s) in discovery; none migrate with DMS" if count else "no sequences"
        else:
            detail = "; ".join(f"{i['rule_id']}: {i['title']} ({i['occurrences']})" for i in hit) or None
        checks.append({
            "id": c["id"], "name": c["name"], "result": result, "count": count,
            "objects": objs[:6], "rules": [i["rule_id"] for i in hit] or c["rules"],
            "blocks": sorted(blocks), "why": c["why"], "detail": detail,
        })
    by_phase = (gate or {}).get("by_phase") or {}
    cdc_blocked = (by_phase.get("migrate_cdc") or {}).get("status") == "blocked"
    full_blocked = (by_phase.get("migrate_full_load") or {}).get("status") == "blocked"
    path = {
        "full_load": "blocked" if full_blocked else "possible",
        "cdc": "blocked" if cdc_blocked else "possible",
        "full_load_blocked_by": (by_phase.get("migrate_full_load") or {}).get("blocked_by") or [],
        "cdc_blocked_by": (by_phase.get("migrate_cdc") or {}).get("blocked_by") or [],
        "consequence": ("Change data capture is not possible on this source as it stands, so a low-downtime "
                        "cutover is not either: the cutover needs an outage window that covers the full load."
                        if cdc_blocked else "Change data capture is possible; a low-downtime cutover can be planned."),
        "used": None,
    }
    if migration:
        path["used"] = ("Phase 7 moved this estate with Data Pump over S3, not DMS: at this size it loads in "
                        "minutes and needs no replication instance. DMS is the path when CDC is required or "
                        "the estate is too large to load inside a window.")
    return {"checks": checks, "path": path,
            "totals": Counter(c["result"] for c in checks)}


def _status(gate, provision, validation, certificate, run_id) -> dict:
    return {
        "gate": {"verdict": (gate or {}).get("verdict"), "summary": (gate or {}).get("summary")} if gate else None,
        "provision": {"stack": provision.get("stack_name"), "ready": provision.get("ready"),
                      "engine": (provision.get("rendered") or {}).get("engine"),
                      "instance": (provision.get("rendered") or {}).get("instance_class")} if provision else None,
        "validation": {"status": validation.get("status"), "mismatches": validation.get("mismatches"),
                       "not_comparable": validation.get("not_comparable"), "run_id": validation.get("run_id")} if validation else None,
        "cutover": {"ready": certificate.get("ready"), "built_at_utc": certificate.get("built_at_utc"),
                    "run_id": certificate.get("collector_run_id"),
                    "unmet": [r["title"] for r in certificate.get("requirements") or [] if r.get("status") == "unmet"]} if certificate else None,
        "same_run": all((x or {}).get("run_id") in (None, run_id) for x in (
            validation, {"run_id": (certificate or {}).get("collector_run_id")} if certificate else None)),
    }
