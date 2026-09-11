"""Kill switch: find what is billing, and stop or delete what is ours.

    python -m killswitch                                   # look only, changes nothing
    python -m killswitch --stop    --confirm <account-id>  # pause (RDS restarts after 7 days)
    python -m killswitch --destroy --confirm <account-id>  # delete everything dbshift-owned

`--confirm` must equal the account the credentials resolve to. It turns "I ran
the wrong command in the wrong terminal" into a refusal instead of a deletion.

Exit code: 0 when nothing of ours is billing, 2 when something is -- so a
scheduled check can alarm on it without parsing output.
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
    __package__ = "killswitch"

from . import actions, scan

DEFAULT_PROFILE = os.environ.get("DBSHIFT_AWS_PROFILE", "dbshift-static")
DEFAULT_REGION = os.environ.get("DBSHIFT_AWS_REGION", "ap-south-1")
OUTPUT = Path(__file__).resolve().parent / "output"


def session_for(profile: str):
    import boto3
    return boto3.Session(profile_name=profile)


def regions_for(session, all_regions: bool, region: str) -> list[str]:
    if not all_regions:
        return [region]
    ec2 = session.client("ec2", region_name=region)
    return sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])


def execute(session, *, mode: str | None, confirm: str | None, regions: list[str],
            include_snapshots: bool = False, force_deletion_protection: bool = False) -> dict:
    """Shared by the CLI and the console. mode=None means inventory only."""
    account = session.client("sts").get_caller_identity()["Account"]
    if mode and confirm != account:
        raise PermissionError(
            f"refusing to {mode}: --confirm must be the account id these credentials resolve "
            f"to ({account}), and it was {confirm!r}")

    found, plan, results = [], [], []
    for region in regions:
        clients = scan.clients_for(session, region)
        here = scan.scan(clients, region)
        found += here
        if mode:
            step = actions.decide(here, mode, include_snapshots=include_snapshots,
                                  force_deletion_protection=force_deletion_protection)
            plan += step
            results += actions.execute(step, clients)

    ours_billing = [r for r in found if r["ours"] and r["billable"]]
    report = {
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "account": account,
        "regions": regions,
        "mode": mode or "inventory",
        "found": found,
        "plan": plan,
        "results": results,
        "ours_billing": len(ours_billing),
        "not_ours_billing": sum(1 for r in found if not r["ours"] and r["billable"]),
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "last_run.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DBShift kill switch")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--stop", action="store_true", help="stop dbshift RDS instances (they restart after 7 days)")
    g.add_argument("--destroy", action="store_true", help="delete everything dbshift-owned")
    ap.add_argument("--confirm", help="account id; required with --stop or --destroy")
    ap.add_argument("--include-snapshots", action="store_true", help="also delete dbshift manual snapshots")
    ap.add_argument("--force-deletion-protection", action="store_true")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--all-regions", action="store_true", help="scan every region, not just the working one")
    args = ap.parse_args(argv)

    mode = "stop" if args.stop else "destroy" if args.destroy else None
    session = session_for(args.profile)
    try:
        report = execute(session, mode=mode, confirm=args.confirm,
                         regions=regions_for(session, args.all_regions, args.region),
                         include_snapshots=args.include_snapshots,
                         force_deletion_protection=args.force_deletion_protection)
    except PermissionError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- expired keys, most often
        # The identity check is the first call, so a failure here means nothing
        # was stopped or deleted. Say that plainly; a traceback hides it.
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        print(f"NOTHING WAS CHANGED -- could not reach AWS: {first}", file=sys.stderr)
        if "ExpiredToken" in first or "expired" in first.lower():
            print("The session keys have expired. Paste fresh ones from the access portal into the "
                  f"'{args.profile}' profile and run this again.", file=sys.stderr)
        return 3

    _print(report)
    return 2 if report["ours_billing"] else 0


def _print(r: dict) -> None:
    print(f"account {r['account']}   regions {', '.join(r['regions'])}   mode {r['mode']}")
    if not r["found"]:
        print("\nNothing billable found. Nothing to stop.")
    else:
        print(f"\n{'KIND':<26}{'ID':<34}{'STATUS':<20}{'OURS':<6}DETAIL")
        for x in r["found"]:
            print(f"{x['kind']:<26}{x['id'][:33]:<34}{str(x['status'])[:19]:<20}"
                  f"{'yes' if x['ours'] else 'NO':<6}{x['detail']}")
    if r["plan"]:
        print("\nPLAN")
        for p in r["plan"]:
            print(f"  {p['action'].upper():<8}{p['kind']:<26}{p['id'][:33]:<34}{p['why']}")
    if r["results"]:
        print("\nRESULTS")
        for x in r["results"]:
            print(f"  {x['outcome']:<10}{x['action']:<8}{x['id']}")
        print("\nDeletes take minutes to finish. Re-run `python -m killswitch` to confirm "
              "nothing of ours is left billing.")
    print(f"\nours still billing: {r['ours_billing']}   "
          f"not ours, billing, left alone: {r['not_ours_billing']}")


if __name__ == "__main__":
    raise SystemExit(main())
