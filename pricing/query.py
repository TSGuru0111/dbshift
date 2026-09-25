"""Live price for one AWS resource, from the Price List Query API (GetProducts).

Two different products are priced here and they must never be mixed up:

    rds  -> service code AmazonRDS                  productFamily "Database Instance"
    dms  -> service code AWSDatabaseMigrationSvc    productFamily "Replication Server"

The DMS service code is `AWSDatabaseMigrationSvc`. `AWSDatabaseMigrationService`
is not in the catalogue (checked against aws/index.json on 2026-09-25).

This module is the only place a price is established. The price card, Provision's
estimate and its deploy gate, and Migrate's cost estimate all come through it, so
for one service + resource + instance class + region + engine/configuration there
is one price and no second lookup that could disagree.

Nothing is guessed. The filters narrow the query, but the response is then
checked attribute by attribute against what was asked, because a TERM_MATCH
filter is only as good as AWS's attribute naming and this catalogue has traps:
RDS Custom prices a db.t3.medium identically to RDS and has its own SKUs;
DMS lists a Multi-AZ product beside every Single-AZ one, at twice the rate. If
what comes back is not exactly one price, the answer is "unavailable", not the
first row.

The role this console runs under has not always had pricing:GetProducts. That is
an expected state, reported as `PricingUnavailable`, and callers keep the
existing recommendation and say "Pricing unavailable" rather than failing.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable

from botocore.exceptions import (BotoCoreError, ClientError, EndpointConnectionError,
                                 NoCredentialsError, ProfileNotFound)

import awsregion

HOURS_PER_MONTH = 730

# RDS attribute spellings. Defined here, not in provision.pricing, so this module
# never imports it: provision.pricing imports this one for the live lookup.
EDITION = {"oracle-ee": "Enterprise", "oracle-se2": "Standard Two"}
LICENCE = {"bring-your-own-license": "Bring your own license", "license-included": "License included"}
VOLUME = {"gp3": "General Purpose-GP3", "gp2": "General Purpose"}

# The Price List API is served from a few regions only, whatever region is being
# priced. us-east-1 is one of them.
API_REGION = "us-east-1"

RDS, DMS = "rds", "dms"
SERVICE = {RDS: "AmazonRDS", DMS: "AWSDatabaseMigrationSvc"}

# Which resource each phase prices. The server enforces this so the Migrate
# screen cannot be handed an RDS price, or Provision a DMS one.
PHASE_RESOURCE = {"target-sizing": RDS, "provision": RDS, "migrate": DMS}

# Every standard-partition region with RDS, DMS and CloudFormation -- see awsregion.
REGIONS = awsregion.regions()

RDS_CLASS = re.compile(r"^db\.[a-z0-9]+\.[a-z0-9]+$")
DMS_CLASS = re.compile(r"^dms\.[a-z0-9]+\.[a-z0-9]+$")

_CACHE_SECONDS = 3600
_cache: dict[tuple, tuple[float, list]] = {}


class PricingError(Exception):
    """The request itself is wrong: bad region, bad class, wrong resource. A 400."""


class PricingUnavailable(Exception):
    """The request is fine but no single price could be established. The UI
    shows "Pricing unavailable" and `reason` says why."""

    def __init__(self, reason: str, code: str):
        super().__init__(reason)
        self.reason, self.code = reason, code


def _filters(pairs: dict) -> list[dict]:
    return [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in pairs.items()]


def _on_demand_terms(item: dict, sku: str) -> list[dict]:
    """GetProducts and the downloadable offer file nest terms differently, and
    only the first is used here:

        GetProducts:  terms.OnDemand = {"<sku>.<offerTermCode>": {priceDimensions...}}
        offer file:   terms.OnDemand = {"<sku>": {"<sku>.<offerTermCode>": {...}}}

    Reading only the offer-file shape found no price on every live response.
    """
    out = []
    for key, value in item.get("terms", {}).get("OnDemand", {}).items():
        if "priceDimensions" in value:
            out.append(value)
        elif key == sku:
            out += [t for t in value.values() if "priceDimensions" in t]
    return out


def rows(item: dict) -> list[dict]:
    """Every on-demand price dimension of one product, in the shape
    provision.pricing.estimate() already reads."""
    out, product = [], item["product"]
    for term in _on_demand_terms(item, product["sku"]):
        for dim in term["priceDimensions"].values():
            if "USD" in dim["pricePerUnit"]:
                out.append({"sku": product["sku"], "unit": dim.get("unit"),
                            "usd": float(dim["pricePerUnit"]["USD"]),
                            "description": dim.get("description", ""),
                            "operation": product.get("attributes", {}).get("operation")})
    return out


def _rds_query(region, instance_type, engine, licence, multi_az) -> tuple[dict, Callable]:
    deployment = "Multi-AZ" if multi_az else "Single-AZ"
    want = {"productFamily": "Database Instance", "regionCode": region,
            "instanceType": instance_type, "deploymentOption": deployment}
    if engine == "postgres":
        want["databaseEngine"] = "PostgreSQL"
    elif engine in EDITION and licence in LICENCE:
        want.update(databaseEngine="Oracle", databaseEdition=EDITION[engine],
                    licenseModel=LICENCE[licence])
    else:
        raise PricingError(f"no price mapping for RDS engine {engine!r} / licence {licence!r}")

    def accept(product: dict) -> bool:
        a = product.get("attributes", {})
        if a.get("deploymentModel") == "Custom":     # RDS Custom: a different product
            return False
        if engine == "postgres" and a.get("databaseEdition"):
            return False                             # PostgreSQL has no edition
        # Every filter attribute again, so a filter AWS ignores cannot slip a row in.
        return (product.get("productFamily") == "Database Instance"
                and all(a.get(k) == v for k, v in want.items() if k != "productFamily"))
    return want, accept


def _storage_query(region, volume_type, multi_az) -> tuple[dict, Callable]:
    if volume_type not in VOLUME:
        raise PricingError(f"no price mapping for storage type {volume_type!r}")
    deployment = "Multi-AZ" if multi_az else "Single-AZ"
    want = {"productFamily": "Database Storage", "regionCode": region,
            "volumeType": VOLUME[volume_type], "deploymentOption": deployment}

    def accept(product: dict) -> bool:
        a = product.get("attributes", {})
        return (a.get("deploymentModel") != "Custom"
                and product.get("productFamily") == "Database Storage"
                and all(a.get(k) == v for k, v in want.items() if k != "productFamily")
                and a.get("databaseEngine") in ("Oracle", "PostgreSQL", "Any"))
    return want, accept


def _dms_query(region, instance_type, multi_az) -> tuple[dict, Callable]:
    # The catalogue drops the "dms." prefix: instanceType is "t3.small" and the
    # prefix survives only in usagetype ("APS3-InstanceUsg:dms.t3.small").
    bare = instance_type.removeprefix("dms.")
    az = "Multiple" if multi_az else "Single"
    want = {"productFamily": "Replication Server", "regionCode": region,
            "instanceType": bare, "operation": "CreateDMSInstance", "availabilityZone": az}

    def accept(product: dict) -> bool:
        a = product.get("attributes", {})
        return (product.get("productFamily") == "Replication Server"
                and all(a.get(k) == v for k, v in want.items() if k != "productFamily")
                and a.get("usagetype", "").endswith(f"Usg:{instance_type}"))
    return want, accept


def _parse_vcpu(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _check(resource_type, region, instance_type):
    if resource_type not in SERVICE:
        raise PricingError(f"unknown resource type {resource_type!r}; expected rds or dms")
    if region not in REGIONS:
        raise PricingError(f"unknown region {region!r}")
    pattern = RDS_CLASS if resource_type == RDS else DMS_CLASS
    if not pattern.match(instance_type or ""):
        kind = "RDS instance class (db.*)" if resource_type == RDS else "DMS replication instance class (dms.*)"
        raise PricingError(f"{instance_type!r} is not a valid {kind}")


def rds_instance_products(session, *, region, instance_type, engine, licence, multi_az=False) -> list[dict]:
    """The validated Amazon RDS instance products. The one place an RDS price is
    established: the price card, the Provision estimate and the deploy gate all
    come through here."""
    _check(RDS, region, instance_type)
    want, accept = _rds_query(region, instance_type, engine, licence, multi_az)
    return [i for i in _products(session, RDS, want) if accept(i["product"])]


def rds_storage_products(session, *, region, volume_type, multi_az=False) -> list[dict]:
    if region not in REGIONS:
        raise PricingError(f"unknown region {region!r}")
    want, accept = _storage_query(region, volume_type, multi_az)
    return [i for i in _products(session, RDS, want) if accept(i["product"])]


def price(session, *, resource_type: str, region: str, instance_type: str,
          engine: str | None = None, licence: str | None = None,
          multi_az: bool = False) -> dict:
    """One normalised price, or raises PricingError / PricingUnavailable."""
    _check(resource_type, region, instance_type)
    if resource_type == RDS:
        matches = rds_instance_products(session, region=region, instance_type=instance_type,
                                        engine=engine, licence=licence, multi_az=multi_az)
    else:
        want, accept = _dms_query(region, instance_type, multi_az)
        matches = [i for i in _products(session, DMS, want) if accept(i["product"])]
    return _normalise(resource_type, region, instance_type, matches)


def _products(session, resource_type, want) -> list[dict]:
    """GetProducts for one service and filter set, cached for an hour -- the price
    list changes on the scale of weeks, and one screen asks several times."""
    key = (resource_type, tuple(sorted(want.items())))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    try:
        client = session.client("pricing", region_name=API_REGION)
        pages = client.get_paginator("get_products").paginate(
            ServiceCode=SERVICE[resource_type], Filters=_filters(want),
            PaginationConfig={"PageSize": 100})
        items = [json.loads(raw) for page in pages for raw in page["PriceList"]]
    except (NoCredentialsError, ProfileNotFound) as exc:
        raise PricingUnavailable("AWS credentials are not configured", "no-credentials") from exc
    except EndpointConnectionError as exc:
        raise PricingUnavailable("cannot reach the AWS Price List API", "network") from exc
    except ClientError as exc:
        err = exc.response.get("Error", {})
        if err.get("Code") in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
            raise PricingUnavailable("this role is not allowed pricing:GetProducts", "access-denied") from exc
        if err.get("Code") in ("ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId"):
            raise PricingUnavailable("AWS credentials have expired -- paste fresh ones on the Config screen",
                                     "credentials-expired") from exc
        raise PricingUnavailable(f"AWS Price List API error: {err.get('Code')}", "aws-error") from exc
    except BotoCoreError as exc:
        raise PricingUnavailable(f"AWS Price List API call failed: {exc}", "aws-error") from exc
    _cache[key] = (time.monotonic(), items)
    return items


def _normalise(resource_type, region, instance_type, matches) -> dict:
    if not matches:
        raise PricingUnavailable(
            f"no {SERVICE[resource_type]} price for {instance_type} in {region} with this configuration",
            "no-match")

    # Distinct prices, not distinct rows: RDS lists one rate under several
    # engine-code SKUs, which is harmless. Two different rates is not.
    priced = [(r["usd"], i["product"]) for i in matches for r in rows(i) if r["unit"] == "Hrs"]
    if not priced:
        raise PricingUnavailable("the matching product carries no on-demand hourly price", "no-price")
    distinct = {h for h, _ in priced}
    if len(distinct) > 1:
        raise PricingUnavailable(
            f"{len(distinct)} different prices match; not guessing which applies", "ambiguous")

    hourly, product = priced[0]
    a = product["attributes"]
    return {
        "service": SERVICE[resource_type],
        "resourceType": resource_type,
        "region": region,
        "regionName": REGIONS[region],
        "instanceType": instance_type,
        "vcpu": _parse_vcpu(a.get("vcpu")),
        "memory": a.get("memory"),
        "hourlyPrice": hourly,
        "monthlyPrice": round(hourly * HOURS_PER_MONTH, 2),
        "monthlyBasis": f"hourly x {HOURS_PER_MONTH} hours; an estimate, not a bill",
        "currency": "USD",
        "matchedProducts": len(matches),
    }
