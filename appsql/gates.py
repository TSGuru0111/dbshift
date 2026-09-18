"""The five gates a converted application SQL statement passes before approval.

    static -> parity -> parse on the target -> result equivalence -> approval

The shape mirrors `convert/gates.py` deliberately: a reader moving between the
two phases should not be learning a second vocabulary. Two gates differ, and
both differences come from the material rather than from taste.

**There is no policy gate.** A converted stored object is DDL, so 4b checks it
only ever CREATEs itself and never DROPs or GRANTs. An application statement is
DML -- a SELECT stays a SELECT and an UPDATE stays an UPDATE -- so the check
that matters is not "what may this create" but "is this still the same *kind*
of statement". That is folded into `static`: a SELECT that converts into an
UPDATE is a defect no parity check would notice.

**Validation is two gates, not one.** 4b compiles a function and that is the
whole question, because PL/pgSQL compilation resolves declarations. For a query
there are two separate questions, and conflating them is how a converter passes
while being wrong:

    parse   does the target accept this statement at all? Cheap, needs only a
            connection, catches a column that does not exist on the target.
    result  does it return the same rows as the Oracle original, over the same
            data? Expensive, needs both engines and real rows, and is the only
            thing that catches a ROWNUM pagination rewrite that parses
            perfectly and returns a different page.

A statement that parses and returns different rows is the exact failure this
phase exists to prevent, so `result` is a gate in its own right and a `parse`
pass is never reported as validation.

Every gate returns pass, fail, or **blocked** -- and blocked is load-bearing.
With no target configured the parse and result gates report blocked with the
reason, never a silent pass. `sizing/target.py` already reads that distinction
for stored code: converted-but-never-compiled is not the same evidence as
compiled, and it refuses to treat one as the other.
"""

from __future__ import annotations

import re

from . import classify

PASS, FAIL, BLOCKED = "pass", "fail", "blocked"

# The statement kinds MyBatis runs, and the rule that a conversion may not
# change one into another.
KINDS = ("select", "insert", "update", "delete")

_LEADING_KEYWORD = re.compile(r"^\s*(?:with\b.*?\)\s*)?(select|insert|update|delete|merge)\b",
                              re.IGNORECASE | re.DOTALL)


def _gate(name, status, detail, remedy=None):
    return {"gate": name, "status": status, "detail": detail, "remedy": remedy}


def _statement_kind(sql: str) -> str | None:
    """The kind of statement this text is, ignoring a leading CTE or MyBatis tag.

    Two things legitimately precede the statement keyword:

      - A CTE. A CONNECT BY rewrite becomes `WITH RECURSIVE ... SELECT`, so the
        leading keyword is WITH and the statement is still a select.
      - A MyBatis `<selectKey>`, which is a separate statement MyBatis runs
        before or after this one. A correct `createLoan` conversion keeps it,
        and the first live model run was rejected for exactly that -- the gate
        read `<selectKey` as the statement keyword and reported "does not begin
        with a statement keyword" about a perfectly good INSERT.
    """
    text = re.sub(r"<selectKey\b[^>]*>.*?</selectKey>", " ", sql or "",
                  flags=re.DOTALL | re.IGNORECASE)
    # Any other leading tag (<where>, <if>) is structure, not a keyword.
    text = re.sub(r"^\s*(?:<[^>]+>\s*)+", "", text)
    m = _LEADING_KEYWORD.search(text)
    if not m:
        return None
    kw = m.group(1).lower()
    # MERGE is the one Oracle kind MyBatis declares as <update>.
    return "update" if kw == "merge" else kw


# ------------------------------------------------------------------- static


