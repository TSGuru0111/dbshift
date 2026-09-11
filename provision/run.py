"""Phase 6 -- render the RDS target and check it can be deployed. Creates nothing.

    python -m provision.run
    python -m provision.run --price-file path/to/AmazonRDS-ap-south-1-index.json

Writes provision/output/<stack>.template.json and provision_plan.json. The
deploy is a separate step that needs an explicit, per-deploy yes; it is not in
this module.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "provision"

from . import policy, preflight, pricing, records, render

OUTPUT = Path(__file__).resolve().parent / "output"
DEFAULT_PROFILE = os.environ.get("DBSHIFT_AWS_PROFILE", "dbshift-static")


def execute(*, session=None, price_file: Path | None = None, operator_cidr: str | None = None,
            now: datetime | None = None, on_event=None) -> dict:
    """Shared by the CLI and the console."""
    def emit(stage, detail):
        if on_event:
            on_event({"event": "stage", "stage": stage, "detail": detail})

    now = now or datetime.now(timezone.utc)
    recs = records.load()
    checks = [records.consistency(recs)]
    emit("records", checks[0]["detail"])
    if checks[0]["status"] == preflight.FAIL:
        return _finish({"ready": False, "checks": checks, "rendered": None}, None)

    run_id = recs["sizing"]["collector_run_id"]
    facts = records.source_facts(run_id)
    estate = records.estate_of(recs["assessment"])
    stack = render.stack_name_for(estate)
    engine, licence = policy.ENGINE[recs["sizing"]["decision"]["edition"]]
    d = recs["sizing"]["decision"]

    checks.append(preflight.gate_allows(recs["gate"]))
    checks.append(preflight.version_direction(facts))
    emit("gate", checks[-2]["detail"])

    resolved = {}
    if session is not None:
        emit("aws", "read-only checks against the account")
        aws, resolved = preflight.aws_checks(session, engine=engine, licence=licence,
                                             instance_class=d["instance_class"],
                                             storage_type=d["storage_type"], stack_name=stack)
        checks += aws
    else:
        checks.append(preflight._c("aws_identity", preflight.BLOCKED, "no AWS session supplied"))

    ip = ({"name": "operator_ip", "status": preflight.PASS, "detail": "supplied", "cidr": operator_cidr}
          if operator_cidr else preflight.operator_cidr())
    checks.append({k: v for k, v in ip.items() if k != "cidr"})

    engine_version = resolved.get("engine_version")
    rendered = None
    if engine_version:
        emit("render", f"{stack} on {engine} {engine_version}")
        rendered = render.render(recs, facts, engine_version=engine_version, stack_name=stack,
                                 estate=estate, now=now)
        checks.append(preflight.validate_template(session, rendered["template"]))

    cost = None
    if price_file and rendered:
        prices = pricing.lookup(price_file, region=policy.REGION, engine=engine, licence=licence,
                                instance_class=d["instance_class"], storage_type=d["storage_type"],
                                multi_az=policy.MULTI_AZ)
        cost = {"prices": prices, "estimate": pricing.estimate(prices, d["storage_gb"])}

    plan = {
        "rendered_at_utc": now.isoformat(),
        "stack_name": stack,
        "estate": estate,
        "collector_run_id": run_id,
        "source": facts,
        "checks": checks,
        "ready": bool(rendered) and not any(c["status"] in (preflight.FAIL, preflight.BLOCKED)
                                            for c in checks),
        "parameters": {"VpcId": resolved.get("vpc_id"), "SubnetIds": resolved.get("subnet_ids"),
                       "OperatorCidr": ip.get("cidr")},
        "rendered": rendered,
        "cost": cost,
        "deploy": "not performed -- Phase 6 renders and checks only; a deploy needs an explicit yes",
    }
    return _finish(plan, rendered)


def _finish(plan: dict, rendered: dict | None) -> dict:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if rendered:
        (OUTPUT / f"{rendered['stack_name']}.template.json").write_text(
            json.dumps(rendered["template"], indent=2), encoding="utf-8")
    (OUTPUT / "provision_plan.json").write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DBShift Phase 6 -- render and preflight; creates nothing")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--no-aws", action="store_true", help="skip every AWS call")
    ap.add_argument("--price-file", type=Path, default=os.environ.get("DBSHIFT_PRICE_FILE"))
    ap.add_argument("--operator-cidr", default=None)
    args = ap.parse_args(argv)

    session = None
    if not args.no_aws:
        import boto3
        session = boto3.Session(profile_name=args.profile)
    plan = execute(session=session, price_file=args.price_file, operator_cidr=args.operator_cidr)
    _report(plan)
    return 0 if plan["ready"] else 1


def _report(p: dict) -> None:
    print(f"\nstack            : {p.get('stack_name', '-')}")
    print(f"estate           : {p.get('estate', '-')}   collector run {p.get('collector_run_id', '-')}")
    print("\nCHECKS (read-only)")
    for c in p["checks"]:
        print(f"  [{c['status'].upper():<7}] {c['name']:<22} {c['detail']}")
        if c.get("remedy") and c["status"] != "pass":
            print(f"  {'':<33}-> {c['remedy']}")
    r = p.get("rendered")
    if r:
        print("\nPROVENANCE -- where each value in the template came from")
        for x in r["provenance"]:
            print(f"  {x['property']:<28} {str(x['value'])[:34]:<35} {x['source']}")
            print(f"  {'':<28} {x['why'][:120]}")
        print(f"\nhanded to later phases: {len(r['handoffs'])} artefacts from Phase 4 "
              f"({', '.join(sorted({h['phase'] for h in r['handoffs']}))})")
    c = p.get("cost")
    if c:
        e = c["estimate"]
        if e:
            print(f"\nCOST ({c['prices']['source']}, {c['prices']['deployment']})")
            print(f"  instance        ${e['instance_per_hour']}/hour")
            print(f"  storage         ${e['storage_per_month']}/month")
            print(f"  one 8-hour day  ${e['per_8h_day']}")
            print(f"  left running    ${e['if_left_running_30_days']} for 30 days")
            print(f"  excludes        {e['excludes']}")
        else:
            print(f"\nCOST: price list ambiguous -- {len(c['prices']['instance_matches'])} instance "
                  f"and {len(c['prices']['storage_matches'])} storage matches; not guessing")
    print(f"\nready to offer a deploy: {p['ready']}")
    print(f"deploy: {p['deploy']}")


if __name__ == "__main__":
    raise SystemExit(main())
