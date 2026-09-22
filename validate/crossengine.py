"""Comparing Oracle to PostgreSQL, where "the same data" is not the same bytes.

The homogeneous path can hash both sides and compare the numbers. Across engines
that is meaningless: `ORA_HASH` does not exist in PostgreSQL, the two engines
render the same value as different text, and in one well-known case they do not
even agree on what a value *is*.

So this module does three things:

  1. Builds a **canonical text form** of each row, per engine, so two different
     renderings of the same value produce the same string on both sides.
  2. Hashes that canonical text with an algorithm both engines have -- MD5 over
     the concatenated row, summed. No Oracle-specific hash anywhere.
  3. Names the differences that are **correct**, so they are reported as
     `expected_difference` with the reason rather than counted as data loss.

The third is the one that matters. A cross-engine validation that flags every
semantic difference as a mismatch is unusable: it will show hundreds of
failures on a perfectly good migration, and a person will start ignoring it.
"""

from __future__ import annotations

# --- the differences that are correct ----------------------------------------
#
# Each of these is a real behavioural difference between the engines. None is a
# migration defect, and every one of them would otherwise show up as a checksum
# mismatch on data that moved perfectly.

EXPECTED_DIFFERENCES = {
    "empty_string_is_null": (
        "Oracle stores an empty string as NULL; PostgreSQL stores it as a "
        "zero-length string, which is a different value from NULL. A column "
        "that was '' on Oracle arrives as NULL on the target and is *correct* "
        "either way -- there is no round trip that could preserve the "
        "distinction, because Oracle never had it."
    ),
    "number_scale": (
        "Oracle NUMBER without a declared scale keeps whatever the caller "
        "inserted; PostgreSQL NUMERIC without a scale does too, but the two "
        "render trailing zeros differently -- 1.50 against 1.5. The value is "
        "identical; the text is not, which is why the canonical form strips "
        "trailing zeros on both sides before hashing."
    ),
    "date_has_time": (
        "Oracle DATE carries a time component; PostgreSQL DATE does not. If "
        "the column was mapped to DATE rather than TIMESTAMP, the time is gone "
        "-- which is data loss, not a rendering difference. This is reported "
        "as a mismatch, and the type mapping is the thing to fix."
    ),
    "identifier_case": (
        "Oracle folds unquoted identifiers to upper case, PostgreSQL to lower. "
        "Phase 7's DMS mappings fold every name down, so a table Oracle calls "
        "CUSTOMER is `customer` here. Same object, different spelling."
    ),
    "char_padding": (
        "Oracle CHAR(n) pads with spaces to the declared length and so does "
        "PostgreSQL character(n) -- but PostgreSQL ignores trailing spaces when "
        "comparing, and a CHAR mapped to VARCHAR loses the padding entirely. "
        "The canonical form right-trims CHAR columns on both sides."
    ),
    "boolean_from_number": (
        "Oracle has no boolean type, so flags are NUMBER(1) or CHAR(1). If the "
        "conversion mapped one to PostgreSQL boolean, 1/0 becomes true/false: "
        "the same meaning in a different representation, which no byte "
        "comparison can reconcile."
    ),
}