def static_check(conv: dict, stmt: dict) -> dict:
    """The conversion is a statement, of the same kind, and accounts for itself."""
    problems = []
    sql = (conv.get("sql") or "").strip()
    if not sql:
        problems.append("no SQL")

    if sql:
        got = _statement_kind(sql)
        want = stmt["kind"] if stmt["kind"] != "insert" or "merge" not in sql.lower() else "insert"
        if got is None:
            problems.append("the converted text does not begin with a statement keyword")
        elif got != want and not (stmt["kind"] == "update" and got == "update"):
            # A kind change is a correctness defect no parity check would see:
            # a SELECT rewritten as an UPDATE parses, and writes.
            problems.append(f"converts a {stmt['kind']} into a {got}")

    if not isinstance(conv.get("constructs"), list):
        problems.append("no construct accounting")

    # A converter must not turn interpolation into a bind parameter. This is
    # the single commonest way an automated rewrite breaks a mapper: the query
    # then fails at runtime rather than at conversion, because a column name
    # arrives as a quoted literal.
    for name in stmt.get("interpolations") or []:
        if f"${{{name}}}" not in sql and f"#{{{name}}}" in sql:
            problems.append(f"turned ${{{name}}} into a bind parameter, which changes a "
                            f"column name into a literal")

    # Every bind the original carried must survive, or the statement silently
    # stops filtering on something.
    missing = [b for b in (stmt.get("bind_params") or [])
               if f"#{{{b}}}" not in sql]
    if missing:
        problems.append(f"drops bind parameter(s): {', '.join(missing)}")

    if problems:
        return _gate("static", FAIL, "; ".join(problems),
                     "A conversion is one statement of the same kind as the original, keeps "
                     "every bind parameter, keeps ${} interpolated, and accounts for every "
                     "construct it met.")
    return _gate("static", PASS,
                 f"a {stmt['kind']} with {len(stmt.get('bind_params') or [])} bind(s) kept; "
                 f"{len(conv['constructs'])} construct(s) accounted for")


# ------------------------------------------------------------------- parity


def parity_check(conv: dict, stmt: dict) -> dict:
    """Every construct found in the source is accounted for, and no Oracle form survives.

    The gate that needs no database and catches the failure mode of every
    text-level translator including a model: output that is valid and silently
    dropped something.

    Deterministic, and the rules win over whatever produced the text.
    """
    sql = conv.get("sql") or ""
    scanned = classify.code_only(sql)
    accounting = {a.get("id"): a for a in (conv.get("constructs") or [])}
    found = {c["id"]: c for c in classify.scan(stmt["sql"])}

    problems: list[str] = []
    notes: list[str] = []

    unaccounted = [cid for cid in found if cid not in accounting]
    if unaccounted:
        problems.append("not accounted for: " + ", ".join(sorted(unaccounted)))

    for cid, con in found.items():
        entry = accounting.get(cid)
        if not entry:
            continue
        state = entry.get("state")
        if state == "translated":
            marker = con.get("marker")
            if marker and not re.search(marker, scanned, re.IGNORECASE | re.MULTILINE):
                problems.append(f"{cid} claims translated but its PostgreSQL form is absent")
        elif state == "not_translated":
            if not entry.get("reason"):
                problems.append(f"{cid} is not_translated without a reason")
            else:
                notes.append(f"{cid} not translated: {entry['reason'][:60]}")
        else:
            problems.append(f"{cid} has no state")

    # Residue: the Oracle form must be gone. A construct marked residue=false
    # is one whose Oracle spelling is legal PostgreSQL -- `||`, FOR UPDATE
    # NOWAIT, MERGE -- where the defect is semantic and the text may survive.
    # `scan()` returns display fields, not the pattern, so the pattern comes
    # from the catalogue itself -- one source of truth for what a construct
    # looks like, rather than a copy travelling on the finding.
    #
    # **Residue is checked case-sensitively for the constructs whose Oracle and
    # PostgreSQL forms differ only by case.** `TO_CHAR` and `to_char` are the
    # same name in two engines; so are TRUNC/trunc, SUBSTR/substring and
    # INSTR/regexp_instr. A case-insensitive residue check rejected the first
    # live model run's `customerStatusReport` for emitting `to_char` -- which
    # was the correct PostgreSQL form, and exactly what the marker asks for.
    # Flagging a correct conversion is worse than missing a wrong one here,
    # because it teaches a reader to ignore the gate.
    catalogue = classify.by_id()
    for cid, con in found.items():
        if not con.get("residue"):
            continue
        entry = accounting.get(cid)
        if entry and entry.get("state") == "not_translated":
            continue
        row = catalogue[cid]
        flags = re.MULTILINE if row.get("case_sensitive_residue") else re.IGNORECASE | re.MULTILINE
        if re.search(row["pattern"], scanned, flags):
            problems.append(f"{con['name']} still present in the converted SQL")

    if problems:
        return _gate("parity", FAIL, "; ".join(problems),
                     "Every construct detected in the source is either translated -- with its "
                     "PostgreSQL form actually present -- or recorded as not_translated with a "
                     "reason. No Oracle form whose replacement is required may survive.")
    detail = f"{len(found)} construct(s) accounted for, no Oracle residue"
    return _gate("parity", PASS, detail + ("; " + "; ".join(notes) if notes else ""))


