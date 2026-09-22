"""Deterministic rewrites for application SQL, and an explicit refusal otherwise.

Same arrangement as `convert/rules.py`: each rewrite records what it did and
why, and anything outside the covered subset raises `Declined` -- a routing
decision, not a failure. A rule that guesses is worse than a rule that declines,
because a guess reaches a client looking like a conversion.

Only the eight `tier=rule` constructs are handled here. The fourteen model-tier
constructs are declined on purpose: `CONNECT BY` to `WITH RECURSIVE` and the
`(+)` outer join need the statement understood, not pattern-matched, and a
regex that appears to handle them produces SQL that parses and returns
different rows. The four manual constructs are declined with the reason a
person needs.

**Three things these rewrites never touch.**

  1. **String literals.** `_segments` yields them separately so a rewrite
     cannot edit the inside of a quoted string -- `'the NEXTVAL of'` in a
     message stays as written.

  2. **MyBatis dynamic tags and their attributes.** `<if test="...">` is Java.
     Editing it would change the branch condition, and the tag must survive
     intact for the converted statement to still be the statement the
     application sends.

  3. **`${}` interpolation.** Rewriting it to `#{}` turns a column name into a
     bound literal and breaks the query at runtime. The `static` gate fails a
     conversion that does this; these rules simply never do it.
"""

from __future__ import annotations

import re

I = re.IGNORECASE

# A dynamic tag, its attributes, or an interpolation site. Protected the same
# way string literals are: yielded as an opaque segment no rewrite can edit.
_PROTECTED = re.compile(r"<[^>]+>|\$\{[^}]*\}")


class Declined(Exception):
    """The rules do not cover this statement. A routing decision, not a failure."""


def _c(construct_id: str, state: str, postgres: str | None = None,
       reason: str | None = None) -> dict:
    """One construct accounting entry, in the shape `gates.parity_check` reads."""
    e = {"id": construct_id, "state": state}
    if postgres:
        e["postgres"] = postgres
    if reason:
        e["reason"] = reason
    return e


def _segments(text: str):
    """Yield (protected, segment). A protected segment is never rewritten.

    Protected means: inside a string literal, inside a MyBatis tag, or an
    interpolation site. Everything else is SQL a rule may edit.
    """
    i, n, start = 0, len(text), 0

    def flush(upto):
        if upto > start:
            yield False, text[start:upto]

    while i < n:
        ch = text[i]
        if ch == "'":
            yield from flush(i)
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            yield True, text[i:j + 1]
            i = start = j + 1
            continue
        m = _PROTECTED.match(text, i)
        if m:
            yield from flush(i)
            yield True, m.group(0)
            i = start = m.end()
            continue
        i += 1
    if start < n:
        yield False, text[start:]


def _sub(pattern: str, repl, text: str, flags=I) -> tuple[str, int]:
    """Substitute outside protected segments. Returns the text and a hit count."""
    out, hits = [], 0
    for protected, seg in _segments(text):
        if protected:
            out.append(seg)
            continue
        new, n = re.subn(pattern, repl, seg, flags=flags)
        hits += n
        out.append(new)
    return "".join(out), hits


def _search(pattern: str, text: str, flags=I) -> bool:
    return any(re.search(pattern, seg, flags)
               for protected, seg in _segments(text) if not protected)


# --------------------------------------------------------------- the rewrites
#
# One function per rule-tier construct. Each returns the new text and its
# accounting entry, or leaves the text alone and returns None when the
# construct is not present.


def _r_dual(sql: str) -> tuple[str, dict | None]:
    """FROM DUAL -> nothing.

    PostgreSQL allows a SELECT with no FROM. Inside a MERGE's USING clause the
    subquery still needs a row source -- but MERGE is model-tier and declined
    before these rules run, so the bare case is the only one reaching here.
    """
    if not _search(r"\bFROM\s+DUAL\b", sql):
        return sql, None
    out, _ = _sub(r"\s*\bFROM\s+DUAL\b", "", sql)
    return out, _c("DUAL", "translated", "the FROM clause is omitted")


