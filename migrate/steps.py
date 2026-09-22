"""Phase 7 -- the migration as an ordered list of steps a person can watch.

Every step declares, before it runs, what it does, why, what it changes, whether
that can be undone, and who or what decided it. Then it reports what it
actually did: each command or SQL statement as it runs (secrets masked), its
output, and the evidence it ends on. The console shows exactly this list.

`decided_by` is recorded honestly:

  rule           a deterministic check in this codebase
  gate           the Phase 5 blocker gate
  phase4_advice  an artefact from the Phase 4 plan -- today a hand-written static
                 fixture standing in for the model, never presented as one
  approval       a named person, recorded with the identity on the credentials
  orchestrator   plain sequencing; no judgement involved

The path is Data Pump end to end. It already loads in the order the architecture
asks for -- tables, then data, then indexes and constraints, then code -- which is
why foreign keys are not validated row by row during the load.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from provision import policy as prov_policy
from provision import records
from provision.deploy import ensure_password, identity_name

PASS, WARN, FAIL, STOP, SKIP = "pass", "warn", "fail", "stop", "skip"
IDENT = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
POLL_SECONDS = 5
# Runbook SQL comes from the Phase 4 plan. Only these shapes may run from it.
RUNBOOK_ALLOWED = re.compile(r"^\s*BEGIN\s+DBMS_(SCHEDULER|MVIEW)\.", re.IGNORECASE)


def q(name: str) -> str:
    """An identifier that is safe to put in SQL text, or an error."""
    if not IDENT.match(name or ""):
        raise ValueError(f"refusing to build SQL around identifier {name!r}")
    return name


def result(status: str, detail: str, **evidence) -> dict:
    return {"status": status, "detail": detail, "evidence": evidence}


@dataclass
class Options:
    source_owner_password: str | None = None   # only for a fresh export
    collector_password: str | None = None      # read-only source counts and file lookups
    collector_user: str = "dbmig_collector"
    source_dsn: str = "localhost:1521/XEPDB1"
    resolve_external_via_s3: bool = False      # the RDS-004 route, recorded as an approval
    fresh_export: bool = False
    resume_from: str | None = None             # re-run from a step; the checks always run


@dataclass
class Ctx:
    session: object
    opts: Options
    records: dict
    plan: dict
    emit: Callable
    step_id: str = ""
    state: dict = field(default_factory=dict)
    approvals: list = field(default_factory=list)
    _target: object = None
    _source: object = None
    _owner: object = None

    @property
    def run_id(self) -> str:
        return self.records["sizing"]["collector_run_id"]

    @property
    def estate(self) -> str:
        return records.estate_of(self.records["assessment"])

    def data(self, name: str) -> list[dict]:
        return records._dataset(self.run_id, name)

    def log(self, line) -> None:
        self.emit({"event": "step_log", "id": self.step_id, "line": str(line)[:600]})

    def client(self, name: str):
        return self.session.client(name, region_name=prov_policy.REGION)

    def target(self):
        if self._target is None:
            import oracledb
            pw = self.client("ssm").get_parameter(Name=self.plan["rendered"]["password_parameter"],
                                                  WithDecryption=True)["Parameter"]["Value"]
            out = self.state["outputs"]
            self._target = oracledb.connect(user=prov_policy.MASTER_USERNAME, password=pw,
                                            dsn=f"{out['Endpoint']}:{out['Port']}/{prov_policy.DB_NAME}")
        return self._target

    def source(self):
        if self._source is None:
            import oracledb
            self._source = oracledb.connect(user=self.opts.collector_user,
                                            password=self.opts.collector_password, dsn=self.opts.source_dsn)
        return self._source

    def owner_conn(self):
        """The schema owner on the target, for work an owner should do itself --
        building its own text index, compiling its own objects."""
        if self._owner is None:
            import oracledb
            name = f"/dbshift/{self.plan['stack_name']}/{self.estate.lower()}-password"
            pw = self.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
            out = self.state["outputs"]
            self._owner = oracledb.connect(user=self.estate, password=pw,
                                           dsn=f"{out['Endpoint']}:{out['Port']}/{prov_policy.DB_NAME}")
        return self._owner

    def owner_sql(self, statement: str) -> None:
        self.log(f"SQL ({self.estate})> " + " ".join(statement.split())[:500])
        self.owner_conn().cursor().execute(statement)

    def sql(self, statement: str, binds: dict | None = None, *, show: str | None = None,
            fetch: bool = False, quiet: bool = False):
        """Run on the target. Logs the statement unless quiet; `show` replaces the
        logged text for anything that carries a secret."""
        if not quiet:
            self.log("SQL> " + " ".join((show or statement).split())[:500])
        cur = self.target().cursor()
        cur.execute(statement, binds or {})
        return cur.fetchall() if fetch else None

    def one(self, statement: str, binds: dict | None = None, *, quiet: bool = False):
        rows = self.sql(statement, binds, fetch=True, quiet=quiet)
        return rows[0][0] if rows else None

    def close(self) -> None:
        for conn in (self._target, self._source, self._owner):
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


# --------------------------------------------------------------------------- checks

def check_records(ctx: Ctx) -> dict:
    c = records.consistency(ctx.records)
    if c["status"] != "pass":
        return result(STOP, c["detail"], remedy=c["remedy"])
    return result(PASS, f"assessment, sizing, plan and gate all describe collector run {ctx.run_id}",
                  estate=ctx.estate)


def check_target(ctx: Ctx) -> dict:
    stack = ctx.plan["stack_name"]
    s = ctx.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    if s["StackStatus"] != "CREATE_COMPLETE":
        return result(STOP, f"{stack} is {s['StackStatus']}; migration starts at CREATE_COMPLETE")
    ctx.state["outputs"] = {o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])}
    ctx.log(f"endpoint {ctx.state['outputs'].get('Endpoint')}:{ctx.state['outputs'].get('Port')}")

    # The target must have been built from the same estate these records describe.
    db = ctx.client("rds").describe_db_instances(DBInstanceIdentifier=stack)["DBInstances"][0]
    built_from = {t["Key"]: t["Value"] for t in db.get("TagList", [])}.get("collector_run_id")
    if built_from != ctx.run_id:
        return result(STOP, f"the target was provisioned from collector run {built_from}; the records "
                            f"now describe run {ctx.run_id}. Refusing to migrate one run's estate into a "
                            "target built for another.")

    from provision import verify as verify_mod
    checks = verify_mod.verify(ctx.session, {"stack_name": stack, "outputs": ctx.state["outputs"]}, ctx.plan)
    for c in checks:
        ctx.log(f"[{c['status']}] {c['name']}: {c['detail']}")
    failed = {c["name"] for c in checks if c["status"] == "fail"}
    fatal = failed & {"connect", "CharacterSetName", "NcharCharacterSetName", "version"}
    if fatal:
        return result(STOP, "the target is not what the render promised: " + ", ".join(sorted(fatal)),
                      checks=checks)
    if failed:
        return result(WARN, "target reachable and correct where it matters; also failed: "
                      + ", ".join(sorted(failed)), checks=checks)
    return result(PASS, "logged in; version, character sets, class and S3 role match the render",
                  checks=checks)


def check_gate(ctx: Ctx) -> dict:
    g = ctx.records["gate"]
    full, cdc = g["by_phase"]["migrate_full_load"], g["by_phase"]["migrate_cdc"]
    resolvable = {"RDS-004"} if ctx.opts.resolve_external_via_s3 else set()
    remaining = [r for r in full["blocked_by"] if r not in resolvable]
    if remaining:
        return result(STOP, f"the full load is blocked by {', '.join(remaining)}",
                      remedy=("RDS-004 can be resolved in this phase: choose 'resolve via S3' and the "
                              "file moves to S3 with its directory recreated on RDS. Or waive it in "
                              "Phase 5." if "RDS-004" in remaining else "Resolve or waive it in Phase 5."))
    if resolvable & set(full["blocked_by"]):
        who = identity_name(ctx.session.client("sts").get_caller_identity()["Arn"])
        ctx.approvals.append({"rule_id": "RDS-004", "approved_by": who,
                              "route": "resolve in Phase 7: file to S3, directory recreated on RDS",
                              "at_utc": datetime.now(timezone.utc).isoformat()})
        ctx.log(f"RDS-004: {who} chose to resolve it in this phase rather than waive it")
    note = (f"Change data capture stays off -- blocked by {', '.join(cdc['blocked_by'])} -- so this is a "
            "one-time full load into an outage window." if cdc["blocked_by"]
            else "Change data capture is not blocked.")
    return result(PASS, "the full load may proceed. " + note,
                  full_load_blocked_by=full["blocked_by"], cdc_blocked_by=cdc["blocked_by"],
                  approvals=ctx.approvals)


# --------------------------------------------------------------------------- export and move

def _export_directory(ctx: Ctx) -> tuple[str | None, str | None]:
    """A directory the owner may write to, from discovery -- never assumed."""
    owner = ctx.estate
    writable = {g["table_name"] for g in ctx.data("table_privileges")
                if g["grantee"] == owner and g["privilege"] == "WRITE"}
    paths = {d["directory_name"]: d["directory_path"] for d in ctx.data("directories")}
    for name in sorted(writable):
        if name in paths:
            return name, paths[name]
    return None, None


def _last_job_line(log_path: Path) -> str | None:
    if not log_path.exists():
        return None
    lines = [ln for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
             if ln.startswith("Job ")]
    return lines[-1].strip() if lines else None


def export(ctx: Ctx) -> dict:
    owner = ctx.estate
    name, path = _export_directory(ctx)
    if not name:
        return result(STOP, f"discovery shows no directory {owner} can write to",
                      remedy="Grant READ, WRITE on a directory to the owner.")
    major = prov_policy.TARGET_MAJOR
    dump, logname = f"{owner}_V{major}.DMP", f"{owner.lower()}_v{major}_exp.log"
    dump_path, log_path = Path(path) / dump, Path(path) / logname
    ctx.state.update(dump=dump, dump_path=dump_path)
    ctx.log(f"directory {name} -> {path}  (the owner's WRITE grant, from discovery)")

    last = _last_job_line(log_path)
    if dump_path.exists() and last and "successfully completed" in last and not ctx.opts.fresh_export:
        size = dump_path.stat().st_size
        ctx.log(f"reusing {dump}: {last}")
        return result(PASS, f"reused the VERSION={major} export ({size / 1048576:.0f} MB): {last}",
                      dump=str(dump_path), bytes=size)

    if not ctx.opts.source_owner_password:
        return result(STOP, f"a fresh export needs the {owner} password",
                      remedy="Enter it in the console or set DBSHIFT_SOURCE_OWNER_PASSWORD. Held in memory only.")
    pw = ctx.opts.source_owner_password
    cmd = ["expdp", f"{owner}/{pw}@{ctx.opts.source_dsn}", f"schemas={owner}", f"version={major}",
           f"directory={name}", f"dumpfile={dump}", f"logfile={logname}", "reuse_dumpfiles=yes"]
    ctx.log("$ " + " ".join(cmd).replace(pw, "********"))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    for line in proc.stdout:
        if line.strip():
            ctx.log(line.rstrip().replace(pw, "********"))
    code = proc.wait()
    last = _last_job_line(log_path)
    if code not in (0, 5) or not dump_path.exists():
        return result(FAIL, f"expdp exited {code}: {last or 'no completion line'}")
    return result(PASS if code == 0 else WARN, f"exported with Data Pump VERSION={major}: {last}",
                  dump=str(dump_path), bytes=dump_path.stat().st_size)


def upload(ctx: Ctx) -> dict:
    bucket, key = ctx.state["outputs"]["ExchangeBucket"], ctx.state["dump"]
    path: Path = ctx.state["dump_path"]
    size = path.stat().st_size
    s3 = ctx.client("s3")
    ctx.log(f"s3://{bucket}/{key}  <-  {path}  ({size / 1048576:.0f} MB)")
    sent, marks = [0], set()

    def progress(n):
        sent[0] += n
        pct = min(100, int(sent[0] * 100 / size)) // 10 * 10
        if pct not in marks:
            marks.add(pct)
            ctx.log(f"uploaded {pct}%")

    started = time.monotonic()
    s3.upload_file(str(path), bucket, key, Callback=progress)
    head = s3.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != size:
        return result(FAIL, f"S3 holds {head['ContentLength']} bytes; the dump is {size}")
    ctx.state.update(bucket=bucket, key=key, size=size)
    return result(PASS, f"{size / 1048576:.0f} MB in S3, size verified", bucket=bucket, key=key,
                  seconds=round(time.monotonic() - started))


def _download(ctx: Ctx, key: str, directory: str) -> tuple[bool, str]:
    """S3 -> an RDS directory through S3_INTEGRATION, following the RDS task log."""
    task = ctx.one("SELECT rdsadmin.rdsadmin_s3_tasks.download_from_s3(p_bucket_name => :b, "
                   "p_s3_prefix => :k, p_directory_name => :d) FROM dual",
                   {"b": ctx.state["bucket"], "k": key, "d": directory})
    ctx.log(f"RDS task {task}")
    seen, deadline = 0, time.monotonic() + 1800
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            rows = ctx.sql("SELECT text FROM TABLE(rdsadmin.rds_file_util.read_text_file('BDUMP', :f))",
                           {"f": f"dbtask-{task}.log"}, fetch=True, quiet=True)
        except Exception:  # noqa: BLE001 -- the log appears a moment after the task starts
            continue
        for (text,) in rows[seen:]:
            ctx.log(text)
        seen = len(rows)
        joined = " ".join(r[0] or "" for r in rows).lower()
        if "finished successfully" in joined:
            return True, rows[-1][0]
        if "task failed" in joined or " error" in joined:
            return False, rows[-1][0]
    return False, "timed out after 30 minutes"


def stage_dump(ctx: Ctx) -> dict:
    ok, last = _download(ctx, ctx.state["key"], "DATA_PUMP_DIR")
    if not ok:
        return result(FAIL, f"S3 -> DATA_PUMP_DIR failed: {last}")
    size = ctx.one("SELECT filesize FROM TABLE(rdsadmin.rds_file_util.listdir('DATA_PUMP_DIR')) "
                   "WHERE filename = :f", {"f": ctx.state["dump"]})
    if size != ctx.state["size"]:
        return result(FAIL, f"DATA_PUMP_DIR holds {size} bytes; the export was {ctx.state['size']}")
    return result(PASS, f"{ctx.state['dump']} is in DATA_PUMP_DIR on the target, {size} bytes -- "
                        "identical to the export", bytes=size)


# --------------------------------------------------------------------------- schema and load

def prepare_schema(ctx: Ctx) -> dict:
    """Create the owner the dump expects. A schema export run by its owner carries
    no CREATE USER, so the target needs the user -- with exactly the privileges
    discovery recorded on the source -- before the import."""
    owner = q(ctx.estate)
    user = next((u for u in ctx.data("users") if u["username"] == owner), None)
    if not user:
        return result(STOP, f"discovery has no user row for {owner}")
    privs = [p["privilege"] for p in ctx.data("system_privileges") if p["grantee"] == owner]
    roles = [r["granted_role"] for r in ctx.data("role_privileges") if r["grantee"] == owner]
    known_roles = {(r.get("role") or r.get("role_name")): r for r in ctx.data("roles")}
    notes, failures = [], []

    if ctx.one("SELECT COUNT(*) FROM dba_users WHERE username = :u", {"u": owner}):
        notes.append(f"{owner} already exists on the target; reused")
    else:
        pname = f"/dbshift/{ctx.plan['stack_name']}/{owner.lower()}-password"
        ensure_password(ctx.client("ssm"), pname, [{"Key": "project", "Value": "dbshift"}])
        pw = ctx.client("ssm").get_parameter(Name=pname, WithDecryption=True)["Parameter"]["Value"]
        have = {r[0] for r in ctx.sql("SELECT tablespace_name FROM dba_tablespaces", fetch=True, quiet=True)}
        dflt = user["default_tablespace"] if user["default_tablespace"] in have else "USERS"
        temp = user["temporary_tablespace"] if user["temporary_tablespace"] in have else "TEMP"
        clause = f"DEFAULT TABLESPACE {q(dflt)} TEMPORARY TABLESPACE {q(temp)} QUOTA UNLIMITED ON {q(dflt)}"
        ctx.sql(f'CREATE USER {owner} IDENTIFIED BY "{pw}" {clause}',
                show=f'CREATE USER {owner} IDENTIFIED BY "********" {clause}')
        notes.append(f"password generated into SSM {pname}; it is never shown")

    for role in roles:
        r = q(role)
        if not ctx.one("SELECT COUNT(*) FROM dba_roles WHERE role = :r", {"r": r}, quiet=True):
            meta = known_roles.get(r) or {}
            if meta.get("oracle_maintained") == "N":
                ctx.sql(f"CREATE ROLE {r}")
                notes.append(f"created role {r} -- custom on the source; its object grants arrive with the import")
            else:
                failures.append(f"role {r} is not on the target and is not a custom role, so it is not faked")
                continue
        try:
            ctx.sql(f"GRANT {r} TO {owner}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"GRANT {r}: {str(exc).splitlines()[0]}")

    # Custom roles that are not granted TO the owner but receive grants FROM its
    # objects. They are not in role_privileges, so the first run missed one --
    # DBMIG_READ_ROLE -- and all three of its SELECT grants failed on import with
    # ORA-01917. Create them before the import so the object grants land.
    custom = {(r.get("role") or r.get("role_name")) for r in ctx.data("roles") if r.get("oracle_maintained") == "N"}
    receiving = sorted({g["grantee"] for g in ctx.data("table_privileges")
                        if g.get("owner") == owner and g["grantee"] in custom} - set(roles))
    for role in receiving:
        r = q(role)
        if not ctx.one("SELECT COUNT(*) FROM dba_roles WHERE role = :r", {"r": r}, quiet=True):
            ctx.sql(f"CREATE ROLE {r}")
            notes.append(f"created role {r} -- not granted to {owner}, but it receives grants on "
                         f"{owner}'s objects, which fail on import without it")

    for p in privs:
        if not re.match(r"^[A-Z ]+$", p):
            failures.append(f"skipped unexpected privilege text {p!r}")
            continue
        try:
            ctx.sql(f"GRANT {p} TO {owner}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"GRANT {p}: {str(exc).splitlines()[0]}")

    if user.get("profile") not in (None, "DEFAULT"):
        notes.append(f"profile {user['profile']} NOT recreated -- the target starts on DEFAULT. SEC-006 "
                     "flagged the source profile, so copying it is the security owner's decision.")
    for n in notes:
        ctx.log(n)
    for f in failures:
        ctx.log("NOT APPLIED: " + f)
    return result(WARN if failures else PASS,
                  f"{owner} ready with {len(privs)} system privilege(s) and {len(roles)} role(s) from discovery"
                  + (f"; {len(failures)} could not be applied" if failures else ""),
                  notes=notes, failures=failures)


def external_files(ctx: Ctx) -> dict:
    """RDS-004, resolved: the external table's file moves to an RDS directory of
    the same name, so the imported table definition works unchanged."""
    owner = q(ctx.estate)
    if not ctx.opts.collector_password:
        return result(STOP, "finding the external table's file needs the read-only collector login",
                      remedy="Connect in the console, or set DBSHIFT_COLLECTOR_PASSWORD.")
    paths = {d["directory_name"]: d["directory_path"] for d in ctx.data("directories")}
    cur = ctx.source().cursor()
    cur.execute("SELECT table_name, directory_name, location FROM dba_external_locations WHERE owner = :o",
                {"o": owner})
    moved, s3 = [], ctx.client("s3")
    for table, directory, location in cur.fetchall():
        d = q(directory)
        if not re.match(r"^[\w.\-]+$", location or ""):
            return result(FAIL, f"refusing unexpected file name {location!r} for {table}")
        ctx.log(f"{table} reads {location} from {directory}")
        if not ctx.one("SELECT COUNT(*) FROM dba_directories WHERE directory_name = :d", {"d": d}, quiet=True):
            ctx.sql("BEGIN rdsadmin.rdsadmin_util.create_directory(p_directory_name => :d); END;", {"d": d})
        local = Path(paths[directory]) / location
        s3.upload_file(str(local), ctx.state["bucket"], location)
        ctx.log(f"s3://{ctx.state['bucket']}/{location}  <-  {local}")
        ok, last = _download(ctx, location, d)
        if not ok:
            return result(FAIL, f"{location} did not reach {d}: {last}")
        try:
            ctx.sql(f"GRANT READ, WRITE ON DIRECTORY {d} TO {owner}")
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"NOT APPLIED: GRANT on {d}: {str(exc).splitlines()[0]}")
        moved.append({"table": table, "directory": d, "file": location})
    return result(PASS, f"{len(moved)} external file(s) now on the target in a directory of the same "
                        "name, so the imported definition needs no change", moved=moved)


def import_schema(ctx: Ctx) -> dict:
    owner, dump = q(ctx.estate), ctx.state["dump"]
    if not re.match(r"^[\w.]+$", dump):
        return result(FAIL, f"refusing unexpected dump name {dump!r}")
    job = f"DBSHIFT_IMP_{int(time.time())}"
    logname = f"{owner.lower()}_import.log"
    ctx.sql(f"""DECLARE h NUMBER; BEGIN
  h := DBMS_DATAPUMP.OPEN(operation => 'IMPORT', job_mode => 'SCHEMA', job_name => '{job}');
  DBMS_DATAPUMP.ADD_FILE(handle => h, filename => '{dump}', directory => 'DATA_PUMP_DIR',
                         filetype => DBMS_DATAPUMP.KU$_FILE_TYPE_DUMP_FILE);
  DBMS_DATAPUMP.ADD_FILE(handle => h, filename => '{logname}', directory => 'DATA_PUMP_DIR',
                         filetype => DBMS_DATAPUMP.KU$_FILE_TYPE_LOG_FILE, reusefile => 1);
  DBMS_DATAPUMP.METADATA_FILTER(handle => h, name => 'SCHEMA_EXPR', value => 'IN (''{owner}'')');
  DBMS_DATAPUMP.SET_PARAMETER(handle => h, name => 'TABLE_EXISTS_ACTION', value => 'REPLACE');
  DBMS_DATAPUMP.START_JOB(handle => h);
  DBMS_DATAPUMP.DETACH(handle => h);
