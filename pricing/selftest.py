"""Offline checks for the Price List lookup. No AWS.

The fake client returns rows shaped like the real catalogue (attribute names
taken from the AWSDatabaseMigrationSvc and AmazonRDS ap-south-1 offer files,
2026-09-25), including the rows that must be rejected: RDS Custom, Multi-AZ,
and DMS's CPU-credit line. The point is that the right row is chosen, and that
anything short of exactly one price is "unavailable" rather than a guess.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "pricing"

from botocore.exceptions import ClientError, NoCredentialsError

from . import query as Q

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}\n       got  {got!r}\n       want {want!r}")


def item(sku, family, usd, **attrs):
    return json.dumps({
        "product": {"sku": sku, "productFamily": family, "attributes": attrs},
        # GetProducts shape: the term is keyed "<sku>.<offerTermCode>" directly
        # under OnDemand. (The offer *file* wraps it once more, by sku.)
        "terms": {"OnDemand": {f"{sku}.JRTCKXETXF": {"priceDimensions": {
            f"{sku}.JRTCKXETXF.6YS6EN2CT7": {"unit": "Hrs", "pricePerUnit": {"USD": str(usd)}}}}}}})


def dms(sku, usd, itype, az="Single", usage=None, op="CreateDMSInstance", region="ap-south-1"):
    usage = usage or ("APS3-InstanceUsg:dms." if az == "Single" else "APS3-Multi-AZUsg:dms.") + itype
    return item(sku, "Replication Server", usd, regionCode=region, instanceType=itype,
                availabilityZone=az, operation=op, usagetype=usage, vcpu="2", memory="16 GiB")


def rds(sku, usd, itype="db.r6i.2xlarge", engine="Oracle", edition="Enterprise",
        lic="Bring your own license", dep="Single-AZ", model=None, region="ap-south-1"):
    a = dict(regionCode=region, instanceType=itype, databaseEngine=engine, deploymentOption=dep,
             licenseModel=lic, vcpu="8", memory="64 GiB")
    if edition:
        a["databaseEdition"] = edition
    if model:
        a["deploymentModel"] = model
    return item(sku, "Database Instance", usd, **a)


class FakeSession:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []

    def client(self, name, region_name=None):
        assert name == "pricing" and region_name == Q.API_REGION
        return self

    def get_paginator(self, op):
        assert op == "get_products"
        return self

    def paginate(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return [{"PriceList": self.rows}]


def unavailable(**kw):
    Q._cache.clear()
    try:
        Q.price(**kw)
    except Q.PricingUnavailable as exc:
        return exc.code
    return "priced"


def refused(**kw):
    try:
        Q.price(**kw)
    except Q.PricingError:
        return True
    return False


def main() -> int:
    print("DMS")
    Q._cache.clear()
    s = FakeSession([dms("A", 0.26, "r6i.large"), dms("B", 0.53, "r6i.large", az="Multiple"),
                     dms("C", 0.05, "r6i.large", usage="APS3-CPUCredits:dms.r6i")])
    out = Q.price(s, resource_type="dms", region="ap-south-1", instance_type="dms.r6i.large")
    check("single-AZ price picked, not Multi-AZ or CPU credits", out["hourlyPrice"], 0.26)
    check("monthly is hourly x 730", out["monthlyPrice"], round(0.26 * 730, 2))
    check("service is the DMS code", out["service"], "AWSDatabaseMigrationSvc")
    check("catalogue's service code is used in the query", s.calls[0]["ServiceCode"], "AWSDatabaseMigrationSvc")
    check("dms. prefix is stripped for the instanceType filter",
          {"Type": "TERM_MATCH", "Field": "instanceType", "Value": "r6i.large"} in s.calls[0]["Filters"], True)
    check("vcpu and memory come from the catalogue", (out["vcpu"], out["memory"]), (2, "16 GiB"))
    check("DMS never asks the RDS catalogue",
          all(c["ServiceCode"] != "AmazonRDS" for c in s.calls), True)

    print("RDS")
    Q._cache.clear()
    s = FakeSession([rds("A", 3.1), rds("B", 3.1, model="Custom"),
                     rds("C", 6.2, dep="Multi-AZ"), rds("D", 3.9, lic="License included")])
    out = Q.price(s, resource_type="rds", region="ap-south-1", instance_type="db.r6i.2xlarge",
                  engine="oracle-ee", licence="bring-your-own-license")
    check("BYOL single-AZ price picked", out["hourlyPrice"], 3.1)
    check("service is the RDS code", (out["service"], s.calls[0]["ServiceCode"]), ("AmazonRDS", "AmazonRDS"))
    check("one product survives validation (Custom, Multi-AZ, LI rejected)", out["matchedProducts"], 1)

    Q._cache.clear()
    s = FakeSession([rds("A", 1.0, engine="PostgreSQL", edition="", lic="No license required",
                         itype="db.r6i.large"),
                     rds("B", 1.0, engine="PostgreSQL", edition="Enterprise", itype="db.r6i.large")])
    out = Q.price(s, resource_type="rds", region="ap-south-1", instance_type="db.r6i.large", engine="postgres")
    check("PostgreSQL: the row with an edition is rejected", out["matchedProducts"], 1)

    print("both catalogue shapes")
    dim = {"unit": "Hrs", "pricePerUnit": {"USD": "0.086"}}
    api = {"product": {"sku": "K", "attributes": {}}, "terms": {"OnDemand": {"K.T": {"priceDimensions": {"K.T.D": dim}}}}}
    offer_file = {"product": {"sku": "K", "attributes": {}}, "terms": {"OnDemand": {"K": {"K.T": {"priceDimensions": {"K.T.D": dim}}}}}}
    check("GetProducts shape yields the price", [r["usd"] for r in Q.rows(api)], [0.086])
    check("offer-file shape yields the same price", [r["usd"] for r in Q.rows(offer_file)], [0.086])

    print("ambiguity and absence")
    rows = [rds("A", 3.1), rds("B", 3.4)]
    check("two different prices -> ambiguous",
          unavailable(session=FakeSession(rows), resource_type="rds", region="ap-south-1",
                      instance_type="db.r6i.2xlarge", engine="oracle-ee", licence="bring-your-own-license"),
          "ambiguous")
    rows = [rds("A", 3.1), rds("B", 3.1)]
    Q._cache.clear()
    check("same price under two SKUs is accepted",
          Q.price(FakeSession(rows), resource_type="rds", region="ap-south-1", instance_type="db.r6i.2xlarge",
                  engine="oracle-ee", licence="bring-your-own-license")["hourlyPrice"], 3.1)
    check("no rows -> no-match",
          unavailable(session=FakeSession([]), resource_type="dms", region="ap-south-1",
                      instance_type="dms.r6i.large"), "no-match")
    check("a row for another region is not accepted",
          unavailable(session=FakeSession([dms("A", 0.26, "r6i.large", region="us-east-1")]),
                      resource_type="dms", region="ap-south-1", instance_type="dms.r6i.large"), "no-match")

    print("AWS failures degrade, they do not raise past the caller's handler")
    denied = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetProducts")
    check("access denied", unavailable(session=FakeSession(error=denied), resource_type="dms",
                                       region="ap-south-1", instance_type="dms.t3.small"), "access-denied")
    check("no credentials", unavailable(session=FakeSession(error=NoCredentialsError()), resource_type="dms",
                                        region="ap-south-1", instance_type="dms.t3.small"), "no-credentials")

    print("bad requests are refused before any call")
    s = FakeSession()
    check("unknown region", refused(session=s, resource_type="rds", region="mars-1", instance_type="db.t3.medium",
                                    engine="postgres"), True)
    check("DMS class handed to RDS", refused(session=s, resource_type="rds", region="ap-south-1",
                                             instance_type="dms.t3.small", engine="postgres"), True)
    check("RDS class handed to DMS", refused(session=s, resource_type="dms", region="ap-south-1",
                                             instance_type="db.t3.medium"), True)
    check("unknown resource type", refused(session=s, resource_type="ec2", region="ap-south-1",
                                           instance_type="db.t3.medium"), True)
    check("no AWS call was made", s.calls, [])

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