# Oracle types whose text rendering differs from PostgreSQL's for the same
# value. Anything not here is compared as plain text on both sides.
def oracle_expr(column_name: str, data_type: str) -> str:
    """Canonical text for one Oracle column.

    Every branch exists to make Oracle's rendering match PostgreSQL's for a
    value that is genuinely equal.
    """
    q = f'"{column_name}"'
    t = (data_type or "").upper()

    if t == "DATE":
        # Oracle DATE is a timestamp to the second. Rendered in full so that a
        # target TIMESTAMP matches, and so that a target DATE (which has no
        # time) visibly does not.
        return f"TO_CHAR({q},'YYYY-MM-DD HH24:MI:SS')"
    if t.startswith("TIMESTAMP"):
        fmt = "YYYY-MM-DD HH24:MI:SS.FF6"
        if "TIME ZONE" in t:
            # Compared in UTC: the same instant stored in two zones is the same
            # instant, and the offset is a rendering detail.
            return f"TO_CHAR(SYS_EXTRACT_UTC({q}),'YYYY-MM-DD HH24:MI:SS.FF6')"
        return f"TO_CHAR({q},'{fmt}')"
    if t in ("NUMBER", "FLOAT", "BINARY_DOUBLE", "BINARY_FLOAT"):
        # TM9 gives the shortest exact decimal: no trailing zeros, no leading
        # space for sign, no exponent for ordinary magnitudes. It matches
        # PostgreSQL's numeric text **except between -1 and 1**, where Oracle
        # drops the leading zero: 0.023 renders as '.023', and PostgreSQL's
        # '0.023' then hashes differently. Measured 2026-09-21 on
        # USAGE_STAGING -- 250,000 rows with an identical SUM and an identical
        # DISTINCT count, reported as a data mismatch purely on that character.
        # The REPLACE puts the zero back, on the sign as well as the bare form.
        tm9 = f"TRIM(TO_CHAR({q},'TM9'))"
        return (f"CASE WHEN {tm9} LIKE '.%' THEN '0' || {tm9} "
                f"WHEN {tm9} LIKE '-.%' THEN '-0' || SUBSTR({tm9},2) "
                f"ELSE {tm9} END")
    if t in ("CHAR", "NCHAR"):
        # Blank padding is storage, not data.
        return f"RTRIM({q})"
    if t == "RAW":
        return f"LOWER(RAWTOHEX({q}))"
    if t in ("CLOB", "NCLOB"):
        # A LOB cannot be concatenated inline past 4000 characters, so the
        # comparison is over a prefix and the evidence says so.
        return f"DBMS_LOB.SUBSTR({q},4000,1)"
    if t == "BLOB":
        return f"LOWER(RAWTOHEX(DBMS_LOB.SUBSTR({q},2000,1)))"
    return f"TO_CHAR({q})"


def postgres_expr(column_name: str, oracle_type: str) -> str:
    """Canonical text for the matching PostgreSQL column.

    Deliberately keyed on the **Oracle** type, not the PostgreSQL one: the
    question is "does this hold the value the source had", and the source type
    is what says how to render it.
    """
    q = f'"{column_name.lower()}"'
    t = (oracle_type or "").upper()

    if t == "DATE":
        return f"to_char({q}::timestamp,'YYYY-MM-DD HH24:MI:SS')"
    if t.startswith("TIMESTAMP"):
        if "TIME ZONE" in t:
            return f"to_char({q} AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.US')"
        return f"to_char({q},'YYYY-MM-DD HH24:MI:SS.US')"
    if t in ("NUMBER", "FLOAT", "BINARY_DOUBLE", "BINARY_FLOAT"):
        # trim_scale drops trailing zeros so 1.50 and 1.5 hash the same, which
        # is the single most common false mismatch across these engines. Then a
        # plain text cast, NOT to_char with a format mask: an FM mask leaves a
        # trailing decimal point on a whole number ("10." for 10), while
        # Oracle's TM9 does not -- so every integer column mismatched. The cast
        # produces exactly what TM9 does.
        return f"trim_scale({q}::numeric)::text"
    if t in ("CHAR", "NCHAR"):
        return f"rtrim({q}::text)"
    if t == "RAW":
        # No ::bytea cast: the column already is one, and casting a bytea
        # through text re-encodes it rather than converting it.
        return f"lower(encode({q},'hex'))"
    if t in ("CLOB", "NCLOB"):
        return f"left({q}::text,4000)"
    if t == "BLOB":
        return f"lower(encode(substring({q} from 1 for 2000),'hex'))"
    return f"{q}::text"


# The placeholder standing in for NULL.
#
# Printable ASCII on purpose. A non-ASCII marker would have to survive the
# source character set, the target's, and both client encodings unchanged --
# transcoded on one side but not the other, it would make every row differ. A
# raw control byte survives encoding but not source control: editors strip it
# and diffs mangle it.
#
# Using the *same* marker on both sides is what makes Oracle's
# empty-string-is-NULL behaviour hash identically to PostgreSQL's NULL, which
# is the single most common false mismatch between these engines.
NULL_MARKER = "<NULL>"


