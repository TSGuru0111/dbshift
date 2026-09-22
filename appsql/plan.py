"""Phase 4d's decision record: every application SQL statement and what happened to it.

A plan is a record, not an action. Nothing here writes to a mapper file, a
database or a target -- the same position `convert/plan.py` takes, for the same
reason: the thing that changes what an application sends to its database is a
person, after reading this.

The record is shaped so a reader can answer three questions without opening the
code:

    what did the agent do with each statement, and where did the text come from
    what did the gates establish, and what could they not establish here
    what is left for a person, and why that specific thing cannot be automated

The third is the one a naive report omits. A phase that lists its successes and
files the rest under "manual" has told a client nothing actionable; each manual
statement here carries the construct that caused it and the reason no rewrite
exists.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import classify, extract, gates, shadow, transform

OUTPUT = Path(__file__).resolve().parent / "output"

# Terminal states, worst-last so a reader scanning a list meets the work first.
# The first four mirror `convert/plan.py`; the last three are this phase's own.
APPROVED = "APPROVED"
READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
CONVERTED_UNPROVEN = "CONVERTED_UNPROVEN"
REJECTED = "REJECTED"
BLOCKED = "BLOCKED"
NEEDS_MODEL = "NEEDS_MODEL_TIER"
MANUAL = "MANUAL"

ORDER = (APPROVED, READY_FOR_APPROVAL, CONVERTED_UNPROVEN, BLOCKED,
         REJECTED, NEEDS_MODEL, MANUAL)


def _fix_id(stmt: dict) -> str:
    key = f"appsql|{stmt['namespace']}|{stmt['statement_id']}|{stmt['sql_sha256'][:16]}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry(stmt: dict, conv: dict, found: list[dict], gate_results: list[dict],
           status: str) -> dict:
    """One statement's record: what it was, what it became, what was proven."""
    return {
        "fix_id": _fix_id(stmt),
        "file": stmt["file"],
        "namespace": stmt["namespace"],
        "statement_id": stmt["statement_id"],
        "kind": stmt["kind"],
        "status": status,
        # The source text, so a reviewer never has to open the mapper to see
        # what was changed.
        "oracle_sql": stmt["sql"],
        "oracle_sql_sha256": stmt["sql_sha256"],
        "postgresql_sql": conv.get("sql"),
        # Where the text came from. `source: null` means nothing was drafted,
        # and `reason` says why -- never an empty conversion presented as one.
        "source": conv.get("source"),
        "model_id": conv.get("model_id"),
        "tokens": conv.get("tokens"),
        "reason": conv.get("reason"),
        "caveat": conv.get("caveat"),
        "manual": conv.get("manual"),
        # What the classifier found, with each construct's tier and note, so the
        # record explains itself without the catalogue alongside.
        "constructs": [
            {"id": x["id"], "name": x["name"], "tier": x["tier"],
             "postgres": x["postgres"], "note": x["note"], "count": x["count"]}
            for x in found
        ],
        "construct_accounting": conv.get("constructs") or [],
        # MyBatis facts a reviewer needs: a dynamic statement has a family of
        # texts, and an interpolation site is an injection surface.
        "dynamic": stmt["dynamic"],
        "dynamic_tags": stmt["dynamic_tags"],
        "bind_params": stmt["bind_params"],
        "interpolations": stmt["interpolations"],
        "probe_renderable": stmt["probe_renderable"],
        "probe_unrenderable_because": stmt["probe_unrenderable_because"],
        "gates": gate_results,
    }


def _status(conv: dict, found: list[dict], gate_results: list[dict]) -> str:
    """The terminal state, which never claims more than the gates established."""
    if conv.get("manual"):
        return MANUAL
    if not conv.get("source"):
        # The rules declined and no model answered. Distinguished from MANUAL
        # because a model tier being off is a configuration state, and a
        # construct having no correct rewrite is a fact about the engines.
        return NEEDS_MODEL
    return gates.outcome(gate_results)


