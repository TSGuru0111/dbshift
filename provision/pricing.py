"""Hourly and monthly cost, read from AWS's public price list -- never guessed.

The role here has no pricing:GetProducts, so this reads the regional offer file
AWS publishes at pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/
current/<region>/index.json. It is passed in as a path and kept out of the repo;
prices change, and a stale number committed to git is worse than none.

Returns every matching price rather than picking one silently. More than one
match means the filter is ambiguous and a person should look.
"""

from __future__ import annotations

import json
from pathlib import Path

LOCATION = {"ap-south-1": "Asia Pacific (Mumbai)"}
EDITION = {"oracle-ee": "Enterprise", "oracle-se2": "Standard Two"}
LICENCE = {"bring-your-own-license": "Bring your own license", "license-included": "License included"}
VOLUME = {"gp3": "General Purpose-GP3", "gp2": "General Purpose"}


def _on_demand(terms: dict, sku: str) -> list[dict]:
    out = []
    for term in terms.get("OnDemand", {}).get(sku, {}).values():
        for dim in term["priceDimensions"].values():
            out.append({"unit": dim["unit"], "usd": float(dim["pricePerUnit"].get("USD", "nan")),
                        "description": dim["description"]})
    return out


def lookup(price_file: Path, *, region: str, engine: str, licence: str, instance_class: str,
           storage_type: str, multi_az: bool) -> dict:
    doc = json.loads(Path(price_file).read_text(encoding="utf-8"))
    products, terms = doc["products"], doc["terms"]
    deployment = "Multi-AZ" if multi_az else "Single-AZ"
    loc = LOCATION[region]

    instance, storage = [], []
    for sku, p in products.items():
        a = p.get("attributes", {})
        if a.get("location") != loc or a.get("deploymentOption") != deployment:
            continue
        # RDS Custom is a different product (you get the OS) with its own SKUs.
        # On 2026-09-11 it priced db.t3.medium EE BYOL identically, which is
        # exactly why it has to be excluded by name rather than by price.
        if a.get("deploymentModel") == "Custom":
            continue
        if (p.get("productFamily") == "Database Instance" and a.get("instanceType") == instance_class
                and a.get("databaseEngine") == "Oracle" and a.get("databaseEdition") == EDITION[engine]
                and a.get("licenseModel") == LICENCE[licence]):
            instance += [{**x, "sku": sku, "operation": a.get("operation")} for x in _on_demand(terms, sku)]
        elif (p.get("productFamily") == "Database Storage" and a.get("volumeType") == VOLUME[storage_type]
              and a.get("databaseEngine") in ("Oracle", "Any")):
            storage += [{**x, "sku": sku, "operation": a.get("operation")} for x in _on_demand(terms, sku)]

    # Storage is listed once per engine code. Keep the line for the same engine
    # code as the instance (e.g. CreateDBInstance:0005 = Oracle EE BYOL).
    ops = {m["operation"] for m in instance}
    if len(ops) == 1:
        same = [s for s in storage if s["operation"] in ops]
        storage = same or storage

    return {"source": "AWS public price list, " + (doc.get("publicationDate") or "unknown date"),
            "location": loc, "deployment": deployment,
            "instance_matches": instance, "storage_matches": storage}


def estimate(prices: dict, storage_gb: int) -> dict | None:
    """Cost of running the target, or None when the price list was ambiguous."""
    inst = [m for m in prices["instance_matches"] if m["unit"] == "Hrs"]
    stor = [m for m in prices["storage_matches"] if m["unit"] == "GB-Mo"]
    if len(inst) != 1 or len({m["usd"] for m in stor}) != 1:
        return None
    hourly = inst[0]["usd"]
    storage_month = stor[0]["usd"] * storage_gb
    return {
        "instance_per_hour": hourly,
        "storage_per_month": round(storage_month, 2),
        "per_hour_all_in": round(hourly + storage_month / 730, 4),
        "per_8h_day": round(hourly * 8 + storage_month / 30, 2),
        "if_left_running_30_days": round(hourly * 730 + storage_month, 2),
        "excludes": "Oracle licences (BYOL: you hold them), data transfer, S3, backups beyond "
                    "the free allowance",
    }