# -------------------------------------------------------------------- parse


def _probe_for_engine(sql: str) -> str:
    """One concrete rendering of a converted statement, for the parse gate.

    MyBatis placeholders are not SQL and no engine parses them:

        #{name}   a bind parameter -> NULL. The gate checks the statement's
                  shape, not its result on a particular argument.
        ${name}   string interpolation for a column or table name. There is no
                  honest substitution, so a statement carrying one is reported
                  unrenderable *before* this function is reached and never
                  sent to an engine.

    Dynamic tags are dropped the same way `extract._probe` drops them, so a
    dynamic statement is checked in its all-branches-false form -- one branch
    of several, which `parse_check` says in its detail.
    """
    out = sql or ""
    out = re.sub(r"<(if|foreach|choose|when|otherwise|bind)\b[^>]*>.*?</\1>", "",
                 out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"</?(where|set|trim)\b[^>]*>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"<selectKey\b[^>]*>.*?</selectKey>", "", out,
                 flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<include\b[^>]*/?>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"#\{\s*[^}]+?\s*\}", "NULL", out)
    return "\n".join(ln for ln in (l.strip() for l in out.splitlines()) if ln).strip()


def _pg_message(exc: Exception) -> str:
    """The engine's own sentence, out of pg8000's wire dict.

    pg8000 raises with the raw PostgreSQL error fields -- `{'S': 'ERROR', 'C':
    '42P01', 'M': 'relation "x" does not exist', ...}`. Putting that on screen
    tells a reader nothing and looks like a crash in the tool. The message
    field plus the SQLSTATE is what a person acts on.
    """
    args = getattr(exc, "args", None)
    fields = args[0] if args and isinstance(args[0], dict) else None
    if fields:
        msg = fields.get("M") or fields.get("message") or ""
        code = fields.get("C") or ""
        detail = fields.get("D") or ""
        hint = fields.get("H") or ""
        out = msg + (f" [{code}]" if code else "")
        for extra in (detail, hint):
            if extra:
                out += f" -- {extra}"
        return out[:300]
    return str(exc).strip().splitlines()[0][:300]


def _parses_on(target, sql: str) -> tuple[bool, str]:
    """Ask the target whether it accepts this statement, without running it.

    `PREPARE` parses and plans the statement -- resolving every table, column
    and function -- and returns no rows, so nothing the statement would have
    read or written happens. It runs inside a transaction that is rolled back
    regardless, for the same reason Phase 4b's compile gate does: the target
    must be left exactly as found.

    A duck-typed `parses()` is honoured first so the self-test's stub keeps
    working. That stub is also how this shipped broken: the gate called
    `target.parses(...)`, which `convert.target.PgTarget` has never had, and
    every offline test passed because the stub did. The first run with a real
    target registered raised AttributeError.
    """
    probe = (sql or "").strip().rstrip(";")
    if hasattr(target, "parses"):
        return target.parses(probe)
    if not probe:
        return False, "nothing to parse"
    try:
        conn = target.connect()
    except Exception as exc:                                  # noqa: BLE001
        return False, f"could not connect to the target: {str(exc)[:200]}"
    try:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            # A bind parameter became NULL in the probe, so the statement has
            # no placeholders left and PREPARE needs no parameter types.
            cur.execute(f"PREPARE dbshift_appsql_probe AS {probe}")
            return True, "the target parsed and planned the statement"
        except Exception as exc:                              # noqa: BLE001
            return False, _pg_message(exc)
        finally:
            try:
                cur.execute("ROLLBACK")
            except Exception:                                 # noqa: BLE001
                pass
    finally:
        try:
            conn.close()
        except Exception:                                     # noqa: BLE001
            pass