END;""")
    ctx.log(f"Data Pump job {job} started on the target; following {logname}")
    seen, deadline, lines = 0, time.monotonic() + 3600, []
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            lines = [r[0] or "" for r in ctx.sql(
                "SELECT text FROM TABLE(rdsadmin.rds_file_util.read_text_file('DATA_PUMP_DIR', :f))",
                {"f": logname}, fetch=True, quiet=True)]
        except Exception:  # noqa: BLE001 -- the log file appears once the job starts writing
            lines = lines
        for text in lines[seen:]:
            ctx.log(text)
        seen = len(lines)
        state = ctx.one("SELECT state FROM dba_datapump_jobs WHERE job_name = :j", {"j": job}, quiet=True)
        if state is None or state in ("NOT RUNNING", "COMPLETED"):
            if any(ln.startswith("Job ") for ln in lines):
                break
    final = next((ln for ln in reversed(lines) if ln.startswith("Job ")), None)
    errors = [ln for ln in lines if ln.startswith("ORA-")]
    if not final:
        return result(FAIL, f"no completion line from job {job}", log_tail=lines[-20:])
    status = PASS if "successfully completed" in final else WARN
    return result(status, final, errors=errors[:40], error_count=len(errors), job=job)


GRANT_SQL = re.compile(r'^GRANT ([A-Z ,]+) ON "([A-Z0-9_$#]+)"\."([A-Z0-9_$#]+)" TO "([A-Z0-9_$#]+)"$')
TEXT_INDEX_DDL = re.compile(
    r'^CREATE INDEX "?[A-Z0-9_$#]+"?\."?[A-Z0-9_$#]+"? ON "?[A-Z0-9_$#]+"?\."?[A-Z0-9_$#]+"? '
    r'\("?[A-Z0-9_$#]+"?\) INDEXTYPE IS "?CTXSYS"?\."?CONTEXT"?( PARAMETERS \(.*\))?( PARALLEL \d+)?$',
    re.IGNORECASE)
COMPILE_SQL = {"SYNONYM": "ALTER SYNONYM {o}.{n} COMPILE", "VIEW": "ALTER VIEW {o}.{n} COMPILE",
               "PROCEDURE": "ALTER PROCEDURE {o}.{n} COMPILE", "FUNCTION": "ALTER FUNCTION {o}.{n} COMPILE",
               "PACKAGE": "ALTER PACKAGE {o}.{n} COMPILE", "PACKAGE BODY": "ALTER PACKAGE {o}.{n} COMPILE BODY",
               "TRIGGER": "ALTER TRIGGER {o}.{n} COMPILE", "TYPE": "ALTER TYPE {o}.{n} COMPILE",
               "MATERIALIZED VIEW": "ALTER MATERIALIZED VIEW {o}.{n} COMPILE"}


def _import_issues(lines: list[str]) -> list[dict]:
    """Every object a Data Pump log reports as failed or compiled with warnings:
    its type and name, the errors, and the SQL Data Pump tried to run."""
    issues, i = [], 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r'ORA-39083: Object type ([A-Z_]+)(?::"([^"]+)"\."([^"]+)")? failed to create', ln)
        if m:
            errors, sql, j = [], [], i + 1
            while j < len(lines) and not lines[j].startswith("Failing sql is:"):
                if lines[j].strip():
                    errors.append(lines[j].strip())
                j += 1
            j += 1
            while j < len(lines) and lines[j].strip() and not lines[j].startswith(("ORA-", "Processing", "Job ")):
                sql.append(lines[j].strip())
                j += 1
            issues.append({"kind": m.group(1), "owner": m.group(2), "name": m.group(3),
                           "errors": errors, "sql": " ".join(sql)})
            i = j
            continue
        m = re.match(r'ORA-39082: Object type ([A-Z_ ]+):"([^"]+)"\."([^"]+)" created with compilation warnings', ln)
        if m:
            issues.append({"kind": m.group(1), "owner": m.group(2), "name": m.group(3),
                           "errors": [ln], "sql": "", "warning": True})
        i += 1
    return issues


def _source_ddl(ctx: Ctx, kind: str, name: str, owner: str) -> str:
    cur = ctx.source().cursor()
    cur.execute("SELECT DBMS_METADATA.GET_DDL(:k, :n, :o) FROM dual", {"k": kind, "n": name, "o": owner})
    val = cur.fetchone()[0]
    return " ".join((val.read() if hasattr(val, "read") else val).split()).rstrip(";").strip()


def repair(ctx: Ctx) -> dict:
    """Triage the import log. Data Pump reports a failure and carries on, and a
    'completed with 17 errors' says nothing about which of them matter. Each is
    classified by rule: repaired where the fix is certain, explained where the
    failure is correct, and left for a person otherwise."""
    owner = q(ctx.estate)
    logname = f"{owner.lower()}_import.log"
    lines = [r[0] or "" for r in ctx.sql(
        "SELECT text FROM TABLE(rdsadmin.rds_file_util.read_text_file('DATA_PUMP_DIR', :f))",
        {"f": logname}, fetch=True, quiet=True)]
    issues = _import_issues(lines)
    ctx.log(f"{len(issues)} object(s) reported in {logname}")
    source_invalid = {(o.get("object_type"), o.get("object_name")) for o in ctx.data("invalid_objects")
                      if o.get("owner") == owner}
    custom = {(r.get("role") or r.get("role_name")) for r in ctx.data("roles") if r.get("oracle_maintained") == "N"}
    collector = (ctx.opts.collector_user or "").upper()
    repaired, expected, left = [], [], []

    for it in issues:
        label = f"{it['kind']} {it['name'] or ''}".strip()
        errors = " ".join(it["errors"])
        if it.get("warning"):
            if (it["kind"], it["name"]) in source_invalid:
                expected.append(f"{label}: invalid on the source too -- reproduced, not introduced")
            else:
                left.append(f"{label}: compiled with warnings, but was valid on the source")
            continue

        if it["kind"] == "OBJECT_GRANT" and "ORA-01917" in errors:
            m = GRANT_SQL.match(it["sql"])
            if not m:
                left.append(f"unrecognised grant: {it['sql'][:100]}")
                continue
            grantee = m.group(4)
            if grantee == collector:
                expected.append(f"{it['sql']} -- the read-only discovery account exists only on the source")
                continue
            exists = (ctx.one("SELECT COUNT(*) FROM dba_roles WHERE role = :r", {"r": grantee}, quiet=True)
                      or ctx.one("SELECT COUNT(*) FROM dba_users WHERE username = :u", {"u": grantee}, quiet=True))
            if not exists:
                if grantee not in custom:
                    left.append(f"{it['sql']} -- {grantee} is not on the target and is not a custom role")
                    continue
                ctx.sql(f"CREATE ROLE {q(grantee)}")
            ctx.sql(it["sql"])
            repaired.append(f"{it['sql']} -- re-applied once {grantee} existed")
            continue

        if it["kind"] == "INDEX" and "PLS-00306" in errors and "ctxsys.driimp.create_index" in it["sql"].lower():
            ddl = _source_ddl(ctx, "INDEX", it["name"], it["owner"])
            if not TEXT_INDEX_DDL.match(ddl):
                left.append(f"{label}: source DDL is not a plain CONTEXT index -- {ddl[:120]}")
                continue
            ctx.log(f"{label}: the dump carries 21c's internal rebuild call, which 19c's CTXSYS rejects "
                    "(PLS-00306). Rebuilding natively from the source's own DDL, as the owner.")
            started = time.monotonic()
            ctx.owner_sql(ddl)
            repaired.append(f"{label}: rebuilt natively on 19c from the source DDL in "
                            f"{time.monotonic() - started:.0f} s (the 21c import call was rejected)")
            continue

        left.append(f"{label}: {errors[:180]}")

    # Objects invalid on the target but valid on the source: compile them as their owner.
    invalid = [tuple(r) for r in ctx.sql("SELECT object_type, object_name FROM dba_objects WHERE owner = :o "
                                         "AND status <> 'VALID'", {"o": owner}, fetch=True, quiet=True)]
    for kind, name in invalid:
        if (kind, name) in source_invalid or kind not in COMPILE_SQL or not IDENT.match(name):
            continue
        try:
            ctx.owner_sql(COMPILE_SQL[kind].format(o=owner, n=name))
        except Exception as exc:  # noqa: BLE001 -- the recheck below reports it
            ctx.log(f"compile {kind} {name}: {str(exc).splitlines()[0]}")
    still = {tuple(r) for r in ctx.sql("SELECT object_type, object_name FROM dba_objects WHERE owner = :o "
                                       "AND status <> 'VALID'", {"o": owner}, fetch=True, quiet=True)}
    for kind, name in invalid:
        if (kind, name) in source_invalid:
            continue
        if (kind, name) in still:
            left.append(f"{kind} {name}: still invalid after compiling")
        else:
            repaired.append(f"{kind} {name}: invalid after import, valid after compiling")

    for r in repaired:
        ctx.log("REPAIRED  " + r)
    for e in expected:
        ctx.log("EXPECTED  " + e)
    for x in left:
        ctx.log("FOR A PERSON  " + x)
    return result(PASS if not left else WARN,
                  f"{len(issues)} import issue(s) and {len(invalid)} invalid object(s): {len(repaired)} repaired, "
                  f"{len(expected)} expected and explained, {len(left)} left for a person",
                  repaired=repaired, expected=expected, left_for_a_person=left)


def runbook(ctx: Ctx) -> dict:
    """Phase 4's target-side steps for this phase, in rule order (the job is
    disabled before the materialized view is refreshed). DMS settings are listed
    as not applicable: this run uses Data Pump."""
    ran, failed, not_applicable = [], [], []
    entries = sorted(ctx.records["remediation"].get("entries", []), key=lambda e: e["rule_id"])
    for e in entries:
        art = e.get("artefact") or {}
        if art.get("type") == "target_runbook":
            for step in art.get("steps", []):
                if step.get("applies_to_phase") != "migrate":
                    continue
                sql = step.get("sql_on_target", "")
                ctx.log(f"{e['rule_id']} [{e.get('source')}] {step.get('when')}")
                if not RUNBOOK_ALLOWED.match(sql):
                    failed.append(f"{e['rule_id']}: refused -- only DBMS_SCHEDULER / DBMS_MVIEW calls may run from the plan")
                    continue
                try:
                    ctx.sql(sql)
                    ran.append(f"{e['rule_id']} {e.get('object_name')}")
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{e['rule_id']}: {str(exc).splitlines()[0]}")
        elif art.get("applies_to_phase") == "migrate" and str(art.get("type", "")).startswith("dms_"):
            not_applicable.append(f"{e['rule_id']} {e.get('object_name')} ({art['type']})")
    if not_applicable:
        ctx.log(f"{len(not_applicable)} DMS setting(s) recorded, not applicable -- this run uses Data Pump")
    for f in failed:
        ctx.log("FAILED: " + f)
    return result(WARN if failed else PASS,
                  f"{len(ran)} runbook step(s) applied" + (f", {len(failed)} failed" if failed else ""),
                  ran=ran, failed=failed, not_applicable=not_applicable)


def reconcile(ctx: Ctx) -> dict:
    """A first count, not validation -- that is Phase 8's job. Objects by type from
    discovery against the target, invalid objects, and exact row counts."""
    owner = q(ctx.estate)
    # Oracle Text keeps its index in DR$ tables whose names and number depend on
    # the engine version (21c has $B, $C, $Q that 19c does not). They are
    # counted separately rather than reported as missing data.
    internal = lambda name: str(name).startswith("DR$")  # noqa: E731
    source_types = Counter(o["object_type"] for o in ctx.data("objects")
                           if o.get("owner") == owner and not internal(o["object_name"]))
    target_types = dict(ctx.sql("SELECT object_type, COUNT(*) FROM dba_objects WHERE owner = :o "
                                "AND object_name NOT LIKE 'DR$%' GROUP BY object_type", {"o": owner}, fetch=True))
    text_src = sum(1 for o in ctx.data("objects") if o.get("owner") == owner and internal(o["object_name"]))
    text_tgt = ctx.one("SELECT COUNT(*) FROM dba_objects WHERE owner = :o AND object_name LIKE 'DR$%'",
                       {"o": owner}, quiet=True)
    ctx.log(f"Oracle Text internals (version-specific names): source {text_src}, target {text_tgt}")
    by_type = {t: {"source": source_types.get(t, 0), "target": target_types.get(t, 0)}
               for t in sorted(set(source_types) | set(target_types))}
    for t, v in by_type.items():
        if v["source"] != v["target"]:
            ctx.log(f"{t:<20} source {v['source']:>4}  target {v['target']:>4}")
    invalid = ctx.sql("SELECT object_type, object_name FROM dba_objects WHERE owner = :o "
                      "AND status <> 'VALID' ORDER BY 1, 2", {"o": owner}, fetch=True, quiet=True)
    for t, n in invalid:
        ctx.log(f"INVALID on target: {t} {n}")

    rows, mismatched = {}, []
    tables = sorted({t["table_name"] for t in ctx.data("tables") if t.get("owner") == owner})
    ext = {e["table_name"] for e in ctx.data("external_tables") if e["owner"] == owner}
    for table in tables:
        if not IDENT.match(table):
            continue
        try:
            tgt = ctx.one(f'SELECT COUNT(*) FROM "{owner}"."{table}"', quiet=True)
        except Exception as exc:  # noqa: BLE001
            tgt = f"error: {str(exc).splitlines()[0]}"
        src = None
        if ctx.opts.collector_password:
            try:
                c = ctx.source().cursor()
                c.execute(f'SELECT COUNT(*) FROM "{owner}"."{table}"')
                src = c.fetchone()[0]
            except Exception as exc:  # noqa: BLE001
                src = f"error: {str(exc).splitlines()[0]}"
        rows[table] = {"source": src, "target": tgt, "external": table in ext}
        # A source count the read-only account could not take is "not comparable",
        # not a mismatch -- claiming either would be a guess.
        if isinstance(src, int) and isinstance(tgt, int) and src != tgt:
            mismatched.append(table)
            ctx.log(f"ROWS {table}: source {src}  target {tgt}")
        elif not isinstance(src, int):
            ctx.log(f"ROWS {table}: target {tgt}; source not comparable ({src})")
    comparable = sum(1 for v in rows.values() if isinstance(v["source"], int) and isinstance(v["target"], int))
    source_invalid = {(o.get("object_type"), o.get("object_name")) for o in ctx.data("invalid_objects")
                      if o.get("owner") == owner}
    new_invalid = [(t, n) for t, n in invalid if (t, n) not in source_invalid]
    same_types = all(v["source"] == v["target"] for v in by_type.values())
    status = PASS if (same_types and not mismatched and not new_invalid) else WARN
    return result(status,
                  f"{sum(v['target'] for v in by_type.values())} objects on the target (plus {text_tgt} Oracle "
                  f"Text internals); {comparable - len(mismatched)} of {comparable} comparable tables match on "
                  f"rows; {len(new_invalid)} object(s) invalid that were valid on the source. "
                  "Full validation is Phase 8.",
                  by_type=by_type, rows=rows, invalid=[f"{t} {n}" for t, n in invalid],
                  text_internals={"source": text_src, "target": text_tgt})


# --------------------------------------------------------------------------- the order

@dataclass(frozen=True)
class Step:
    id: str
    title: str
    why: str
    decided_by: str
    touches: str
    reversible: str
    fn: Callable
    when: Callable | None = None


STEPS = [
    Step("records", "Records agree", "Every record must describe the same collector run before anything moves.",
         "rule", "nothing", "nothing to undo", check_records),
    Step("target", "Target is up and correct",
         "The instance must exist, be built from these records, and match its render: version, character "
         "sets, S3 role.", "rule", "nothing (read-only login)", "nothing to undo", check_target),
    Step("gate", "Blocker gate allows a full load",
         "Phase 5 decides which blockers stand in front of loading data. Change data capture stays off while "
         "its blockers are open.", "gate", "nothing", "nothing to undo", check_gate),
    Step("export", "Export the source for 19c",
         "RDS offers 19c only, so the dump must be written with Data Pump VERSION=19. Run as the schema "
         "owner, not a DBA -- least privilege.", "rule", "source: writes a dump file, reads the schema",
         "delete the dump file", export),
    Step("upload", "Upload the dump to S3",
         "RDS has no server disk to copy to; S3 is the only way in, which is why Phase 6 built a bucket.",
         "orchestrator", "AWS S3: one object (expires in 7 days)", "delete the object", upload),
    Step("stage", "Bring the dump into RDS",
         "S3_INTEGRATION copies it into DATA_PUMP_DIR on the instance; the size is checked against the export.",
         "orchestrator", "target: one file in DATA_PUMP_DIR", "delete the file", stage_dump),
    Step("schema", "Create the schema owner",
         "A dump written by its owner carries no CREATE USER, so the owner is created with exactly the "
         "privileges and roles discovery recorded. The flagged profile is not copied.",
         "rule", "target: one user, grants, a custom role", "drop the user", prepare_schema),
    Step("external", "Resolve RDS-004: move the external file",
         "The file moves to an RDS directory of the same name, so the imported table works unchanged.",
         "approval", "AWS S3 and one target directory", "drop the directory",
         external_files, when=lambda ctx: ctx.opts.resolve_external_via_s3),
    Step("import", "Import schema and data",
         "Data Pump loads tables, then rows, then builds indexes and constraints, then code -- the order "
         "the architecture asks for, so foreign keys are not checked row by row during the load.",
         "orchestrator", "target: the whole schema", "drop the user cascade and re-import", import_schema),
    Step("repair", "Triage the import log and repair",
         "Data Pump reports a failure and carries on. Every failure is classified by rule: repaired where the "
         "fix is certain, explained where failing is correct, left for a person otherwise.",
         "rule", "target: re-applied grants, a rebuilt index, recompiled objects",
         "drop what was re-created", repair),
    Step("runbook", "Apply Phase 4's target steps",
         "Disable the scheduler job before it runs against a half-built schema, then give the materialized "
         "view its first complete refresh.", "phase4_advice", "target: one job, one materialized view",
         "re-enable the job", runbook),
    Step("reconcile", "First count", "Objects by type and exact row counts, source against target. "
         "Validation proper is Phase 8.", "rule", "nothing (read-only)", "nothing to undo", reconcile),
]