def _r_nextval(sql: str) -> tuple[str, dict | None]:
    """seq.NEXTVAL -> nextval('seq'), lower-casing the sequence name.

    The name becomes a string literal, so case matters: an unquoted Oracle
    identifier folds to upper case and an unquoted PostgreSQL one folds to
    lower, which is why CUSTOMER_SEQ.NEXTVAL is nextval('customer_seq') and not
    nextval('CUSTOMER_SEQ') -- the latter looks right and finds nothing.
    """
    if not _search(r"\b\w+\.NEXTVAL\b", sql):
        return sql, None
    out, _ = _sub(r"\b(\w+)\.NEXTVAL\b",
                  lambda m: f"nextval('{m.group(1).lower()}')", sql)
    return out, _c("SEQ_NEXTVAL", "translated", "nextval('sequence')")


def _r_currval(sql: str) -> tuple[str, dict | None]:
    if not _search(r"\b\w+\.CURRVAL\b", sql):
        return sql, None
    out, _ = _sub(r"\b(\w+)\.CURRVAL\b",
                  lambda m: f"currval('{m.group(1).lower()}')", sql)
    return out, _c("SEQ_CURRVAL", "translated", "currval('sequence')")


def _r_nvl(sql: str) -> tuple[str, dict | None]:
    if not _search(r"\bNVL\s*\(", sql):
        return sql, None
    out, _ = _sub(r"\bNVL\s*\(", "COALESCE(", sql)
    return out, _c("NVL", "translated", "COALESCE()")


def _r_minus(sql: str) -> tuple[str, dict | None]:
    if not _search(r"^\s*MINUS\s*$", sql, I | re.MULTILINE):
        return sql, None
    out, _ = _sub(r"^(\s*)MINUS(\s*)$", r"\1EXCEPT\2", sql, I | re.MULTILINE)
    return out, _c("MINUS", "translated", "EXCEPT")


def _r_sysdate(sql: str) -> tuple[str, dict | None]:
    """SYSDATE -> date_trunc('second', LOCALTIMESTAMP).

    Oracle DATE carries second precision. A bare LOCALTIMESTAMP is microsecond,
    so a value the application writes never equals one it compares against --
    the truncation is the whole point of the rewrite.
    """
    if not _search(r"\bSYSDATE\b", sql):
        return sql, None
    out, _ = _sub(r"\bSYSDATE\b", "date_trunc('second', LOCALTIMESTAMP)", sql)
    return out, _c("SYSDATE", "translated",
                   "date_trunc('second', LOCALTIMESTAMP) -- Oracle DATE is second precision")


def _r_systimestamp(sql: str) -> tuple[str, dict | None]:
    if not _search(r"\bSYSTIMESTAMP\b", sql):
        return sql, None
    out, _ = _sub(r"\bSYSTIMESTAMP\b", "CURRENT_TIMESTAMP", sql)
    return out, _c("SYSTIMESTAMP", "translated", "CURRENT_TIMESTAMP")


def _r_to_number(sql: str) -> tuple[str, dict | None]:
    """TO_NUMBER(x) -> CAST(x AS numeric).

    Only the one-argument form. TO_NUMBER with a format model is a different
    problem -- the models differ between the engines -- and is declined.
    """
    if not _search(r"\bTO_NUMBER\s*\(", sql):
        return sql, None
    if _search(r"\bTO_NUMBER\s*\([^)]*,", sql):
        raise Declined("TO_NUMBER with a format model: the Oracle and PostgreSQL "
                       "number format models differ, so the rewrite needs judgement")
    out, _ = _sub(r"\bTO_NUMBER\s*\(([^()]*)\)",
                  lambda m: f"CAST({m.group(1)} AS numeric)", sql)
    return out, _c("TO_NUMBER", "translated", "CAST(... AS numeric)")