def oracle_checksum_sql(owner: str, table: str, columns: list[dict]) -> str:
    """Row count and a summed MD5 over the canonical row text.

    STANDARD_HASH with MD5 rather than ORA_HASH: PostgreSQL has md5(), so both
    sides can compute the same function over the same text. The sum is order-
    independent, which matters because no ORDER BY is applied to either side.
    """
    parts = [f"NVL({oracle_expr(c['column_name'], c['data_type'])},'{NULL_MARKER}')"
             for c in columns]
    joined = " || '|' || ".join(parts)
    return (
        f"SELECT COUNT(*), "
        f"NVL(SUM(TO_NUMBER(SUBSTR(STANDARD_HASH({joined},'MD5'),1,12),"
        f"'XXXXXXXXXXXX')),0) "
        f'FROM "{owner}"."{table}"')


def postgres_checksum_sql(schema: str, table: str, columns: list[dict]) -> str:
    """The same computation on the target, over the same canonical text."""
    parts = [f"coalesce({postgres_expr(c['column_name'], c['data_type'])},'{NULL_MARKER}')"
             for c in columns]
    joined = " || '|' || ".join(parts)
    return (
        f"SELECT count(*), "
        f"coalesce(sum(('x' || substr(md5({joined}),1,12))::bit(48)::bigint),0) "
        f'FROM "{schema.lower()}"."{table.lower()}"')


# Types excluded from the checksum, with the reason. Comparing them would
# produce a mismatch on identical data, which is worse than not comparing.
UNCOMPARABLE_TYPES = {
    "LONG": "Oracle LONG cannot be used in most SQL expressions, including a hash",
    "LONG RAW": "Oracle LONG RAW cannot be used in SQL expressions",
    "XMLTYPE": "XML is stored and re-serialised differently by each engine; the same "
               "document is not the same text",
    "BFILE": "a pointer to a file outside the database; there is nothing on the target to compare",
    "ROWID": "a physical address, meaningless on a different engine",
    "UROWID": "a physical address, meaningless on a different engine",
    "ANYDATA": "a self-describing container with no PostgreSQL equivalent",
}


def comparable_columns(columns: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split columns into those a checksum can include and those it cannot."""
    ok, skipped = [], []
    for c in columns:
        t = (c.get("data_type") or "").upper()
        reason = UNCOMPARABLE_TYPES.get(t)
        if reason is None and c.get("data_type_owner"):
            reason = (f"{t} is a user-defined type; the target holds its parts differently "
                      "and there is no text form both engines agree on")
        if reason:
            skipped.append({**c, "why_skipped": reason})
        else:
            ok.append(c)
    return ok, skipped


def explain_difference(oracle_type: str, oracle_value, target_value) -> dict | None:
    """Is this difference one of the known-correct ones?

    Returns the explanation when the difference is expected, or None when it is
    a real mismatch. Only ever *downgrades* a mismatch to an explained
    difference -- it never turns a difference into a match.
    """
    t = (oracle_type or "").upper()

    if oracle_value is None and target_value == "":
        return {"kind": "empty_string_is_null",
                "why": EXPECTED_DIFFERENCES["empty_string_is_null"]}
    if oracle_value == "" and target_value is None:
        return {"kind": "empty_string_is_null",
                "why": EXPECTED_DIFFERENCES["empty_string_is_null"]}

    if t in ("NUMBER", "FLOAT") and oracle_value is not None and target_value is not None:
        try:
            if float(oracle_value) == float(target_value):
                return {"kind": "number_scale", "why": EXPECTED_DIFFERENCES["number_scale"]}
        except (TypeError, ValueError):
            pass

    if t in ("CHAR", "NCHAR") and isinstance(oracle_value, str) and isinstance(target_value, str):
        if oracle_value.rstrip() == target_value.rstrip():
            return {"kind": "char_padding", "why": EXPECTED_DIFFERENCES["char_padding"]}

    if t in ("NUMBER", "CHAR") and str(target_value).lower() in ("true", "false"):
        expected = "true" if str(oracle_value) in ("1", "Y", "T") else "false"
        if str(target_value).lower() == expected:
            return {"kind": "boolean_from_number",
                    "why": EXPECTED_DIFFERENCES["boolean_from_number"]}

    return None


def target_name(oracle_name: str) -> str:
    """What Phase 7 called this object on the target.

    One function, because getting it wrong in one place and right in another is
    how a validation reports a missing table that is sitting there under a
    different spelling.
    """
    return (oracle_name or "").lower()
