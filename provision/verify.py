"""After a create: log in to the new target and check it is what the render said.

    python -m provision.verify

CloudFormation reporting CREATE_COMPLETE means the resources exist. It does not
mean the database has the character set a migration needs, or that the S3 role
is actually active. This asks the database and RDS directly, and compares every
answer with the provenance the render recorded -- the same "verify, don't
assume" as collector.verify in Phase 1.

It also settles the two things the render deliberately left unverified: whether
Oracle Text is present on RDS 19c, and whether RDS built a container database.

Read-only on both sides. The master password is read from SSM into memory and
never printed or written.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "provision"

from . import policy
from . import run as run_mod

PASS, FAIL, INFO = "pass", "fail", "info"


def _c(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def verify(session, deployed: dict, plan: dict) -> list[dict]:
    import oracledb

    expect = {x["property"]: x["value"] for x in plan["rendered"]["provenance"]}
    stack = deployed["stack_name"]
    checks: list[dict] = []

    rds = session.client("rds", region_name=policy.REGION)
    db = rds.describe_db_instances(DBInstanceIdentifier=stack)["DBInstances"][0]
    roles = {r["FeatureName"]: r["Status"] for r in db.get("AssociatedRoles", [])}
    checks.append(_c("s3_integration_role", PASS if roles.get("S3_INTEGRATION") == "ACTIVE" else FAIL,
                     f"S3_INTEGRATION role status {roles.get('S3_INTEGRATION', 'absent')}"))
    for prop, actual in [("DBInstanceClass", db["DBInstanceClass"]),
                         ("EngineVersion", db["EngineVersion"]),
                         ("LicenseModel", db["LicenseModel"])]:
        checks.append(_c(prop, PASS if actual == expect.get(prop) else FAIL,
                         f"{actual} (rendered {expect.get(prop)})"))

    ssm = session.client("ssm", region_name=policy.REGION)
    password = ssm.get_parameter(Name=plan["rendered"]["password_parameter"],
                                 WithDecryption=True)["Parameter"]["Value"]
    endpoint = deployed["outputs"]["Endpoint"]
    port = deployed["outputs"]["Port"]
    try:
        conn = oracledb.connect(user=policy.MASTER_USERNAME, password=password,
                                dsn=f"{endpoint}:{port}/{policy.DB_NAME}")
    except oracledb.Error as exc:
        checks.append(_c("connect", FAIL, str(exc).splitlines()[0]))
        return checks
    finally:
        password = None   # noqa: F841 -- drop the reference as soon as it is used

    with conn:
        cur = conn.cursor()

        def one(sql):
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None

        checks.append(_c("connect", PASS, f"logged in as {policy.MASTER_USERNAME}"))
        version = one("SELECT version_full FROM v$instance")
        checks.append(_c("version", PASS if str(version).startswith(policy.TARGET_MAJOR + ".") else FAIL,
                         f"{version}"))
        for param, prop in [("NLS_CHARACTERSET", "CharacterSetName"),
                            ("NLS_NCHAR_CHARACTERSET", "NcharCharacterSetName")]:
            actual = one(f"SELECT value FROM nls_database_parameters WHERE parameter = '{param}'")
            if prop in expect:
                checks.append(_c(prop, PASS if actual == expect[prop] else FAIL,
                                 f"{actual} (rendered {expect[prop]})"))
        # The two questions the render left open, answered by the database itself.
        checks.append(_c("container_database", INFO, f"v$database.cdb = {one('SELECT cdb FROM v$database')}"))
        text = one("SELECT status FROM dba_registry WHERE comp_id = 'CONTEXT'")
        checks.append(_c("oracle_text", PASS if text == "VALID" else FAIL,
                         f"CONTEXT component {text or 'not installed'} -- needed by IX_COMM_NOTES_TEXT (RDS-008)"))
    return checks


def main(argv: list[str] | None = None) -> int:
    import boto3

    out = run_mod.OUTPUT
    deployed_path = out / "deployed.json"
    if not deployed_path.exists():
        print("nothing deployed -- run python -m provision.deploy first")
        return 1
    deployed = json.loads(deployed_path.read_text(encoding="utf-8"))
    plan = json.loads((out / "provision_plan.json").read_text(encoding="utf-8"))
    checks = verify(boto3.Session(profile_name=run_mod.DEFAULT_PROFILE), deployed, plan)
    for c in checks:
        print(f"  [{c['status'].upper():<4}] {c['name']:<22} {c['detail']}")
    (out / "verify.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    return 1 if any(c["status"] == FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
