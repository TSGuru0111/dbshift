"""Phase 5 over AWS SCT's action items.

    python -m blocker.sct_run                       # the verdict, split by where
    python -m blocker.sct_run --json                # the record
    python -m blocker.sct_run --compare             # ...next to the rules-engine gate

Reads the newest SCT assessment, the Phase 4 SCT remediation plan if one exists,
and the discovery manifest's facts for CDC readiness. Decides nothing that a
model touched.

`--compare` exists because this gate replaces a working one. A verdict that
disagrees with `blocker.run` is worth seeing side by side rather than discovering
at a client.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "blocker"

from collector import mode as migration_mode
from sct import parse as sct_parse
from sct import route as sct_route
from sct import runner as sct_runner

from . import sct_gate

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = Path(__file__).resolve().parent / "output"


def _newest_sct(target: str = "") -> tuple[dict | None, Path | None]:
    if not sct_runner.OUTPUT.is_dir():
        return None, None
    found = [d / "sct_assessment.json" for d in sct_runner.OUTPUT.iterdir()
             if (d / "sct_assessment.json").exists()
             and (not target or d.name.endswith(target))]
    if not found:
        return None, None
    newest = max(found, key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(newest.read_text(encoding="utf-8")), newest
    except (OSError, json.JSONDecodeError):
        return None, None


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _facts_and_mode(estate_schemas: list[str] | None = None
                    ) -> tuple[dict, dict | None, str | None]:
    """CDC evidence and the declared mode, from the collector run for this estate.

    **The readiness is already computed and stored.** `collector/mode.py` writes
    it into the manifest as `migration_mode.cdc_readiness` when discovery runs,
    from the Connect preflight's reading of `v$database`. The first version of
    this function looked for `manifest["facts"]`, which does not exist, so the
    gate reported "log mode is unknown" about a source it already knew to be
    NOARCHIVELOG -- a gate inventing an absence of evidence.

    **The run is matched to the estate SCT assessed.** Taking the newest run
    regardless compared a DBMIG_TELCO SCT report against a DBMIG_APP discovery,
    which is exactly the class of bug `collector.verify` was fixed for on
    2026-09-14.
    """
    out = ROOT / "collector" / "output"
    if not out.is_dir():
        return {}, None, None
    runs = sorted((d for d in out.iterdir() if d.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)

    wanted = {s.upper() for s in (estate_schemas or [])}
    fallback = None
    for run in runs:
        manifest = _load(run / "manifest.json") or {}
        if not manifest:
            continue
        mode = manifest.get("migration_mode")
        # The stored readiness IS the evidence. Re-deriving it here would be a
        # second copy free to drift from the one the mode picker uses.
        readiness = (mode or {}).get("cdc_readiness") or {}
        facts = {
            "log_mode": readiness.get("log_mode"),
            "supplemental_logging": readiness.get("supplemental_log_data_min"),
        } if readiness else {}

        # `manifest["schemas"]` is a dict -- {configured, present, missing,
        # discovered_not_configured} -- not a list. Iterating it directly
        # yielded its key names, so no estate ever matched and every run
        # reported "no discovery run found". Match on what was actually
        # collected (`present`), falling back to what was asked for.
        schema_record = manifest.get("schemas")
        if isinstance(schema_record, dict):
            collected = schema_record.get("present") or schema_record.get("configured") or []
        else:
            collected = schema_record or []
        schemas = {str(s).upper() for s in collected}
        if wanted and schemas and (wanted & schemas):
            return facts, mode, run.name
        if fallback is None:
            fallback = (facts, mode, run.name)

    if wanted and fallback:
        # No discovery run for this estate. Say so rather than quietly using
        # another estate's evidence, which would be a confident wrong answer.
        return {}, fallback[1], None
    return fallback if fallback else ({}, None, None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", default="", help="SCT target id")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--compare", action="store_true",
                    help="show the rules-engine gate's verdict alongside")
    args = ap.parse_args(argv)

    record, path = _newest_sct(args.target)
    if record is None:
        print("No AWS SCT assessment on disk. Run `python -m sct.run` first.",
              file=sys.stderr)
        return 2

    csvs = sct_runner.artefact_paths(record).get("csv") or []
    if not csvs:
        print(f"{path} carries no SCT CSV, so there is nothing to gate.", file=sys.stderr)
        return 2

    parsed = sct_parse.parse_csv_file(csvs[0])
    assessment = {
        "issues": sct_route.annotate(parsed["issues"]),
        "target": record.get("target") or {},
        "collector_run_id": record.get("collector_run_id"),
    }

    estate_schemas = (record.get("source") or {}).get("schemas") or []
    facts, mode_record, discovery_run = _facts_and_mode(estate_schemas)
    if estate_schemas and discovery_run is None:
        print(f"NOTE: no discovery run found for {', '.join(estate_schemas)}, so CDC "
              f"readiness has no evidence and is reported as unknown, not as clear.\n")
    remediation = _load(ROOT / "remediate" / "output" / "sct_remediation_plan.json")

    decision = sct_gate.evaluate(
        assessment, facts=facts, migration_mode_record=mode_record,
        remediation=remediation,
    )

    OUTPUT.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT / "sct_gate_decision.json"
    out_path.write_text(json.dumps(decision, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")

    if args.json:
        print(json.dumps(decision, indent=2, default=str))
        return 0

    print(f"AWS SCT blocker gate -- {decision['target'] or 'unknown target'}")
    print(f"from {path}\n")
    print(f"VERDICT: {decision['verdict']}")
    print(f"  {decision['summary']}\n")

    for group in decision["groups"]:
        if not group["item_count"]:
            continue
        flag = f"  {group['blocking_count']} BLOCKING" if group["blocking_count"] else ""
        print(f"== {group['label']}  ({group['item_count']} items, "
              f"{group['occurrence_count']} occurrences){flag}")
        print(f"   {group['meaning']}")
        for item in group["items"]:
            halting = item in decision["blockers"]
            mark = "HALT" if halting else ("waived" if item in decision["waived"]
                                           else "work")
            print(f"   [{mark:6}] {item['issue_code']:5} {item['who_label']:18} "
                  f"x{item['occurrences']}  {item['title'][:56]}")
            if halting:
                print(f"            blocks     : {', '.join(item['blocks_in_scope'])}")
                print(f"            clears when: {item['clears_when'][:88]}")
                if item["fix_drafted"]:
                    print("            a fix is drafted in the Phase 4 SCT plan")
        print()

    cdc = decision["cdc_readiness"]
    print(f"CDC READINESS  [{cdc['status']}]")
    print(f"  {cdc['detail']}")
    if cdc.get("why_not_from_sct"):
        print(f"  {cdc['why_not_from_sct']}")
    print()

    print("DOWNSTREAM PHASES")
    for phase, state in decision["by_phase"].items():
        blocked_by = ", ".join(state["blocked_by"]) or state.get("why", "clear")
        print(f"  {phase:20} {state['status']:12} {blocked_by}")

    if decision["unrouted"]:
        print(f"\n{len(decision['unrouted'])} action item(s) are not in the routing "
              f"table and were treated as work, not as blockers. Add a row to "
              f"sct/route.py: " + ", ".join(i["issue_code"] for i in decision["unrouted"]))

    if args.compare:
        print("\n" + "=" * 68)
        print("THE RULES-ENGINE GATE, for comparison")
        rules = _load(ROOT / "assess" / "output" / "assessment.json")
        if rules is None:
            print("  no assessment.json on disk")
        else:
            from . import gate as rules_gate
            other = rules_gate.evaluate(rules, None, [])
            print(f"  VERDICT: {other['verdict']}"
                  f"   ({other['critical_findings']} critical finding(s))")
            for b in other["blockers"]:
                print(f"    {b['rule_id']:9} blocks {', '.join(b['blocks_in_scope'])}"
                      f" -- {b['title'][:52]}")
            # **Check they read the same estate before comparing verdicts.** An
            # SCT report on DBMIG_TELCO beside a rules assessment on DBMIG_APP
            # is not a comparison, and printing "AGREE" for it would repeat the
            # mistake collector.verify was fixed for on 2026-09-14.
            rules_owners = sorted({f.get("owner") for f in rules.get("findings", [])
                                   if f.get("owner")})
            sct_owners = sorted({s.upper() for s in estate_schemas})
            print(f"  rules engine read : {', '.join(rules_owners) or 'unknown'}")
            print(f"  AWS SCT read      : {', '.join(sct_owners) or 'unknown'}")
            if not (set(rules_owners) & set(sct_owners)):
                print("\n  DIFFERENT ESTATES -- these verdicts are not comparable. Run "
                      "SCT against the\n  same schema the assessment covers before "
                      "reading anything into this.")
            else:
                agree = other["verdict"] == decision["verdict"]
                print(f"\n  the two gates {'AGREE' if agree else 'DISAGREE'} "
                      f"on the verdict.")
                if not agree:
                    print("  A disagreement is not automatically a bug -- they judge "
                          "different\n  evidence -- but it must be understood before "
                          "this gate is trusted.")

    print(f"\nwritten: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