def _r_decode(sql: str) -> tuple[str, dict | None]:
    """DECODE is rule-tier in the catalogue but declined here, deliberately.

    DECODE's arguments are a flat list whose arity decides whether the last one
    is a default or another search/result pair, and a faithful CASE needs the
    argument list parsed with nesting and commas inside function calls handled.
    A regex that gets this right for the demo statement and wrong for a nested
    call is the kind of rule that is worse than no rule, so this declines and
    the model handles it under the same gates.
    """
    if not _search(r"\bDECODE\s*\(", sql):
        return sql, None
    raise Declined("DECODE: a faithful CASE rewrite needs the argument list parsed, "
                   "including nesting and the odd/even arity that decides whether the "
                   "last argument is a default")


def _r_to_date(sql: str) -> tuple[str, dict | None]:
    """TO_DATE is declined: the target column's type decides the rewrite.

    to_date returns a PostgreSQL DATE, which has no time component. Where the
    Oracle value needed its time, the correct rewrite is to_timestamp. Choosing
    between them needs the column, which these rules do not have.
    """
    if not _search(r"\bTO_DATE\s*\(", sql):
        return sql, None
    raise Declined("TO_DATE: to_date returns a PostgreSQL DATE with no time component, "
                   "so whether this is to_date or to_timestamp depends on the target "
                   "column's type")


# Order matters only where one rewrite's output could match another's pattern.
# It cannot here -- no rewrite emits an Oracle form -- but the order is fixed
# anyway so two runs produce byte-identical output.
_RULES = (
    _r_dual, _r_nextval, _r_currval, _r_nvl, _r_minus,
    _r_sysdate, _r_systimestamp, _r_to_number,
    # Declining rules last, so a statement carrying both a handled and a
    # declined construct still reports the declined one as its reason.
    _r_decode, _r_to_date,
)