def parse_check(conv: dict, stmt: dict, target=None) -> dict:
    """The target accepts the statement. Needs a connection; blocked without one.

    Uses the probe rendering, not the statement, and says so: a dynamic
    statement has a family of texts and only one branch is checked here.
    """
    if target is None:
        return _gate("parse", BLOCKED,
                     "no PostgreSQL target configured, so nothing was sent to an engine",
                     "Configure a target (DBSHIFT_PG_DSN) and run again. Until then this "
                     "statement is converted but unproven, which is weaker evidence than "
                     "converted and parsed.")
    if not stmt.get("probe_renderable"):
        return _gate("parse", BLOCKED,
                     f"not renderable: {stmt.get('probe_unrenderable_because')}",
                     "A statement carrying ${} interpolation has no complete text until a "
                     "column name is supplied at runtime. Parsing a guessed rendering would "
                     "validate a query the application never sends.")

    # The converted SQL still carries MyBatis placeholders -- `#{name}` is
    # required to survive (the static gate fails a conversion that drops one),
    # and PostgreSQL cannot parse a brace. So they are substituted here, the
    # same way `extract._probe` does for the source: a bind becomes NULL
    # because the statement's *shape* is what this gate checks, not its result
    # on any particular argument.
    #
    # Sending the raw conversion instead reported `syntax error at or near "{"`
    # on all twelve statements -- a gate failing on its own probe rather than
    # on the conversion.
    probe = _probe_for_engine(conv.get("probe_sql") or conv.get("sql"))
    ok, detail = _parses_on(target, probe)
    if not ok:
        # **A missing table is not a rejected conversion.** Phase 4c generates
        # the target's schema and rolls it back, so on an unpopulated target
        # every statement fails with `relation "customer" does not exist` --
        # which says nothing about the rewrite. Reporting that as REJECTED
        # condemned all twelve conversions for a fact about the database.
        #
        # 42P01 (undefined_table) and 3F000 (invalid_schema_name) are
        # environment, so they block. Everything else -- syntax, an unknown
        # function, a column the target does not have -- is the conversion's
        # own problem and fails.
        # A shadow schema was built, so the names *are* resolvable and an
        # unresolved one is the conversion's problem -- the application naming
        # an object the target will not have. Without a shadow the same error
        # means only that nothing was there to resolve against.
        shadowed = "shadow table(s)" in (getattr(target, "last_detail", "") or "") or (
            hasattr(target, "describe") and (target.describe() or {}).get("tables_built"))
        if ("[42P01]" in detail or "[3F000]" in detail) and not shadowed:
            return _gate("parse", BLOCKED,
                         f"the target has no schema to parse against: {detail}",
                         "Phase 4c generates this schema and rolls it back, so nothing is "
                         "there to resolve against. Run 4c first so this phase can build a "
                         "shadow from its DDL, and the statement can be parsed for real.")
        return _gate("parse", FAIL, detail,
                     "The target rejected the converted statement. The message names what it "
                     "could not resolve -- usually a column or function that does not exist "
                     "under that name on PostgreSQL.")
    branch = " (one branch of a dynamic statement)" if stmt.get("dynamic") else ""
    return _gate("parse", PASS, f"the target parses the converted statement{branch}")


# ------------------------------------------------------------------- result


