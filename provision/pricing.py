"""Hourly and monthly cost, read from AWS's price list -- never guessed.

`lookup_live` is the source: the Price List Query API, through pricing.query, the
same path the price card uses, so the estimate, the card and the deploy gate
agree. `lookup` reads the regional offer file AWS publishes at
pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/<region>/
index.json and is only the fallback when the API cannot answer (a role without
pricing:GetProducts, or offline). Both return the same shape and both say which
they were in `source`; provision.run never uses the two at once. The file is
passed in as a path and kept out of the repo; prices change, and a stale number
committed to git is worse than none.

Returns every matching price rather than picking one silently. More than one
match means the filter is ambiguous and a person should look.
"""

from __future__ import annotations

import json
from pathlib import Path

import awsregion

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
    loc = awsregion.name(region)

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
        # PostgreSQL has no edition and no licence model in the offer file --
        # `licenseModel` is "No license required" -- so matching on EDITION and
        # LICENCE the way the Oracle branch does would find nothing and the
        # deploy would be refused for want of a price that is right there.
        if engine == "postgres":
            matches_engine = (a.get("databaseEngine") == "PostgreSQL"
                              and not a.get("databaseEdition"))
        else:
            matches_engine = (a.get("databaseEngine") == "Oracle"
                              and a.get("databaseEdition") == EDITION[engine]
                              and a.get("licenseModel") == LICENCE[licence])
        if (p.get("productFamily") == "Database Instance"
                and a.get("instanceType") == instance_class and matches_engine):
            instance += [{**x, "sku": sku, "operation": a.get("operation")} for x in _on_demand(terms, sku)]
        elif (p.get("productFamily") == "Database Storage" and a.get("volumeType") == VOLUME[storage_type]
              and a.get("databaseEngine") in ("Oracle", "PostgreSQL", "Any")):
            storage += [{**x, "sku": sku, "operation": a.get("operation")} for x in _on_demand(terms, sku)]

    return _narrowed("AWS public price list, " + (doc.get("publicationDate") or "unknown date"),
                     loc, deployment, instance, storage)


def _narrowed(source: str, loc: str, deployment: str, instance: list, storage: list) -> dict:
    # Storage is listed once per engine code. Keep the line for the same engine
    # code as the instance (e.g. CreateDBInstance:0005 = Oracle EE BYOL).
    ops = {m["operation"] for m in instance}
    if len(ops) == 1:
        same = [s for s in storage if s["operation"] in ops]
        storage = same or storage

    return {"source": source, "location": loc, "deployment": deployment,
            "instance_matches": instance, "storage_matches": storage}


def lookup_live(session, *, region: str, engine: str, licence: str, instance_class: str,
                storage_type: str, multi_az: bool) -> dict:
    """The same answer as `lookup`, from the Price List API. Raises
    pricing.query.PricingUnavailable when the API cannot give one."""
    from pricing import query
    products = query.rds_instance_products(session, region=region, instance_type=instance_class,
                                           engine=engine, licence=licence, multi_az=multi_az)
    storage = query.rds_storage_products(session, region=region, volume_type=storage_type,
                                         multi_az=multi_az)
    return _narrowed("AWS Price List API (GetProducts)", awsregion.name(region),
                     "Multi-AZ" if multi_az else "Single-AZ",
                     [r for i in products for r in query.rows(i)],
                     [r for i in storage for r in query.rows(i)])


def estimate(prices: dict, storage_gb: int, *, oracle: bool = True) -> dict | None:
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
        # What the hourly rate does not cover, which differs by engine: on
        # PostgreSQL there is no licence to exclude, and saying "Oracle licences"
        # there reads as a cost the client does not have.
        "excludes": (("Oracle licences (BYOL: you hold them), " if oracle else "")
                     + "data transfer, S3, backups beyond the free allowance"),
    }
