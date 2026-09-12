"""Phase 4b -- Convert PL/SQL. Builds the conversion plan; applies nothing.

Per object: route by rule (classify) -> source a conversion (rules, then the
static fixture or the model according to model_mode) -> five gates -> status.
Types go first so tables and later objects can reference them, then routines,
then package bodies, then triggers, so the compile transaction sees each
dependency before the thing that needs it."""

from __future__ import annotations

import json
from pathlib import Path

from . import classify, gates, inventory, model, rules, target as target_mod

OUTPUT = Path(__file__).resolve().parent / "output"

ORDER = {"TYPE": 0, "TYPE BODY": 1, "FUNCTION": 2, "PROCEDURE": 2, "PACKAGE": 3, "PACKAGE BODY": 3, "TRIGGER": 4}
STATUS_ORDER = ["APPROVED", "READY_FOR_APPROVAL", "BLOCKED", "REJECTED", "MODEL_REQUIRED",
                "MANUAL", "EXCLUDED_BROKEN_ON_SOURCE", "ABSORBED_INTO_BODY"]


def _key(obj: dict) -> str:
    return f"{obj['owner']}.{obj['object_type']}.{obj['object_name']}"


def build(inv: dict, *, model_mode: str = "static", pg_target: target_mod.PgTarget | None = None,
          approved_by: str | None = None, compile: bool = True, client=None,
          output_dir: Path = OUTPUT, on_event=None) -> dict:
    if model_mode not in ("off", "static", "live"):
        raise ValueError(f"model_mode must be off, static or live, not {model_mode!r}")
    emit = on_event or (lambda evt: None)

    objects = sorted(inv["objects"], key=lambda o: (ORDER.get(o["object_type"], 9), o["object_name"]))
    total = len(objects)
    specs = {(o["owner"], o["object_name"]): o["source_text"] for o in objects if o["object_type"] == "PACKAGE"}
    bodies = {(o["owner"], o["object_name"]) for o in objects if o["object_type"] == "PACKAGE BODY"}

    entries: list[dict] = []
    queued: list[dict] = []
    for index, obj in enumerate(objects, 1):
        owner = obj["owner"]
        known_types = {n for (o, n) in inv["types"] if o == owner}
        emit({"event": "converting", "index": index, "total": total, "object_type": obj["object_type"],
              "object_name": obj["object_name"]})
        r = classify.route(obj, inv)
        entry = {
            "owner": owner, "object_type": obj["object_type"], "object_name": obj["object_name"],
            "source_sha256": obj["source_sha256"], "line_count": obj.get("line_count"),
            "route": r["route"], "route_reason": r["reason"], "constructs_found": r["constructs"],
            "status": None, "source": None, "model_id": None, "conversion": None, "gates": [], "reason": None,
        }

        if r["route"] in (classify.EXCLUDED, classify.MANUAL):
            entry["status"] = r["route"]
            entry["reason"] = r["reason"]
        elif r["route"] == classify.ABSORBED:
            if (owner, obj["object_name"]) not in bodies:
                entry["status"] = "MANUAL"
                entry["reason"] = "package specification without a body: nothing to flatten"
            else:
                try:
                    rules.check_package_spec(obj["source_text"])
                    entry["status"] = "ABSORBED_INTO_BODY"
                    entry["reason"] = ("members " + ", ".join(n for _, n in rules.package_members(obj["source_text"]))
                                       + f" are produced by the body as {obj['object_name'].lower()}$<member>")
                except rules.Declined as exc:
                    entry["status"] = "MODEL_REQUIRED"
                    entry["reason"] = f"rules_declined: {exc}"
        else:
            conv, source, reason = None, None, None
            if r["route"] == classify.RULE:
                try:
                    conv = rules.convert(
                        obj, owner=owner, known_types=known_types,
                        spec_text=specs.get((owner, obj["object_name"])) if obj["object_type"] == "PACKAGE BODY" else None,
                        trigger_meta=inv["triggers"].get((owner, obj["object_name"])),
                    )
                    source = "rule"
                except rules.Declined as exc:
                    reason = f"rules_declined: {exc}"
            else:
                reason = r["reason"]

            if conv is None:
                if model_mode == "off":
                    entry["status"], entry["reason"] = "MODEL_REQUIRED", f"{reason}; model disabled"
                elif model_mode == "static":
                    conv, source = model.static_convert(obj)
                    if conv is None:
                        entry["status"], entry["reason"] = "MODEL_REQUIRED", f"{reason}; {source}"
                else:
                    try:
                        col_types = _column_types(inv, owner, obj)
                        conv, source = model.live_convert(obj, r["constructs"], col_types, client=client)
                    except Exception as exc:  # noqa: BLE001 -- the reason belongs in the plan
                        entry["status"], entry["reason"] = "MODEL_REQUIRED", f"{reason}; model: {exc}"

            if conv is not None:
                entry["conversion"] = conv
                entry["source"] = conv.get("source", source)
                entry["model_id"] = conv.get("model_id")
                expected = rules.expected_names(obj)
                results = [gates.static_check(conv, obj, expected), gates.policy_check(conv, obj)]
                if all(g["status"] == gates.PASS for g in results):
                    results.append(gates.parity_check(conv, obj, r["constructs"]))
                entry["gates"] = results
                if any(g["status"] == gates.FAIL for g in results):
                    entry["status"] = "REJECTED"
                    entry["reason"] = next(g["detail"] for g in results if g["status"] == gates.FAIL)
                else:
                    queued.append(entry)
        entries.append(entry)
        emit({"event": "converted", "index": index, "status": entry["status"] or "queued_for_compile",
              "route": r["route"], "source": entry["source"]})

    if queued:
        emit({"event": "compiling", "count": len(queued),
              "target": pg_target.describe() if (compile and pg_target) else None})

    # ---- compile everything that survived the offline gates, in one rolled-back transaction
    pg_info = None
    compile_results: dict[str, dict] = {}
    shadow_notes: list[str] = []
    shadow_failed: dict[str, str] = {}
    if queued and compile and pg_target is not None:
        pg_info = target_mod.check_target(pg_target)
        if pg_info["ok"]:
            by_owner: dict[str, list[dict]] = {}
            for e in queued:
                by_owner.setdefault(e["owner"], []).append(e)
            for owner, group in by_owner.items():
                tables = inventory.table_names(inv, owner)
                referenced: set[str] = set()
                for e in group:
                    obj = next(o for o in objects if _key(o) == _key(e))
                    referenced |= rules.referenced_tables(obj["source_text"], tables)
                    trig = inv["triggers"].get((owner, e["object_name"]))
                    if trig and trig.get("table_name"):
                        referenced.add(trig["table_name"].upper())
                type_convs = [(_key(e), e["conversion"]["statements"], e["conversion"]["creates"])
                              for e in group if e["object_type"] == "TYPE"]
                other = [(_key(e), e["conversion"]["statements"], e["conversion"]["creates"])
                         for e in group if e["object_type"] != "TYPE"]
                shadow, notes = target_mod.shadow_statements(
                    inv, owner, referenced, [s for _, stmts, _ in type_convs for s in stmts])
                shadow_notes += notes
                try:
                    compile_results.update(target_mod.compile_all(pg_target, owner, shadow, other, type_convs))
                except Exception as exc:  # noqa: BLE001
                    # One owner's scaffold failing must not silence every other
                    # owner's compile, nor read as "no target configured". Its
                    # own objects block on the reason; the rest are judged.
                    shadow_failed[owner] = str(exc)

    for e in queued:
        result = compile_results.get(_key(e)) if (pg_info and pg_info.get("ok")) else None
        if pg_info and pg_info.get("ok") and e["owner"] in shadow_failed:
            result = {"ok": False, "shadow_failed": True, "message": shadow_failed[e["owner"]]}
        e["gates"].append(gates.compile_check(result, pg_info.get("detail") if pg_info else None))
        e["gates"].append(gates.approval(approved_by))
        e["status"] = gates.verdict(e["gates"])
        if e["status"] == "REJECTED":
            e["reason"] = next(g["detail"] for g in e["gates"] if g["status"] == gates.FAIL)
        elif e["status"] == "BLOCKED":
            e["reason"] = next(g["detail"] for g in e["gates"] if g["status"] == gates.BLOCKED)

    # ---- write one DDL file per converted object
    ddl_dir = output_dir / "plpgsql"
    ddl_dir.mkdir(parents=True, exist_ok=True)
    for old in ddl_dir.glob("*.sql"):
        old.unlink()
    for e in entries:
        if e["conversion"]:
            path = ddl_dir / f"{e['owner']}.{e['object_type'].replace(' ', '_')}.{e['object_name']}.sql"
            header = (f"-- {e['owner']}.{e['object_name']} ({e['object_type']}) -> PostgreSQL\n"
                      f"-- source: {e['source']}" + (f" ({e['conversion'].get('fixture_id')})" if e['source'] == 'static_fixture' else "")
                      + f"; status: {e['status']}; source sha256 {e['source_sha256'][:12]}\n"
                      f"-- NOT applied anywhere. Review, then approve.\n\n")
            path.write_text(header + e["conversion"]["ddl"] + "\n", encoding="utf-8")
            e["ddl_file"] = str(path.relative_to(output_dir.parent.parent))

    totals: dict[str, int] = {}
    for e in entries:
        totals[e["status"]] = totals.get(e["status"], 0) + 1

    in_the_way = []
    if any(g["gate"] == "compile" and g["status"] == gates.BLOCKED for e in entries for g in e["gates"]):
        in_the_way.append(next(g["remedy"] for e in entries for g in e["gates"]
                               if g["gate"] == "compile" and g["status"] == gates.BLOCKED))
    if totals.get("MODEL_REQUIRED"):
        in_the_way.append(f"{totals['MODEL_REQUIRED']} object(s) need the reasoning tier; Bedrock invoke is "
                          "blocked on this account (docs/05-aws-services.md)")

    return {
        "phase": "4b-convert",
        "collector_run_id": inv["collector_run_id"],
        "estate": inv["estate"],
        "target_engine": "postgresql",
        "model_mode": model_mode,
        "model_generation_enabled": model_mode == "live",
        "static_outputs_used": sum(1 for e in entries if e["source"] == "static_fixture"),
        "rule_version": rules.RULE_VERSION,
        "pg_target": pg_info,
        "shadow_notes": shadow_notes,
        "totals": totals,
        "entries": entries,
        "what_is_in_the_way": in_the_way,
        "nothing_applied": True,
    }


def _column_types(inv: dict, owner: str, obj: dict) -> dict[str, list[str]]:
    tables = rules.referenced_tables(obj["source_text"], inventory.table_names(inv, owner))
    known = {n for (o, n) in inv["types"] if o == owner}
    out = {}
    for tbl in sorted(tables):
        cols = []
        for c in inventory.columns_of(inv, owner, tbl):
            pg, _ = target_mod._column_type(c, owner, known)
            cols.append(f"{c['column_name'].lower()} {pg}")
        out[tbl.lower()] = cols
    return out


def write(result: dict, output_dir: Path = OUTPUT) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / "conversion_plan.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return out