# Constructs these rules cannot handle, with the reason a person reads. Checked
# before any rewrite runs: a statement carrying one of these is declined whole
# rather than half-converted.
_NOT_COVERED = {
    "OUTER_JOIN_PLUS": "the (+) operator was removed from PostgreSQL; which table becomes "
                       "the outer side of an explicit LEFT JOIN depends on which side "
                       "carried it, and a comma-join must be re-expressed as a join tree",
    "ROWNUM": "ROWNUM is assigned before ORDER BY in the same query block, which is why "
              "the pagination idiom nests twice. A rewrite that flattens it returns "
              "different rows",
    "CONNECT_BY": "LEVEL must be synthesised in the recursive term and ORDER SIBLINGS BY "
                  "has no equivalent, so the shape of the CTE depends on the hierarchy",
    "LISTAGG": "the WITHIN GROUP ordering moves inside string_agg, and ON OVERFLOW has "
               "no equivalent",
    "MERGE": "PostgreSQL 15 added MERGE, but ON CONFLICT is usually the better rewrite "
             "and needs a unique constraint to name -- the target version decides",
    "TRUNC": "TRUNC is overloaded on number and date in Oracle; PostgreSQL has separate "
             "functions, so the argument's type decides the rewrite",
    "MONTHS_BETWEEN": "Oracle returns a fractional month count on a 31-day convention "
                      "where the PostgreSQL idiom returns whole months, so a threshold "
                      "comparison changes which rows match",
    "ADD_MONTHS": "ADD_MONTHS clamps to month end -- 31 Jan plus one month is 28 Feb "
                  "where the interval form gives 3 March",
    "SUBSTR_NEGATIVE": "Oracle counts a negative start from the end of the string; "
                       "PostgreSQL's substring() returns a different result rather than "
                       "raising, so the defect is silent",
    "INSTR": "position() takes no start or occurrence argument, so the 4-argument form "
             "needs a regexp or a lateral join",
    "TO_CHAR_FORMAT": "the format models differ in case handling and padding, and the "
                      "month abbreviation is locale-dependent on both engines",
    "NVL2": "the three-argument form has no PostgreSQL equivalent and becomes a CASE",
    "FOR_UPDATE_NOWAIT": "the syntax is accepted unchanged, but Oracle raises ORA-00054 "
                         "and PostgreSQL SQLSTATE 55P03, so application code catching the "
                         "Oracle code stops handling lock contention",
    "CONCAT_PIPE": "|| is legal PostgreSQL with different NULL semantics: NULL || 'x' is "
                   "'x' in Oracle and NULL in PostgreSQL, so whether concat() is the "
                   "faithful rewrite depends on whether the column is nullable",
    "SELECTKEY_SEQUENCE": "MyBatis handles generated keys differently per engine; the "
                          "idiomatic rewrite drops selectKey for RETURNING, which changes "
                          "the mapper's structure rather than only its SQL",
    # The manual tier. Declined with the reason, and no model is asked either.
    "EMPTY_STRING_NULL": "Oracle stores '' as NULL and PostgreSQL stores a zero-length "
                         "string. No rewrite of the SQL fixes this -- every IS NULL test "
                         "and unique constraint over the column behaves differently, and "
                         "it is an application redesign",
    "OPTIMIZER_HINT": "a hint is a comment, so PostgreSQL ignores it silently rather than "
                      "failing. It must be reported as dropped, not converted -- a "
                      "dropped hint nobody saw is a performance incident after cutover",
    "ROWID": "ctid changes on UPDATE and VACUUM, so an application that round-trips a "
             "ROWID must be redesigned around the primary key",
    "MYBATIS_INTERPOLATION": "${} must stay interpolated. Rewriting it to #{} turns a "
                             "column name into a bound literal and breaks the query at "
                             "runtime rather than at conversion",
    "RESERVED_WORD_OBJECT": "Phase 4c renames the target object because its Oracle name is "
                            "a PostgreSQL keyword -- ORDER becomes order_col. Only 4c knows "
                            "the new name, so these rules cannot supply it; the statement is "
                            "reported so a person applies 4c's mapping",
}


def convert(sql: str, constructs: list[dict]) -> dict:
    """Rewrite one statement by rule, or decline with a reason.

    `constructs` is what `classify.scan` found in the source. A statement
    carrying anything outside the deterministic subset is declined **whole**:
    half-converting it would produce text that looks finished and is not.
    """
    blocking = [c for c in constructs if c["id"] in _NOT_COVERED]
    if blocking:
        first = blocking[0]
        raise Declined(f"{first['name']}: {_NOT_COVERED[first['id']]}")

    out = sql
    accounting: list[dict] = []
    for rule in _RULES:
        out, entry = rule(out)
        if entry:
            accounting.append(entry)

    # Anything the classifier found but no rule claimed. Recorded rather than
    # ignored, so the parity gate sees a complete accounting -- an unmentioned
    # construct is exactly what parity exists to catch.
    claimed = {e["id"] for e in accounting}
    for con in constructs:
        if con["id"] not in claimed:
            accounting.append(_c(con["id"], "not_translated",
                                 reason="no rule claimed this construct and it is not in "
                                        "the declined list; review the catalogue"))

    if not accounting:
        raise Declined("no catalogued construct found, so there is nothing to rewrite "
                       "and nothing to prove; the statement may port unchanged")

    return {
        "sql": " ".join(out.split()) if "\n" not in out else out.strip(),
        "constructs": accounting,
        "source": "rule",
        "model_id": None,
    }


def declined_reason(constructs: list[dict]) -> str | None:
    """Why the rules would decline these constructs, without running them."""
    for con in constructs:
        if con["id"] in _NOT_COVERED:
            return f"{con['name']}: {_NOT_COVERED[con['id']]}"
    return None
