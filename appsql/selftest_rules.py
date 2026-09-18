"""Offline checks for the deterministic application SQL rewrites. No database, no AWS.

The rules' job is to be *right* on a small subset and to decline everything
else clearly. So these checks are mostly about the declining:

  1. **A statement is declined whole, never half-converted.** Seven of the
     eighteen seeded statements carry a rule-tier construct *and* something the
     rules cannot handle -- `createLoan` has three handleable constructs and a
     `selectKey`. Converting the three and leaving the fourth produces text
     that looks finished and is not, which is the worst possible output. This
     is why the construct-level figure (8 of 27 deterministic) and the
     statement-level figure (2 of 18, 11%) differ, and the statement figure is
     the honest one.

  2. **A rewrite never edits a protected segment.** String literals, MyBatis
     tags and `${}` interpolation are yielded opaque. A rule that edits a
     `test="..."` attribute changes a branch condition; one that rewrites
     `${}` to `#{}` breaks the query at runtime.

  3. **Every declined construct names a reason a person can act on.** A
     decline with no reason is indistinguishable from a bug.

  4. **The accounting is complete.** Every construct the classifier found is
     either translated or recorded not_translated with a reason, because that
     is what the parity gate checks and a gap there is a silent drop.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "appsql"

from . import classify, extract, gates, rules

MAPPERS = Path(__file__).resolve().parent.parent / "scripts" / "demo-app" / "mappers"


def _conv(sql):
    """Rewrite a bare SQL string, scanning it first as the pipeline does."""
    return rules.convert(sql, classify.scan(sql))


class Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = Check()

    # ------------------------------------------------------- the rewrites
    print("the eight deterministic rewrites")
    r = _conv("SELECT customer_seq.NEXTVAL FROM DUAL")
    c("FROM DUAL is removed", "DUAL" not in r["sql"].upper(), r["sql"])
    c("NEXTVAL becomes nextval()", "nextval(" in r["sql"])
    c("the sequence name is lower-cased", "'customer_seq'" in r["sql"], r["sql"])
    c("the rewrite is labelled a rule", r["source"] == "rule")
    c("no model is claimed", r["model_id"] is None)

    c("NVL becomes COALESCE",
      "COALESCE(" in _conv("SELECT NVL(a, b) FROM t")["sql"])
    c("MINUS becomes EXCEPT",
      "EXCEPT" in _conv("SELECT a FROM t\nMINUS\nSELECT a FROM u")["sql"])
    c("SYSDATE is truncated to seconds",
      "date_trunc('second'" in _conv("SELECT SYSDATE FROM DUAL")["sql"])
    c("SYSTIMESTAMP becomes CURRENT_TIMESTAMP",
      "CURRENT_TIMESTAMP" in _conv("SELECT SYSTIMESTAMP FROM DUAL")["sql"])
    c("TO_NUMBER becomes a cast",
      "CAST(" in _conv("SELECT TO_NUMBER(x) FROM t")["sql"])
    c("CURRVAL becomes currval()",
      "currval('s')" in _conv("SELECT s.CURRVAL FROM DUAL")["sql"])

    # The truncation is the point of the SYSDATE rule: a bare LOCALTIMESTAMP is
    # microsecond and would never equal a stored Oracle DATE.
    c("SYSDATE is not rewritten to a bare LOCALTIMESTAMP",
      "date_trunc" in _conv("SELECT SYSDATE FROM DUAL")["sql"])

    # --------------------------------------------------- protected segments
    print("\nprotected segments are never edited")
    r = _conv("SELECT 'the NEXTVAL of it', s.NEXTVAL FROM DUAL")
    c("a literal mentioning a construct is untouched",
      "'the NEXTVAL of it'" in r["sql"], r["sql"])
    c("the real construct outside the literal is rewritten",
      "nextval('s')" in r["sql"], r["sql"])

    r = _conv("SELECT NVL(a,b) FROM t <if test=\"NVL(x) != null\">AND x = 1</if>")
    c("a dynamic tag survives intact",
      'test="NVL(x) != null"' in r["sql"], r["sql"])
    c("the tag's attribute is not rewritten",
      "test=\"COALESCE" not in r["sql"])
    c("SQL outside the tag is still rewritten", "COALESCE(a,b)" in r["sql"])

    # ${} must survive verbatim. A statement carrying it is declined outright
    # (it is a manual construct), so the property is asserted at the rewrite
    # level: even if a future rule ran over such a statement, no substitution
    # may reach an interpolation site.
    out, _hits = rules._sub(r"\bNVL\s*\(", "COALESCE(",
                            "SELECT NVL(a,b) FROM t ORDER BY ${NVL_col}")
    c("interpolation survives a rewrite verbatim", "${NVL_col}" in out, out)
    c("a rewrite never edits inside an interpolation site",
      "${COALESCE" not in out, out)
    c("SQL outside the interpolation is still rewritten", "COALESCE(a,b)" in out)
    c("a statement carrying interpolation is declined outright",
      _declines("SELECT NVL(a,b) FROM t ORDER BY ${orderBy}"))

    # ---------------------------------------------------------- declining
    print("\ndeclining is explicit and reasoned")
    for sql, what in [
        ("SELECT a FROM t, u WHERE t.id = u.id(+)", "the (+) outer join"),
        ("SELECT * FROM (SELECT a, ROWNUM rn FROM t) WHERE rn > 1", "ROWNUM"),
        ("SELECT LEVEL FROM t CONNECT BY PRIOR id = pid", "CONNECT BY"),
        ("SELECT LISTAGG(a, ',') FROM t", "LISTAGG"),
        ("MERGE INTO t USING u ON (t.id = u.id) WHEN MATCHED THEN UPDATE SET x = 1", "MERGE"),
        ("SELECT TRUNC(d) FROM t", "TRUNC"),
        ("SELECT MONTHS_BETWEEN(a, b) FROM t", "MONTHS_BETWEEN"),
        ("SELECT ADD_MONTHS(d, 1) FROM t", "ADD_MONTHS"),
        ("SELECT SUBSTR(a, -4) FROM t", "a negative SUBSTR"),
        ("SELECT INSTR(a, '-', 1, 2) FROM t", "the 4-argument INSTR"),
        ("SELECT TO_CHAR(d, 'DD-MON-YYYY') FROM t", "a TO_CHAR format model"),
        ("SELECT NVL2(a, b, c) FROM t", "NVL2"),
        ("SELECT a FROM t FOR UPDATE NOWAIT", "FOR UPDATE NOWAIT"),
        ("SELECT ROWID FROM t", "ROWID"),
        ("SELECT /*+ FULL(t) */ a FROM t", "an optimizer hint"),
        ("SELECT NVL(a, '') FROM t", "the empty-string case"),
        ("SELECT a FROM t ORDER BY ${col}", "interpolation"),
        ("SELECT DECODE(s, 'A', 'x') FROM t", "DECODE"),
        ("SELECT TO_DATE(s, 'YYYY') FROM t", "TO_DATE"),
    ]:
        try:
            _conv(sql)
            c(f"{what} is declined", False, "it converted instead")
        except rules.Declined as exc:
            c(f"{what} is declined", True)
            c(f"  ...and the reason is substantive", len(str(exc)) > 50, str(exc))

    c("a statement with no catalogued construct is declined too",
      _declines("SELECT a FROM t WHERE b = 1"))

    print("\ndeclining names the construct before the reason")
    try:
        _conv("SELECT ROWID FROM t")
    except rules.Declined as exc:
        c("the construct is named first", str(exc).startswith("ROWID"), str(exc))
        c("the reason follows a colon", ": " in str(exc))

    # ------------------------------------------- whole-statement declining
    print("\na mixed statement is declined whole, never half-converted")
    mixed = "SELECT NVL(a, b), ROWID FROM t"
    c("a handleable construct does not rescue a blocked statement", _declines(mixed))
    try:
        _conv(mixed)
    except rules.Declined as exc:
        c("the blocking construct is the one reported", "ROWID" in str(exc))
    c("declined_reason agrees without running the rules",
      rules.declined_reason(classify.scan(mixed)) is not None)
    c("declined_reason is silent on a convertible statement",
      rules.declined_reason(classify.scan("SELECT NVL(a,b) FROM DUAL")) is None)

    # ------------------------------------------------------- accounting
    print("\nthe accounting is complete enough for the parity gate")
    r = _conv("SELECT customer_seq.NEXTVAL FROM DUAL")
    ids = {e["id"] for e in r["constructs"]}
    c("every construct found is accounted for", ids == {"DUAL", "SEQ_NEXTVAL"}, str(ids))
    c("each entry carries a state", all(e.get("state") for e in r["constructs"]))
    c("a translated entry names its PostgreSQL form",
      all(e.get("postgres") for e in r["constructs"] if e["state"] == "translated"))

    # The real proof: the parity gate accepts what the rules produce. If the
    # accounting were incomplete, parity would fail its own conversion.
    stmts = {s["statement_id"]: s for s in extract.from_dir(MAPPERS)["statements"]}
    converted, declined = {}, {}
    for sid, s in stmts.items():
        found = classify.scan(s["sql"])
        try:
            converted[sid] = rules.convert(s["sql"], found)
        except rules.Declined as exc:
            declined[sid] = str(exc)

    c("every rule conversion passes its own parity gate",
      all(gates.parity_check(conv, stmts[sid])["status"] == gates.PASS
          for sid, conv in converted.items()),
      str([sid for sid, conv in converted.items()
           if gates.parity_check(conv, stmts[sid])["status"] != gates.PASS]))
    c("every rule conversion passes static",
      all(gates.static_check(conv, stmts[sid])["status"] == gates.PASS
          for sid, conv in converted.items()))
    c("a rule conversion with no target is unproven, not ready",
      all(gates.outcome(gates.run(conv, stmts[sid])) == "CONVERTED_UNPROVEN"
          for sid, conv in converted.items()))

    # ------------------------------------------------ the honest headline
    print("\nthe statement figure, not the construct figure")
    c("two of eighteen statements convert by rule", len(converted) == 2,
      f"{len(converted)}: {sorted(converted)}")
    c("sixteen are declined", len(declined) == 16, str(len(declined)))
    c("every decline carries a reason", all(len(v) > 50 for v in declined.values()))
    c("the two converted are the single-construct-family ones",
      set(converted) == {"nextCustomerId", "customersWithoutLoans"}, str(set(converted)))

    # This is the number that would be overstated by counting constructs: seven
    # statements carry a rule-tier construct and are still declined.
    mixed_count = sum(
        1 for sid, s in stmts.items()
        if sid in declined
        and any(x["tier"] == "rule" for x in classify.scan(s["sql"])))
    c("seven declined statements do carry rule-tier constructs",
      mixed_count == 7, str(mixed_count))
    c("so the construct figure overstates what converts",
      mixed_count > 0)

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


def _declines(sql: str) -> bool:
    try:
        _conv(sql)
        return False
    except rules.Declined:
        return True


if __name__ == "__main__":
    raise SystemExit(main())