def result_check(conv: dict, stmt: dict, comparison=None) -> dict:
    """The converted statement returns the same rows as the original.

    The gate that distinguishes this phase from a reviewer saying "looks
    equivalent". A ROWNUM pagination rewrite parses on the target, passes
    parity, and can still return a different page; only running both against
    the same data settles it.

    `comparison` is supplied by the caller -- it needs both engines and real
    rows, which is a Phase 8 capability. Blocked when absent.
    """
    if comparison is None:
        return _gate("result", BLOCKED,
                     "not compared: needs the Oracle source and the PostgreSQL target, over "
                     "the same rows",
                     "Result equivalence is the only check that catches a rewrite which "
                     "parses and returns different rows. Until it runs, a converted "
                     "statement is syntactically proven and semantically unproven.")

    if comparison.get("not_comparable"):
        return _gate("result", BLOCKED,
                     f"not comparable: {comparison['not_comparable']}",
                     "One side could not be read. Never counted as a match -- an unread side "
                     "is unknown, not equal.")

    mismatches = comparison.get("mismatches") or []
    expected = comparison.get("expected_differences") or []
    rows = comparison.get("rows_compared", 0)

    if mismatches:
        first = mismatches[0]
        return _gate("result", FAIL,
                     f"{len(mismatches)} unexplained difference(s) over {rows} row(s); "
                     f"first: {first}",
                     "The converted statement returns different data. This is the failure the "
                     "phase exists to catch, and no amount of review substitutes for it.")

    detail = f"identical over {rows} row(s)"
    if expected:
        # An expected difference is a real engine difference that is correct.
        # Reported, never hidden, and never counted as a mismatch.
        kinds = sorted({e.get("kind") for e in expected})
        detail += f"; {len(expected)} expected difference(s): {', '.join(kinds)}"
    return _gate("result", PASS, detail)


# ----------------------------------------------------------------- approval


def approval_check(conv: dict, approved_by: str | None) -> dict:
    """A named person, always. There is no auto-apply for application SQL.

    4b takes the same position for stored code and the reasoning is identical:
    this is business logic, and a gate proving it parses and returns the same
    rows on one data set is not a warrant to change what the application sends.
    """
    if not approved_by:
        return _gate("approval", BLOCKED, "no approver named",
                     "Application SQL is changed by a person who accepts it. Pass "
                     "--approved-by with a name that means something to the client.")
    return _gate("approval", PASS, f"approved by {approved_by}")


# -------------------------------------------------------------------- runner

ORDER = ("static", "parity", "parse", "result", "approval")


def run(conv: dict, stmt: dict, target=None, comparison=None,
        approved_by: str | None = None) -> list[dict]:
    """Every gate, stopping at the first failure.

    A blocked gate does not stop the run: blocked means "could not be
    established here", and the gates after it may still say something useful.
    A *failed* gate stops, because a later pass would read as reassurance about
    a conversion already known to be wrong.
    """
    gates: list[dict] = []
    for name in ORDER:
        if name == "static":
            g = static_check(conv, stmt)
        elif name == "parity":
            g = parity_check(conv, stmt)
        elif name == "parse":
            g = parse_check(conv, stmt, target)
        elif name == "result":
            g = result_check(conv, stmt, comparison)
        else:
            g = approval_check(conv, approved_by)
        gates.append(g)
        if g["status"] == FAIL:
            break
    return gates


def outcome(gates: list[dict]) -> str:
    """The terminal state these gates imply."""
    by = {g["gate"]: g for g in gates}
    if any(g["status"] == FAIL for g in gates):
        return "REJECTED"
    if by.get("approval", {}).get("status") == PASS:
        return "APPROVED"
    # Everything a machine can establish has been established.
    if (by.get("parse", {}).get("status") == PASS
            and by.get("result", {}).get("status") == PASS):
        return "READY_FOR_APPROVAL"
    if by.get("parity", {}).get("status") == PASS:
        # Converted and parity-checked, but nothing was run against an engine.
        # Deliberately distinct from READY: a client reading "ready" about an
        # unvalidated statement has been told something untrue.
        return "CONVERTED_UNPROVEN"
    return "BLOCKED"
