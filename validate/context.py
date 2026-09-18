"""Phase 8 plumbing: which estate to compare, and the two read-only connections.

The estate compared is **the collector run the target was built from**, read from
the RDS instance's own `collector_run_id` tag -- not whatever the output folders
hold today. On 2026-09-11 those folders moved on to a later discovery run while
the target still held the earlier one; validating against "the latest files"
would have compared a database with facts it was never built from.

Nothing in Phase 8 writes to either database. Every statement is a SELECT.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from provision import policy as prov_policy
from provision import records

MATCH = "match"
EXPECTED = "expected_difference"   # different, and correctly so -- with the reason
MISMATCH = "mismatch"              # different and unexplained; only these fail the phase
NOT_COMPARABLE = "not_comparable"  # one side could not be read; never guessed at

# Names Oracle generates and versions differ on. Compared as counts, never as names.
INTERNAL_PREFIXES = ("DR$", "SYS_", "AQ$_", "MLOG$", "RUPD$", "BIN$")


def is_internal(name: str) -> bool:
    return str(name or "").startswith(INTERNAL_PREFIXES)


@dataclass
class Options:
    collector_user: str = "dbmig_collector"
    collector_password: str | None = None
    source_dsn: str = "localhost:1521/XEPDB1"
    checksum: bool = True          # level 4 reads every row on both sides
    max_checksum_rows: int | None = None   # skip tables above this, rather than run for hours
    # Which engine the target runs. Set from the Phase 3 decision, not guessed:
    # it decides how every comparison is built, because "the same data" is not
    # the same bytes across engines.
    target_engine: str = "ORACLE"
    target_password: str | None = None     # PostgreSQL master password, memory only
    # A PostgreSQL target that is not an RDS instance: host:port/dbname.
    #
    # Every path here otherwise reads the target's endpoint from RDS and the
    # estate from the instance's own tags -- good discipline, because it means a
    # validation cannot be run against a database nobody can identify. It also
    # makes the phase untestable without a live instance, which is how the five
    # levels went unexercised on the heterogeneous path.
    #
    # With this set, the endpoint and the estate come from the caller instead
    # and the record says so: `target_source: "local"` rather than the instance
    # tags. It is for a demo or a rehearsal against the Docker PostgreSQL, never
    # for a client's cutover -- a validation whose target nobody can name is not
    # evidence.
    target_dsn: str | None = None
    # Required with `target_dsn`, because the estate is what decides which
    # objects to compare and no tag is available to read it from.
    target_estate: str | None = None
    target_run_id: str | None = None
    target_user: str | None = None


def finding(level: int, check: str, verdict: str, detail: str, why: str = "", **evidence) -> dict:
    return {"level": level, "check": check, "verdict": verdict, "detail": detail,
            "why": why, "evidence": evidence}


@dataclass
class Ctx:
    session: object
    opts: Options
    plan: dict
    emit: Callable
    run_id: str = ""
    estate: str = ""
    level: int = 0
    state: dict = field(default_factory=dict)
    _target: object = None
    _source: object = None

    # ---- what to compare -----------------------------------------------------
    def resolve_estate(self) -> dict:
        """The collector run the target was built from, from the instance's tag.

        With `target_dsn` the instance does not exist, so the estate and run id
        come from the caller. Recorded as `target_source: "local"` so a reader
        can tell a demo validation from one whose target identified itself.
        """
        if self.opts.target_dsn:
            if not (self.opts.target_estate and self.opts.target_run_id):
                raise RuntimeError(
                    "a local target has no tags to read, so --target-estate and "
                    "--target-run must both be given: without them this cannot say "
                    "what it is comparing")
            self.run_id = self.opts.target_run_id
            self.estate = self.opts.target_estate
            self.state["instance_status"] = "local (not an RDS instance)"
            self.state["target_source"] = "local"
            host, rest = self.opts.target_dsn.split(":", 1)
            port, database = rest.split("/", 1)
            self.state["outputs"] = {"Endpoint": host, "Port": port, "Database": database}
            if not (records.COLLECTOR_OUTPUT / self.run_id).exists():
                raise RuntimeError(f"collector run {self.run_id} has no discovery output")
            # The same three keys the RDS path returns, so every caller and
            # every event reads one shape. "available" because a local
            # database that answered is available; `target_source` in the
            # state is what distinguishes this from an instance.
            return {"run_id": self.run_id, "estate": self.estate, "status": "available"}
        stack = self.plan["stack_name"]
        db = self.session.client("rds", region_name=prov_policy.REGION).describe_db_instances(
            DBInstanceIdentifier=stack)["DBInstances"][0]
        tags = {t["Key"]: t["Value"] for t in db.get("TagList", [])}
        self.run_id = tags.get("collector_run_id") or ""
        self.estate = tags.get("estate") or ""
        self.state["instance_status"] = db["DBInstanceStatus"]
        if not self.run_id or not self.estate:
            raise RuntimeError(f"{stack} carries no collector_run_id/estate tag; cannot say what to compare")
        if not (records.COLLECTOR_OUTPUT / self.run_id).exists():
            raise RuntimeError(f"the target was built from collector run {self.run_id}, whose discovery "
                               "output is no longer on disk; there is nothing to compare against")
        return {"run_id": self.run_id, "estate": self.estate, "status": db["DBInstanceStatus"]}

    def data(self, name: str) -> list[dict]:
        return records._dataset(self.run_id, name)

    def log(self, line) -> None:
        self.emit({"event": "log", "level": self.level, "line": str(line)[:600]})

    @property
    def cross_engine(self) -> bool:
        """True when source and target are different engines.

        Everything downstream branches on this rather than on a connection
        type, so a comparison cannot silently use Oracle rules against a
        PostgreSQL target.
        """
        return self.opts.target_engine == "POSTGRESQL"

    # ---- connections ---------------------------------------------------------
    def target(self):
        if self.cross_engine:
            return self._postgres_target()
        if self._target is None:
            import oracledb
            pw = self.session.client("ssm", region_name=prov_policy.REGION).get_parameter(
                Name=self.plan["rendered"]["password_parameter"], WithDecryption=True)["Parameter"]["Value"]
            out = self.state["outputs"]
            self._target = oracledb.connect(user=prov_policy.MASTER_USERNAME, password=pw,
                                            dsn=f"{out['Endpoint']}:{out['Port']}/{prov_policy.DB_NAME}")
            self._normalise(self._target)
        return self._target

    def _postgres_target(self):
        """The PostgreSQL target, when Phase 3 chose the heterogeneous path.

        Separate from the Oracle path rather than a branch inside it: the
        driver, the credential source and the session normalisation are all
        different, and threading three `if`s through one function would hide
        that.
        """
        if self._target is None:
            import pg8000.dbapi
            out = self.state["outputs"]
            pw = self.opts.target_password
            if not pw:
                pw = self.session.client("ssm", region_name=prov_policy.REGION).get_parameter(
                    Name=self.plan["rendered"]["password_parameter"],
                    WithDecryption=True)["Parameter"]["Value"]
            self._target = pg8000.dbapi.connect(
                host=out["Endpoint"], port=int(out["Port"]),
                database=out.get("Database") or prov_policy.PG_DB_NAME,
                user=self.opts.target_user or prov_policy.PG_MASTER_USERNAME,
                password=pw)
            cur = self._target.cursor()
            # The same reason the Oracle side normalises: a session-level
            # format difference would make identical data hash differently.
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute("SET extra_float_digits = 0")
            cur.close()
        return self._target

    def source(self):
        if self._source is None:
            import oracledb
            if not self.opts.collector_password:
                raise RuntimeError("the read-only collector login is needed to read the source")
            self._source = oracledb.connect(user=self.opts.collector_user,
                                            password=self.opts.collector_password, dsn=self.opts.source_dsn)
            self._normalise(self._source)
        return self._source

    @staticmethod
    def _normalise(conn) -> None:
        """Same formatting on both sides, or the checksums differ for no reason."""
        cur = conn.cursor()
        cur.execute("ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '.,'")
        cur.execute("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'")
        cur.close()

    def rows(self, conn, sql: str, binds: dict | None = None, *, quiet: bool = True) -> list[tuple]:
        if not quiet:
            self.log("SQL> " + " ".join(sql.split())[:400])
        cur = conn.cursor()
        cur.execute(sql, binds or {})
        return cur.fetchall()

    def one(self, conn, sql: str, binds: dict | None = None, *, quiet: bool = True):
        got = self.rows(conn, sql, binds, quiet=quiet)
        return got[0][0] if got else None

    def both(self, sql: str, binds: dict | None = None, *, target_sql: str | None = None) -> tuple:
        """One query per side. Returns (source, target), where either may be an
        error string -- a side that cannot be read is never guessed.

        `target_sql` is how the heterogeneous path works: the same *question*
        asked in the target's own dialect. Without it a cross-engine run would
        send Oracle SQL to PostgreSQL and read the resulting syntax error as a
        difference in the data.
        """
        if target_sql is None and self.cross_engine:
            raise ValueError(
                "a cross-engine comparison needs target_sql: Oracle SQL cannot be sent to "
                "PostgreSQL, and a syntax error there is not evidence about the data")
        out = []
        for conn, text in ((self.source, sql), (self.target, target_sql or sql)):
            opened = None
            try:
                opened = conn()
                out.append(self.rows(opened, text, binds))
            except Exception as exc:  # noqa: BLE001
                out.append(f"error: {str(exc).splitlines()[0]}")
                # PostgreSQL refuses every later statement on a connection whose
                # transaction failed ("current transaction is aborted"). Without
                # this rollback one missing table turns the remaining questions
                # into 25P02 errors, and they get reported under whatever cause
                # the caller attributes to an unreadable side -- a guess, about
                # tables that were never actually asked.
                if opened is not None:
                    try:
                        opened.rollback()
                    except Exception:  # noqa: BLE001
                        pass
        return out[0], out[1]

    def close(self) -> None:
        for conn in (self._target, self._source):
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


def reachability(ctx: "Ctx") -> dict:
    """Can this machine reach the target at all, and if not, why?

    Without this the first run produced eleven DPY-6005 connection errors and no
    diagnosis. The usual cause is mundane: the security group admits one address,
    fixed when the stack was deployed, and a home or office address changes.
    """
    try:
        ctx.target()
        return {"ok": True, "detail": "target reachable"}
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).splitlines()[0]

    allowed, mine = [], None
    try:
        cfn = ctx.session.client("cloudformation", region_name=prov_policy.REGION)
        sg = cfn.describe_stack_resources(StackName=ctx.plan["stack_name"],
                                          LogicalResourceId="DbSecurityGroup"
                                          )["StackResources"][0]["PhysicalResourceId"]
        ec2 = ctx.session.client("ec2", region_name=prov_policy.REGION)
        for perm in ec2.describe_security_groups(GroupIds=[sg])["SecurityGroups"][0]["IpPermissions"]:
            allowed += [r["CidrIp"] for r in perm.get("IpRanges", [])]
    except Exception:  # noqa: BLE001 -- diagnosis is best effort
        pass
    try:
        import urllib.request
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as resp:
            mine = resp.read().decode().strip()
    except Exception:  # noqa: BLE001
        pass

    remedy = None
    if mine and allowed and f"{mine}/32" not in allowed:
        remedy = (f"the listener admits {', '.join(allowed)}, and this machine is now {mine}/32. "
                  "A home or office address changes; update the security group rule, or redeploy "
                  "with the current address, then validate again.")
    return {"ok": False, "detail": f"cannot reach the target -- {detail}", "remedy": remedy,
            "allowed": allowed, "this_machine": f"{mine}/32" if mine else None}


def load_plan() -> dict:
    from provision import run as provision_run
    path = provision_run.OUTPUT / "provision_plan.json"
    if not path.exists():
        raise RuntimeError("no provisioned target: Phase 6 has not rendered one")
    return json.loads(path.read_text(encoding="utf-8"))
