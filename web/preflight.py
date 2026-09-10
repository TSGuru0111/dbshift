"""Connection preflight.

Answers "can we actually run a discovery against this database, and if not, what
exactly is missing" — before a run starts, rather than failing on query 40 of 66.

Each check returns pass / warn / fail with a remedy, because at a client site the
useful output is not "it failed" but "ask the DBA for X".
"""

from __future__ import annotations

import socket
import time

import oracledb

CATALOG_PROBE = "SELECT COUNT(*) FROM dba_objects WHERE ROWNUM < 2"

# Pick a table the collector would actually profile. Testing an arbitrary table
# lands on an Oracle-managed internal (AQ$, DR$, MLOG$) that the grant script
# deliberately excludes, and reports "no data access" to someone who has it.
DATA_PROBE_OWNER = """
    SELECT owner, table_name FROM dba_tables
    WHERE owner = :o
      AND table_name NOT LIKE 'DR$%' AND table_name NOT LIKE 'AQ$%'
      AND table_name NOT LIKE 'MLOG$%' AND table_name NOT LIKE 'RUPD$%'
      AND table_name NOT LIKE 'SYS_IOT%'
      AND ROWNUM < 2
"""


def _check(name, status, detail, remedy=None):
    return {"name": name, "status": status, "detail": detail, "remedy": remedy}


def parse_dsn(dsn: str) -> tuple[str, int]:
    """`host:port/service` -> (host, port). Defaults to 1521 when omitted."""
    hostport = dsn.split("/", 1)[0]
    if ":" in hostport:
        host, _, port = hostport.partition(":")
        try:
            return host, int(port)
        except ValueError:
            return host, 1521
    return hostport, 1521


