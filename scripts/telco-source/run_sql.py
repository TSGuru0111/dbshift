"""Run a .sql build script against Oracle without sqlplus.

sqlplus is not installed on this machine (only the Instant-Client-free thin
oracledb driver is available), so the estate build scripts need a runner.

This is a deliberately small SQL*Plus subset -- enough for the build scripts in
this directory and nothing more:

  * statements separated by ";" at end of line
  * PL/SQL blocks and CREATE .. TYPE/PROCEDURE/FUNCTION/PACKAGE/TRIGGER
    terminated by a lone "/" on its own line
  * SET / PROMPT / WHENEVER directives are recognised and skipped
  * SELECTs print their rows, so the verification queries at the end of each
    script still show their output

Errors are printed and the run continues (matching WHENEVER SQLERROR CONTINUE),
then a summary lists every failure. Exit code is non-zero if anything failed,
so a broken build cannot look like a clean one -- this project has already lost
time to partially-applied scripts that reported success.

Usage:
  python run_sql.py <script.sql> --user U --password P [--dsn localhost:1521/XEPDB1]
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import oracledb

# Directives this runner understands and deliberately ignores.
SKIP_PREFIXES = ("SET ", "PROMPT", "WHENEVER ", "SPOOL", "SHOW ")

# Statements that must be terminated by "/" rather than ";" because their body
# legitimately contains semicolons.
PLSQL_START = re.compile(
    r"^\s*(DECLARE|BEGIN|CREATE\s+(OR\s+REPLACE\s+)?"
    r"(TYPE|PROCEDURE|FUNCTION|PACKAGE|TRIGGER)\b)",
    re.IGNORECASE,
)


def split_statements(sql: str) -> list[str]:
    """Split a script into executable statements.

    Tracks PL/SQL blocks so a semicolon inside a procedure body does not end the
    statement early -- the single most likely way for a runner like this to
    silently truncate a script.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_plsql = False

    for raw in sql.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        # A lone "/" terminates whatever is buffered.
        if stripped == "/":
            if buf:
                text = "\n".join(buf).strip()
                # A single-line type body such as
                #   CREATE OR REPLACE TYPE t AS VARRAY(4) OF VARCHAR2(20);
                #   /
                # ends with BOTH ";" and "/". The ";" is SQL*Plus's terminator,
                # not part of the statement, and Oracle rejects it over the
                # wire -- but only strip it when it is the sole trailing one,
                # since a real PL/SQL body legitimately ends "END;".
                if text.endswith(";") and not re.search(
                    r"\bEND\s*\w*\s*;$", text, re.IGNORECASE
                ):
                    text = text[:-1].strip()
                statements.append(text)
                buf = []
            in_plsql = False
            continue

        if not buf:
            if not stripped or stripped.startswith("--"):
                continue
            if stripped.upper().startswith(SKIP_PREFIXES):
                continue
            in_plsql = bool(PLSQL_START.match(stripped))

        buf.append(line)

        # Outside PL/SQL, a trailing semicolon ends the statement.
        if not in_plsql and stripped.endswith(";"):
            text = "\n".join(buf).strip()
            statements.append(text[:-1].strip())
            buf = []

    if buf:
        leftover = "\n".join(buf).strip()
        if leftover:
            statements.append(leftover)
    return statements


def label(stmt: str, width: int = 78) -> str:
    flat = " ".join(stmt.split())
    return flat[:width] + ("..." if len(flat) > width else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a build script against Oracle")
    ap.add_argument("script")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--dsn", default="localhost:1521/XEPDB1")
    ap.add_argument("--stop-on-error", action="store_true")
    args = ap.parse_args(argv)

    with open(args.script, encoding="utf-8") as fh:
        statements = split_statements(fh.read())

    print(f"script     : {args.script}")
    print(f"statements : {len(statements)}")
    print(f"connecting : {args.user}@{args.dsn}\n")

    try:
        conn = oracledb.connect(user=args.user, password=args.password, dsn=args.dsn)
    except oracledb.Error as exc:
        # A failed logon is a routine operator mistake, not a bug in this
        # script. A 20-line driver traceback buries that.
        print(f"connection failed: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 2

    con_name = conn.cursor().execute(
        "select sys_context('USERENV','CON_NAME') from dual"
    ).fetchone()[0]
    print(f"container  : {con_name}")
    if con_name != "XEPDB1":
        print("REFUSING: not connected to XEPDB1. CREATE USER fails in CDB$ROOT.")
        return 2
    print()

    failures: list[tuple[int, str, str]] = []
    started = time.perf_counter()

    for i, stmt in enumerate(statements, 1):
        cur = conn.cursor()
        t0 = time.perf_counter()
        try:
            cur.execute(stmt)
            ms = int((time.perf_counter() - t0) * 1000)
            if cur.description:  # a SELECT -- show what it returned
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                print(f"[{i:3}] OK   {ms:>6} ms  {label(stmt)}")
                print("        " + " | ".join(cols))
                for r in rows[:40]:
                    print("        " + " | ".join("" if v is None else str(v) for v in r))
                if len(rows) > 40:
                    print(f"        ... {len(rows) - 40} more rows")
            else:
                print(f"[{i:3}] OK   {ms:>6} ms  {label(stmt)}")
        except oracledb.Error as exc:
            msg = str(exc).split("\n")[0]
            print(f"[{i:3}] FAIL             {label(stmt)}")
            print(f"        -> {msg}")
            failures.append((i, label(stmt), msg))
            # A dead connection is not a statement-level error: every remaining
            # statement will "fail" for the same reason, burying the one real
            # cause under a wall of noise. Stop and say so.
            if isinstance(exc, oracledb.InterfaceError) or "DPY-1001" in msg:
                print("\nconnection lost -- abandoning the rest of the script")
                break
            if args.stop_on_error:
                break
        finally:
            try:
                cur.close()
            except oracledb.Error:
                pass

    try:
        conn.commit()
        conn.close()
    except oracledb.Error as exc:
        # Losing the connection after the work is done should not turn a
        # reported result into a traceback.
        print(f"note: could not close cleanly ({str(exc).splitlines()[0]})")

    elapsed = time.perf_counter() - started
    print(f"\n{'-' * 70}")
    print(f"executed {len(statements)} statements in {elapsed:.1f}s, {len(failures)} failed")
    for i, text, msg in failures:
        print(f"  [{i}] {text}\n       {msg}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
