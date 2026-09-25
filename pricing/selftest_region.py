"""Offline checks that the selected region and the live price reach everything.

No AWS. A fake Price List client filters the way GetProducts does, with a
different rate per region, so a price that ignored the region would be caught.

Groups:
  1. changing the region changes the price
  2. RDS phases ask AmazonRDS, Migrate asks the DMS catalogue -- never crossed
  3. the region reaches policy, DMS, the kill switch and the plan cache; and no
     source file pins a region literal any more
  4. no stale hard-coded DMS rate exists or overrides the live one
  5. one authoritative price: card, Provision estimate and DMS class list agree,
     and the API wins over a price file when both exist
  6. pricing failing degrades to "unavailable" and breaks nothing else
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))
    __package__ = "pricing"

from botocore.exceptions import ClientError

import awsregion
from dms import policy as dms_policy
from dms import run as dms_run
from killswitch import run as ks_run
from pricing import query as Q
from provision import policy as prov_policy
from provision import run as prov_run

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}\n       got  {got!r}\n       want {want!r}")


# ---- a catalogue with a different rate per region ---------------------------

DMS_RATE = {("ap-south-1", "t3.small"): 0.053, ("eu-west-1", "t3.small"): 0.078,
            ("ap-south-1", "t3.medium"): 0.106, ("eu-west-1", "t3.medium"): 0.156,
            ("ap-south-1", "c5.large"): 0.192, ("eu-west-1", "c5.large"): 0.222}
RDS_RATE = {"ap-south-1": 1.052, "eu-west-1": 1.30}
STORAGE_RATE = {"ap-south-1": 0.138, "eu-west-1": 0.127}


def _item(sku, family, usd, unit="Hrs", **attrs):
    return json.dumps({"product": {"sku": sku, "productFamily": family, "attributes": attrs},
                       # GetProducts shape: term keyed "<sku>.<offerTermCode>" under OnDemand
                       "terms": {"OnDemand": {f"{sku}.JRTCKXETXF": {"priceDimensions": {
                           f"{sku}.JRTCKXETXF.D": {"unit": unit, "description": family,
                                                   "pricePerUnit": {"USD": str(usd)}}}}}}})


def catalogue():
    rows = {"AWSDatabaseMigrationSvc": [], "AmazonRDS": []}
    for (region, cls), usd in DMS_RATE.items():
        rows["AWSDatabaseMigrationSvc"].append(_item(
            f"D-{region}-{cls}", "Replication Server", usd, regionCode=region, instanceType=cls,
            availabilityZone="Single", operation="CreateDMSInstance",
            usagetype=f"X-InstanceUsg:dms.{cls}", vcpu="2", memory="4 GiB"))
    for region, usd in RDS_RATE.items():
        rows["AmazonRDS"].append(_item(
            f"R-{region}", "Database Instance", usd, regionCode=region, instanceType="db.r6i.2xlarge",
            databaseEngine="Oracle", databaseEdition="Enterprise", licenseModel="Bring your own license",
            deploymentOption="Single-AZ", operation="CreateDBInstance:0005", vcpu="8", memory="64 GiB"))
    for region, usd in STORAGE_RATE.items():
        rows["AmazonRDS"].append(_item(
            f"S-{region}", "Database Storage", usd, unit="GB-Mo", regionCode=region,
            volumeType="General Purpose-GP3", deploymentOption="Single-AZ", databaseEngine="Any",
            operation="CreateDBInstance:0005"))
    return rows


class FakePricing:
    """Answers get_products like AWS: only rows matching every TERM_MATCH filter."""

    def __init__(self, error=None):
        self.error, self.calls, self.cat = error, [], catalogue()

    def client(self, name, region_name=None):
        assert name == "pricing"
        return self

    def get_paginator(self, op):
        return self

    def paginate(self, ServiceCode, Filters, **_):
        self.calls.append(ServiceCode)
        if self.error:
            raise self.error
        want = {f["Field"]: f["Value"] for f in Filters}
        out = []
        for raw in self.cat[ServiceCode]:
            p = json.loads(raw)["product"]
            flat = {**p["attributes"], "productFamily": p["productFamily"]}
            if all(flat.get(k) == v for k, v in want.items()):
                out.append(raw)
        return [{"PriceList": out}]


DENIED = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetProducts")
RDS_ARGS = dict(engine="oracle-ee", licence="bring-your-own-license", instance_class="db.r6i.2xlarge",
                storage_type="gp3", multi_az=False)


def set_region(code):
    awsregion._set = code


def main() -> int:
    saved_file, saved_set = awsregion.STATE_FILE, awsregion._set
    tmp = tempfile.TemporaryDirectory()
    awsregion.STATE_FILE = Path(tmp.name) / "target_region.json"
    awsregion._set = None
    try:
        return run()
    finally:
        awsregion.STATE_FILE, awsregion._set = saved_file, saved_set
        tmp.cleanup()


def run() -> int:
    print("regions")
    regs = awsregion.regions()
    check("every standard-partition region with RDS, DMS and CloudFormation is offered", len(regs) >= 30, True)
    check("ap-south-1, ap-southeast-1, us-east-1, eu-west-1 present",
          all(c in regs for c in ("ap-south-1", "ap-southeast-1", "us-east-1", "eu-west-1")), True)
    check("names come from AWS, not a hand list", regs["eu-west-1"], "Europe (Ireland)")
    check("the pricing module offers the same list", Q.REGIONS, regs)
    try:
        awsregion.set_region("mars-1")
        rejected = False
    except ValueError:
        rejected = True
    check("an unknown region cannot be selected", rejected, True)

    print("1. changing region changes pricing")
    s = FakePricing()
    Q._cache.clear()
    a = Q.price(s, resource_type="dms", region="ap-south-1", instance_type="dms.t3.small")["hourlyPrice"]
    b = Q.price(s, resource_type="dms", region="eu-west-1", instance_type="dms.t3.small")["hourlyPrice"]
    check("DMS price differs by region", (a, b), (0.053, 0.078))
    ra = Q.price(s, resource_type="rds", region="ap-south-1", instance_type="db.r6i.2xlarge",
                 engine="oracle-ee", licence="bring-your-own-license")["hourlyPrice"]
    rb = Q.price(s, resource_type="rds", region="eu-west-1", instance_type="db.r6i.2xlarge",
                 engine="oracle-ee", licence="bring-your-own-license")["hourlyPrice"]
    check("RDS price differs by region", (ra, rb), (1.052, 1.30))
    check("a region with no product for the class is unavailable, not another region's price",
          _code(lambda: Q.price(s, resource_type="dms", region="us-east-1", instance_type="dms.t3.small")),
          "no-match")

    print("2. phases price the right service")
    from web import server as W
    from fastapi import HTTPException
    Q._cache.clear()
    sess = FakePricing()
    W._aws_session = lambda: sess
    W.STATE.sizing = {"decision": {"instance_class": "db.r6i.2xlarge", "engine": "ORACLE", "edition": "EE"}}
    real_plan = W._provision_plan
    W._provision_plan = lambda: None
    seen = {}
    for phase, res, extra in (("target-sizing", "rds", {}), ("provision", "rds", {}),
                              ("migrate", "dms", {"instanceType": "dms.t3.small"})):
        sess.calls.clear()
        Q._cache.clear()
        out = W.aws_pricing(phase=phase, resourceType=res, **extra)
        seen[phase] = (out["available"], out["service"], sorted(set(sess.calls)))
    check("target-sizing asks AmazonRDS", seen["target-sizing"], (True, "AmazonRDS", ["AmazonRDS"]))
    check("provision asks AmazonRDS", seen["provision"], (True, "AmazonRDS", ["AmazonRDS"]))
    check("migrate asks the DMS catalogue only", seen["migrate"],
          (True, "AWSDatabaseMigrationSvc", ["AWSDatabaseMigrationSvc"]))

    def status(**kw):
        try:
            W.aws_pricing(**kw)
        except HTTPException as exc:
            return exc.status_code
    check("migrate cannot be asked for RDS", status(phase="migrate", resourceType="rds"), 400)
    check("provision cannot be asked for DMS", status(phase="provision", resourceType="dms"), 400)

    print("3. the selected region is propagated")
    W._provision_plan = real_plan
    set_region("eu-west-1")
    check("provision policy follows", prov_policy.REGION, "eu-west-1")
    check("dms policy follows", dms_policy.REGION, "eu-west-1")
    check("the console reports it", W.price_region()["region"], "eu-west-1")
    check("the pricing endpoint defaults to it",
          W.aws_pricing(phase="migrate", resourceType="dms", instanceType="dms.t3.small")["region"], "eu-west-1")
    check("kill switch scans it", "eu-west-1" in ks_run.regions_for(None, False, awsregion.current()), True)
    W.STATE.provision = {"stack_name": "dbshift-x", "region": "ap-south-1"}
    check("a plan rendered for another region is not reused", W._provision_plan(), None)
    W.STATE.provision = {"stack_name": "dbshift-x", "region": "eu-west-1"}
    check("a plan for this region is", W._provision_plan()["stack_name"], "dbshift-x")
    W.STATE.provision = None
    check("changing region does not touch the sizing decision",
          W.STATE.sizing["decision"]["instance_class"], "db.r6i.2xlarge")
    awsregion.mark_used("ap-south-1")
    check("kill switch still scans a region deployed to earlier",
          ks_run.regions_for(None, False, "eu-west-1"), ["ap-south-1", "eu-west-1"])
    check("...and the console scan does too", awsregion.scan_regions(), ["ap-south-1", "eu-west-1"])

    offenders = []
    pat = re.compile(r"""region_name\s*=\s*["'][a-z]{2}-[a-z]+-\d["']""")
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if ".venv" in rel or "selftest" in rel or rel.startswith(("bedrock/", "pricing/")):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line) and "us-east-1" not in line:
                offenders.append(f"{rel}:{n}")
    check("no source pins a region literal into a client", offenders, [])
    frozen = [f"{p.relative_to(ROOT).as_posix()}" for p in (ROOT / "provision" / "policy.py", ROOT / "dms" / "policy.py")
              if re.search(r'^REGION\s*=', p.read_text(encoding="utf-8"), re.M)]
    check("neither policy module freezes REGION as a constant", frozen, [])

    print("4. no stale hard-coded DMS price")
    check("no INSTANCE_CLASSES entry carries a rate", any("usd_per_hour" in c for c in dms_policy.INSTANCE_CLASSES), False)
    src = (ROOT / "dms" / "policy.py").read_text(encoding="utf-8")
    check("the old constants are gone", [r for r in ("0.036", "0.073", "0.154") if r in src], [])
    Q._cache.clear()
    set_region("ap-south-1")
    live = {c["class"]: c["usd_per_hour"] for c in dms_run.priced_classes(FakePricing())}
    check("the class list carries the live Mumbai rates", live,
          {"dms.t3.small": 0.053, "dms.t3.medium": 0.106, "dms.c5.large": 0.192})
    set_region("eu-west-1")
    live = {c["class"]: c["usd_per_hour"] for c in dms_run.priced_classes(FakePricing())}
    check("...and the live Ireland rates after a region change", live,
          {"dms.t3.small": 0.078, "dms.t3.medium": 0.156, "dms.c5.large": 0.222})
    check("selection logic is unchanged: same classes, same vCPU and memory",
          [(c["class"], c["vcpu"], c["memory_gb"]) for c in dms_run.priced_classes(FakePricing())],
          [(c["class"], c["vcpu"], c["memory_gb"]) for c in dms_policy.INSTANCE_CLASSES])

    print("5. one authoritative price")
    set_region("ap-south-1")
    Q._cache.clear()
    fake = FakePricing()
    card = Q.price(fake, resource_type="rds", region="ap-south-1", instance_type="db.r6i.2xlarge",
                   engine="oracle-ee", licence="bring-your-own-license")["hourlyPrice"]
    cost = prov_run._cost(fake, None, region="ap-south-1", storage_gb=100, **RDS_ARGS)
    check("the estimate's instance rate is the card's", cost["estimate"]["instance_per_hour"], card)
    check("and says it came from the API", cost["prices"]["source"], "AWS Price List API (GetProducts)")
    check("storage is priced from the API too", cost["estimate"]["storage_per_month"], round(0.138 * 100, 2))
    dms_card = Q.price(fake, resource_type="dms", region="ap-south-1", instance_type="dms.t3.small")["hourlyPrice"]
    check("the DMS class list agrees with the DMS card",
          {c["class"]: c["usd_per_hour"] for c in dms_run.priced_classes(fake)}["dms.t3.small"], dms_card)

    stale = Path(tmp_dir := tempfile.mkdtemp()) / "rds.json"
    stale.write_text(json.dumps(_offer_file(usd=9.99)), encoding="utf-8")
    both = prov_run._cost(fake, stale, region="ap-south-1", storage_gb=100, **RDS_ARGS)
    check("with the API answering, a price file cannot override it", both["estimate"]["instance_per_hour"], 1.052)

    print("6. pricing failure breaks nothing")
    Q._cache.clear()
    down = FakePricing(error=DENIED)
    classes = dms_run.priced_classes(down)
    check("DMS: every class is still listed", [c["class"] for c in classes],
          [c["class"] for c in dms_policy.INSTANCE_CLASSES])
    check("DMS: with no rate and the reason", {c["usd_per_hour"] for c in classes}, {None})
    check("DMS: reason names the missing permission",
          "pricing:GetProducts" in classes[0]["price_unavailable"], True)
    check("DMS: the API was not asked three times", len(down.calls), 1)
    check("DMS with no session at all still lists classes",
          len(dms_run.priced_classes(None)), len(dms_policy.INSTANCE_CLASSES))
    gone = prov_run._cost(down, None, region="ap-south-1", storage_gb=100, **RDS_ARGS)
    check("Provision: no estimate, a stated reason, no exception",
          (gone["estimate"], "No price could be established" in gone["unavailable"]), (None, True))
    fallback = prov_run._cost(down, stale, region="ap-south-1", storage_gb=100, **RDS_ARGS)
    check("Provision: the file is used only because the API could not answer, and labelled",
          (fallback["estimate"]["instance_per_hour"], fallback["prices"]["source"].startswith("AWS public price list")),
          (9.99, True))
    out = W.aws_pricing(phase="migrate", resourceType="dms", instanceType="dms.t3.small") \
        if False else None
    W._aws_session = lambda: down
    Q._cache.clear()
    r = W.aws_pricing(phase="target-sizing", resourceType="rds")
    check("Target & Sizing endpoint: available false with a reason, HTTP 200",
          (r["available"], r["code"]), (False, "access-denied"))
    check("...and the sizing decision it was asked about is untouched",
          W.STATE.sizing["decision"]["instance_class"], "db.r6i.2xlarge")
    r = W.aws_pricing(phase="migrate", resourceType="dms", instanceType="dms.t3.small")
    check("Migrate endpoint degrades the same way", (r["available"], r["code"]), (False, "access-denied"))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def _code(fn):
    try:
        fn()
    except Q.PricingUnavailable as exc:
        return exc.code
    return "priced"


def _offer_file(usd):
    """The shape of AWS's downloadable offer file, priced deliberately wrong."""
    def term(sku, price, unit):
        return {sku: {"t": {"priceDimensions": {"d": {"unit": unit, "description": "x",
                                                       "pricePerUnit": {"USD": str(price)}}}}}}
    products = {
        "I": {"productFamily": "Database Instance", "attributes": {
            "location": awsregion.name("ap-south-1"), "deploymentOption": "Single-AZ", "instanceType": "db.r6i.2xlarge",
            "databaseEngine": "Oracle", "databaseEdition": "Enterprise", "licenseModel": "Bring your own license",
            "operation": "CreateDBInstance:0005"}},
        "S": {"productFamily": "Database Storage", "attributes": {
            "location": awsregion.name("ap-south-1"), "deploymentOption": "Single-AZ",
            "volumeType": "General Purpose-GP3", "databaseEngine": "Any", "operation": "CreateDBInstance:0005"}},
    }
    return {"publicationDate": "2020-01-01", "products": products,
            "terms": {"OnDemand": {**term("I", usd, "Hrs"), **term("S", 0.5, "GB-Mo")}}}


if __name__ == "__main__":
    sys.exit(main())
