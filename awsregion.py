"""The one place that says which AWS region this project acts in.

Everything that creates, reads or deletes an AWS resource -- RDS, CloudFormation,
SSM, DMS, the kill switch -- resolves its region here, through
`provision.policy.REGION` and `dms.policy.REGION` (both read `current()`), so the
region a person picks in the console is the region the resources land in and the
region prices are quoted for. There is no second copy to drift.

Resolution order: a value set in this process, else the one persisted from the
last console choice, else DBSHIFT_AWS_REGION (the kill switch's existing
override), else ap-south-1. The persisted file is what lets the CLI entry points
and a restarted console agree with what is already deployed.

`used()` is every region this project has ever created something in. The kill
switch scans those as well as the current one: a region chosen after a deploy
must not hide a billing instance in the region it was deployed to.

The list of regions is botocore's own endpoint data for the standard `aws`
partition, restricted to regions where RDS, DMS and CloudFormation all exist --
what Provision and Migrate need -- so it needs no upkeep as AWS adds regions.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

DEFAULT = "ap-south-1"
STATE_FILE = Path(__file__).resolve().parent / "web" / "target_region.json"

_set: str | None = None


@lru_cache(maxsize=1)
def regions() -> dict[str, str]:
    """{code: console name}, e.g. {"ap-south-1": "Asia Pacific (Mumbai)"}."""
    import botocore.loaders
    import botocore.session
    session = botocore.session.get_session()
    partition = next(p for p in botocore.loaders.create_loader().load_data("endpoints")["partitions"]
                     if p["partition"] == "aws")
    need = [set(session.get_available_regions(s)) for s in ("rds", "dms", "cloudformation")]
    return {code: info["description"] for code, info in sorted(partition["regions"].items())
            if all(code in have for have in need)}


def name(code: str) -> str:
    return regions().get(code, code)


def _read() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    STATE_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def current() -> str:
    return _set or _read().get("current") or os.environ.get("DBSHIFT_AWS_REGION") or DEFAULT


def set_region(code: str) -> str:
    global _set
    if code not in regions():
        raise ValueError(f"unknown region {code!r}")
    _set = code
    _write({**_read(), "current": code})
    return code


def mark_used(code: str | None = None) -> None:
    """Record that something was created in this region (default: the current one)."""
    code = code or current()
    data = _read()
    if code not in data.get("used", []):
        _write({**data, "used": sorted({*data.get("used", []), code})})


def used() -> list[str]:
    return _read().get("used", [])


def scan_regions() -> list[str]:
    """Where the kill switch must look: the current region and every region used."""
    return sorted({current(), *used()})