def build(root: Path, model_mode: str = "off", target=None,
          comparisons: dict | None = None, approved_by: str | None = None,
          client=None, ddl_plan: dict | None = None) -> dict:
    """Extract, convert and gate every statement under `root`.

    `comparisons` maps a statement id to a result comparison, when one is
    available. Phase 8 produces those; absent, the result gate reports blocked
    rather than passing.

    `ddl_plan` is Phase 4c's output. Given it and a target, the parse gate
    resolves against a **shadow schema** built from that DDL inside a
    transaction that is rolled back -- so a statement is parsed against the
    schema the application will actually meet, without anything persisting.
    Without it the parse gate reports blocked on an unpopulated target, because
    `relation "customer" does not exist` says nothing about a rewrite.
    """
    extracted = extract.from_dir(root)
    comparisons = comparisons or {}
    entries: list[dict] = []
    shadow_state: dict | None = None

    # One transaction for every statement, rather than one per statement: the
    # schema is built once and 18 probes run inside it.
    parse_target, ctx = target, None
    if target is not None and ddl_plan:
        ctx = shadow.Shadow(target, ddl_plan)
        parse_target = ctx.__enter__()
        shadow_state = parse_target.describe()

    try:
        for stmt in extracted["statements"]:
            found = classify.scan(stmt["sql"])
            conv = transform.transform(stmt, model_mode=model_mode, client=client)
            gate_results: list[dict] = []
            if conv.get("source"):
                gate_results = gates.run(
                    conv, stmt, target=parse_target,
                    comparison=comparisons.get(stmt["statement_id"]),
                    approved_by=approved_by)
            entries.append(_entry(stmt, conv, found, gate_results,
                                  _status(conv, found, gate_results)))
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)

    entries.sort(key=lambda e: (ORDER.index(e["status"]) if e["status"] in ORDER else 99,
                                e["file"], e["statement_id"]))
    totals = Counter(e["status"] for e in entries)
    by_source = Counter(e["source"] or "none" for e in entries)

    # Tier counts over *statements*, not constructs. A statement carrying one
    # unhandleable construct is declined whole, so the construct figure
    # overstates what converts -- 8 of 26 constructs are deterministic on the
    # demo app where only 2 of 16 statements are.
    tiers = Counter()
    for e in entries:
        worst = "rule"
        for con in e["constructs"]:
            if con["tier"] == "manual":
                worst = "manual"
                break
            if con["tier"] == "model":
                worst = "model"
        tiers[worst] += 1

    return {
        "phase": "4d",
        "name": "Application SQL",
        "generated_at": _now(),
        "root": str(root),
        "model_mode": model_mode,
        "target_configured": target is not None,
        # What the parse gate resolved against, or None when it could not
        # build one. A reader must be able to tell "parsed against 16 tables
        # from 4c's DDL" from "parsed against whatever happened to be there".
        "shadow": shadow_state,
        "results_compared": bool(comparisons),
        "approved_by": approved_by,
        "file_count": extracted["file_count"],
        "files": extracted["files"],
        "statement_count": extracted["statement_count"],
        "dynamic_count": extracted["dynamic_count"],
        "unrenderable_count": extracted["unrenderable_count"],
        "totals": dict(totals),
        "by_source": dict(by_source),
        "statement_tiers": dict(tiers),
        # Said plainly, because it is the number a client should quote and the
        # construct-level figure flatters the tooling.
        "pct_statements_deterministic": (
            round(100 * by_source.get("rule", 0) / extracted["statement_count"])
            if extracted["statement_count"] else None),
        "entries": entries,
        "what_this_phase_did_not_do": [
            "No mapper file was modified. Every rewrite is a proposal in this record.",
            "Nothing was applied to a database. The parse gate creates nothing.",
            ("No result comparison ran, so no rewrite is proven to return the same rows."
             if not comparisons else
             "Result comparisons ran for the statements listed; the rest are unproven."),
        ],
    }


def write(plan: dict, output_dir: Path = OUTPUT) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "appsql_plan.json"
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

    # One .sql per converted statement, so a reviewer can read a diff rather
    # than a JSON field.
    sql_dir = output_dir / "postgresql"
    sql_dir.mkdir(exist_ok=True)
    for e in plan["entries"]:
        if not e.get("postgresql_sql"):
            continue
        header = (f"-- {e['namespace']}.{e['statement_id']}  ({e['kind']})\n"
                  f"-- status: {e['status']}   source: {e['source']}"
                  + (f"   model: {e['model_id']}" if e.get("model_id") else "") + "\n"
                  "-- constructs: "
                  + ", ".join(f"{x['id']}({x['tier']})" for x in e["constructs"]) + "\n"
                  + (f"-- caveat: {e['caveat']}\n" if e.get("caveat") else "")
                  + "-- NOT APPLIED. Phase 4d proposes; a person approves.\n\n")
        (sql_dir / f"{e['statement_id']}.sql").write_text(
            header + e["postgresql_sql"] + "\n", encoding="utf-8")
    return path


def load(output_dir: Path = OUTPUT) -> dict | None:
    path = output_dir / "appsql_plan.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