def run(dsn: str, user: str, password: str, schema: str = "") -> dict:
    """`schema` may be blank, one name, or a comma-separated list.

    Blank means "work it out" -- the schemas actually present are discovered from
    the database rather than assumed, so this runs against any estate and not
    just the one it was written against.
    """
    checks: list[dict] = []
    facts: dict = {}
    host, port = parse_dsn(dsn)
    requested = [s.strip().upper() for s in (schema or "").split(",") if s.strip()]

    # 1 -- TCP reachability. Distinguishes "wrong password" from "cannot get there
    # at all", which at a client site is the difference between a DBA ticket and a
    # network ticket.
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=5):
            latency = int((time.perf_counter() - started) * 1000)
        checks.append(_check("Network reachable", "pass", f"{host}:{port} answered in {latency} ms"))
    except OSError as exc:
        checks.append(
            _check(
                "Network reachable",
                "fail",
                f"cannot open a TCP connection to {host}:{port} — {exc.__class__.__name__}",
                f"Open outbound TCP {port} to {host}. On a client network this usually "
                "means a firewall rule or a VPN, and it is the first thing to request.",
            )
        )
        return {"ok": False, "checks": checks, "facts": facts}

    # 2 -- Authentication.
    try:
        conn = oracledb.connect(user=user, password=password, dsn=dsn)
    except oracledb.Error as exc:
        message = str(exc).splitlines()[0]
        remedy = "Check the username and password."
        if "ORA-12514" in message or "ORA-12541" in message:
            remedy = (
                "The listener answered but does not know this service name. Confirm the "
                "service (not the SID) — for XE that is XEPDB1, not XE."
            )
        elif "ORA-01017" in message:
            remedy = "Credentials rejected. Confirm the account and password with the DBA."
        elif "ORA-28000" in message:
            remedy = "The account is locked. Ask the DBA to unlock it."
        checks.append(_check("Authentication", "fail", message, remedy))
        return {"ok": False, "checks": checks, "facts": facts}

    checks.append(_check("Authentication", "pass", f"connected as {user}"))

    try:
        cur = conn.cursor()

        # 3 -- Identity and container.
        cur.execute(
            """SELECT sys_context('USERENV','CON_NAME'),
                      sys_context('USERENV','DB_NAME'),
                      (SELECT banner_full FROM v$version WHERE ROWNUM < 2)
               FROM dual"""
        )
        con_name, db_name, banner = cur.fetchone()
        facts.update({"container": con_name, "db_name": db_name, "version": banner})
        if con_name == "CDB$ROOT":
            checks.append(
                _check(
                    "Container",
                    "fail",
                    "connected to CDB$ROOT, the container root",
                    "Connect to the pluggable database service instead. Application objects "
                    "do not live in the root and discovery will find nothing useful.",
                )
            )
        else:
            checks.append(_check("Container", "pass", f"{con_name}"))

        # 4 -- Catalogue access. Everything in discovery depends on this.
        try:
            cur.execute(CATALOG_PROBE)
            cur.fetchone()
            checks.append(_check("Catalogue access", "pass", "DBA_* views readable"))
        except oracledb.Error:
            checks.append(
                _check(
                    "Catalogue access",
                    "fail",
                    "cannot read DBA_* views",
                    "Grant SELECT_CATALOG_ROLE to this account. Without it discovery "
                    "cannot inventory anything.",
                )
            )

        # 4b -- Which schemas are actually here. Oracle flags its own with
        # ORACLE_MAINTAINED='Y', so anything else that owns objects is an
        # application schema. Discovering this is what makes the tool portable.
        discovered: list[dict] = []
        try:
            cur.execute(
                """SELECT u.username, COUNT(o.object_name)
                   FROM dba_users u
                   LEFT JOIN dba_objects o ON o.owner = u.username
                   WHERE u.oracle_maintained = 'N'
                   GROUP BY u.username
                   ORDER BY COUNT(o.object_name) DESC, u.username"""
            )
            discovered = [{"schema": r[0], "objects": r[1]} for r in cur.fetchall()]
        except oracledb.Error:
            discovered = []

        owning = [d for d in discovered if d["objects"] > 0]
        facts["schemas_discovered"] = discovered
        selected = requested or [d["schema"] for d in owning]
        facts["schemas_selected"] = selected

        if not selected:
            checks.append(
                _check(
                    "Application schemas",
                    "fail",
                    "no non-Oracle schema owns any object in this database",
                    "Name the schema to inspect explicitly, or confirm you are connected "
                    "to the right database — an empty estate has nothing to migrate.",
                )
            )
        else:
            missing = [s for s in requested if s not in {d["schema"] for d in discovered}]
            summary = ", ".join(
                f"{d['schema']} ({d['objects']})" for d in owning[:6]
            ) or ", ".join(selected)
            if missing:
                checks.append(
                    _check(
                        "Application schemas",
                        "warn",
                        f"requested {', '.join(missing)} — not present. Found: {summary}",
                        "Check the spelling, or leave the schema field blank to use every "
                        "application schema found.",
                    )
                )
            else:
                checks.append(
                    _check(
                        "Application schemas",
                        "pass",
                        f"{len(selected)} selected — {summary}"
                        + ("" if requested else "  (auto-discovered)"),
                    )
                )

        # 5 -- Row-level read. Metadata access does not imply data access; this is
        # the exact gap that made data-quality profiling silently impossible.
        schema = selected[0] if selected else (requested[0] if requested else "")
        data_ok, sample, data_error = False, None, None
        try:
            cur.execute(DATA_PROBE_OWNER, {"o": schema})
            row = cur.fetchone()
            sample = row[1] if row else None
        except oracledb.Error as exc:
            data_error = str(exc).splitlines()[0]
        if sample:
            try:
                cur.execute(f'SELECT COUNT(*) FROM {schema}."{sample}" WHERE ROWNUM < 2')
                cur.fetchone()
                data_ok = True
            except oracledb.Error as exc:
                data_error = str(exc).splitlines()[0]
        elif not data_error:
            data_error = f"no non-internal tables found in {schema}"

        if data_ok:
            checks.append(
                _check("Row data access", "pass", f"can read {schema} tables (sampled {sample})")
            )
        else:
            # Report the actual error. Assuming ORA-00942 here once hid a
            # completely different cause.
            checks.append(
                _check(
                    "Row data access",
                    "warn",
                    f"catalogue is readable but {schema} rows are not — {data_error}",
                    "Discovery and structural rules still work. Data-quality profiling "
                    "(duplicates, encoding) is skipped. If this is a grant problem, see "
                    "scripts/oracle-source/05_grant_collector_read.sql.",
                )
            )

        # 6 -- CDC readiness. Not needed to discover, decisive for how you migrate.
        try:
            cur.execute("SELECT log_mode, supplemental_log_data_min FROM v$database")
            log_mode, supp = cur.fetchone()
            facts.update({"log_mode": log_mode, "supplemental_logging": supp})
            if log_mode == "ARCHIVELOG" and supp in ("YES", "IMPLICIT"):
                checks.append(_check("CDC readiness", "pass", f"{log_mode}, supplemental logging {supp}"))
            else:
                checks.append(
                    _check(
                        "CDC readiness",
                        "warn",
                        f"{log_mode}, supplemental logging {supp}",
                        "DMS change data capture needs ARCHIVELOG plus supplemental logging. "
                        "Without both, only a full-outage load is possible. Enabling "
                        "ARCHIVELOG requires a database restart, so it needs a window.",
                    )
                )
        except oracledb.Error:
            checks.append(_check("CDC readiness", "warn", "v$database not readable", None))

        # 7 -- Estate size, so the operator knows what they are about to scan.
        # Across every selected schema, not just the first.
        if selected:
            binds = {f"s{i}": s for i, s in enumerate(selected)}
            placeholders = ", ".join(f":{k}" for k in binds)
            try:
                cur.execute(
                    "SELECT COUNT(*), ROUND(NVL(SUM(bytes),0)/1073741824, 2) "
                    f"FROM dba_segments WHERE owner IN ({placeholders})",
                    binds,
                )
                segs, gb = cur.fetchone()
                facts.update({"segments": segs, "size_gb": float(gb or 0)})
            except oracledb.Error:
                pass

        cur.close()
    finally:
        conn.close()

    blocking = [c for c in checks if c["status"] == "fail"]
    return {"ok": not blocking, "checks": checks, "facts": facts}


NETWORK_REQUIREMENTS = [
    {
        "title": "Outbound TCP to the database listener",
        "detail": "Default 1521. From the machine running the collector to the database host. "
        "This is the one that blocks everything else.",
    },
    {
        "title": "A read-only database account",
        "detail": "SELECT_CATALOG_ROLE plus CREATE SESSION covers discovery and every "
        "structural rule. Explicit SELECT on application tables additionally enables "
        "data-quality profiling.",
    },
    {
        "title": "VPN or private link, if the database is not internet-facing",
        "detail": "Most client databases are not. The collector runs inside their network "
        "and pushes results outward — nothing needs to connect inbound to it.",
    },
    {
        "title": "No inbound firewall hole",
        "detail": "Deliberately. Security teams refuse them, and the collector never needs one.",
    },
]
