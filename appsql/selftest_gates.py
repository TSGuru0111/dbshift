"""Offline checks for the five application SQL gates. No database, no AWS.

The target and the result comparison are stubbed, so every path runs here --
including the ones that need two engines -- without a connection.

What these checks defend, in order of how much damage the failure would do:

  1. **A pass is never reported for something that was not established.** With
     no target, parse and result report *blocked* and the outcome is
     CONVERTED_UNPROVEN, never READY_FOR_APPROVAL. A client told a statement is
     ready when nothing ran against an engine has been told something untrue,
     and `sizing/target.py` already refuses to treat converted-but-uncompiled
     stored code as costed evidence -- the same discipline, applied to queries.

  2. **`${}` is never turned into `#{}`.** The commonest way an automated
     rewrite breaks a MyBatis mapper: a column name arrives as a quoted
     literal and the query fails at runtime, not at conversion.

  3. **A construct cannot be silently dropped.** Parity is the gate that needs
     no database and catches what every text-level translator gets wrong.

  4. **A failed gate stops the run.** A later pass after a known failure reads
     as reassurance about a conversion already known to be wrong.

  5. **The gate calls a method the real target actually has.** The parse gate
     shipped calling `target.parses(sql)`, which `convert.target.PgTarget` has
     never had. Every check here passed, because the stub below *did* have it,
     and the first run with a PostgreSQL registered raised AttributeError in
     the browser. A stub that invents an interface tests nothing but itself, so
     the contract is now asserted against the real class as well.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "appsql"

from . import classify, extract, gates

MAPPERS = Path(__file__).resolve().parent.parent / "scripts" / "demo-app" / "mappers"


class _Target:
    """A PostgreSQL that accepts or rejects, and counts what it was asked."""

    def __init__(self, ok=True, detail="ok"):
        self.ok, self.detail, self.seen = ok, detail, []

    def parses(self, sql):
        self.seen.append(sql)
        return self.ok, self.detail


def _conv(sql, constructs):
    return {"sql": sql, "constructs": constructs, "probe_sql": sql}


def _t(cid, state="translated", reason=None, postgres=None):
    e = {"id": cid, "state": state}
    if reason:
        e["reason"] = reason
    if postgres:
        e["postgres"] = postgres
    return e


class Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = Check()
    r = extract.from_dir(MAPPERS)
    byid = {s["statement_id"]: s for s in r["statements"]}

    nextid = byid["nextCustomerId"]
    page = byid["pageCustomers"]
    search = byid["searchLoans"]
    good = _conv("SELECT nextval('customer_seq')", [_t("DUAL"), _t("SEQ_NEXTVAL")])

    # ------------------------------------------------------------- static
    print("static: one statement, same kind, binds and interpolation intact")
    c("a clean conversion passes",
      gates.static_check(good, nextid)["status"] == gates.PASS)
    c("empty SQL fails",
      gates.static_check(_conv("", []), nextid)["status"] == gates.FAIL)
    c("text that is not a statement fails",
      gates.static_check(_conv("customer_seq", []), nextid)["status"] == gates.FAIL)
    c("no construct accounting fails",
      gates.static_check({"sql": "SELECT 1"}, nextid)["status"] == gates.FAIL)

    g = gates.static_check(_conv("UPDATE customer SET status = NULL", [_t("ROWNUM")]), page)
    c("a select converted into an update fails", g["status"] == gates.FAIL)
    c("the kind change is named", "into a update" in g["detail"])

    # A CTE is still a select: the CONNECT BY rewrite produces WITH RECURSIVE,
    # and reading the first keyword naively would reject every one of them.
    chain = byid["loanApprovalChain"]
    g = gates.static_check(
        _conv("WITH RECURSIVE t AS (SELECT 1) SELECT * FROM t WHERE x = #{rootLoanId}",
              [_t("CONNECT_BY")]), chain)
    c("a WITH RECURSIVE rewrite is still a select", g["status"] == gates.PASS, g["detail"])

    # Both of these were real false positives on the first live Bedrock run:
    # the model produced correct SQL and the gates rejected it. A gate that
    # flags a correct conversion is worse than one that misses a wrong one,
    # because it teaches a reader to ignore the gate.
    create = byid["createLoan"]
    g = gates.static_check(
        _conv('<selectKey keyProperty="loanId" resultType="long" order="BEFORE">\n'
              "SELECT nextval('loan_seq')\n</selectKey>\n"
              "INSERT INTO loan (loan_id, customer_id, principal_amt, interest_rate, "
              "loan_status, disbursed_on) VALUES (#{loanId}, #{customerId}, "
              "#{principalAmt}, #{interestRate}, 'P', "
              "date_trunc('second', LOCALTIMESTAMP))",
              [_t("DUAL"), _t("SEQ_NEXTVAL"), _t("SYSDATE"),
               _t("SELECTKEY_SEQUENCE", "not_translated", "MyBatis keeps selectKey")]),
        create)
    c("a kept <selectKey> does not hide the statement keyword",
      g["status"] == gates.PASS, g["detail"])

    g = gates.static_check(
        _conv("WITH RECURSIVE t AS (SELECT 1) SELECT * FROM t WHERE x = #{rootLoanId}",
              [_t("CONNECT_BY")]), chain)
    c("a leading CTE does not hide the statement keyword", g["status"] == gates.PASS)

    g = gates.static_check(
        _conv("SELECT customer_id FROM customer LIMIT 10", [_t("ROWNUM")]), page)
    c("a dropped bind parameter fails", g["status"] == gates.FAIL)
    c("the dropped binds are named",
      "endRow" in g["detail"] and "startRow" in g["detail"])

    g = gates.static_check(
        _conv("SELECT l.loan_id FROM loan l ORDER BY #{orderBy}",
              [_t("TO_NUMBER"), _t("MYBATIS_INTERPOLATION", "not_translated", "stays interpolated")]),
        search)
    c("turning ${} into #{} fails", g["status"] == gates.FAIL)
    c("the interpolation mistake is explained",
      "column name into a literal" in g["detail"])

    g = gates.static_check(
        _conv("SELECT l.loan_id FROM loan l WHERE l.customer_id = #{customerId} "
              "AND l.principal_amt >= CAST(#{minAmount} AS numeric) AND l.loan_status "
              "IN (#{s}) ORDER BY ${orderBy}",
              [_t("TO_NUMBER"), _t("MYBATIS_INTERPOLATION", "not_translated", "stays interpolated")]),
        search)
    c("keeping ${} interpolated passes", g["status"] == gates.PASS, g["detail"])

    # ------------------------------------------------------------- parity
    print("\nparity: nothing dropped, no Oracle residue")
    c("a clean conversion passes parity",
      gates.parity_check(good, nextid)["status"] == gates.PASS)

    g = gates.parity_check(
        _conv("SELECT nextval('customer_seq') FROM DUAL", [_t("DUAL"), _t("SEQ_NEXTVAL")]),
        nextid)
    c("surviving Oracle residue fails", g["status"] == gates.FAIL)
    c("the surviving construct is named", "FROM DUAL" in g["detail"])

    g = gates.parity_check(_conv("SELECT nextval('customer_seq')", [_t("SEQ_NEXTVAL")]), nextid)
    c("an unaccounted construct fails", g["status"] == gates.FAIL)
    c("the unaccounted construct is named", "DUAL" in g["detail"])

    g = gates.parity_check(
        _conv("SELECT customer_seq.NEXTVAL", [_t("DUAL"), _t("SEQ_NEXTVAL")]), nextid)
    c("claiming translated without the PostgreSQL form fails", g["status"] == gates.FAIL)
    c("the false claim is named", "form is absent" in g["detail"])

    g = gates.parity_check(
        _conv("SELECT nextval('customer_seq')",
              [_t("DUAL"), {"id": "SEQ_NEXTVAL", "state": "not_translated"}]), nextid)
    c("not_translated without a reason fails", g["status"] == gates.FAIL)
    g = gates.parity_check(
        _conv("SELECT customer_seq.NEXTVAL",
              [_t("DUAL"), _t("SEQ_NEXTVAL", "not_translated", "the sequence is created later")]),
        nextid)
    c("not_translated with a reason is allowed to keep the Oracle form",
      g["status"] == gates.PASS, g["detail"])
    c("the reason is surfaced", "not translated" in g["detail"])

    g = gates.parity_check(_conv("SELECT nextval('x')", [{"id": "DUAL"}, _t("SEQ_NEXTVAL")]), nextid)
    c("a construct with no state fails", g["status"] == gates.FAIL)

    # The second live-run false positive. TO_CHAR and to_char are the same
    # name in two engines, so a case-insensitive residue check rejected the
    # model for emitting the correct PostgreSQL form.
    print("\nresidue is case-sensitive where only case distinguishes the engines")
    report = byid["customerStatusReport"]
    pg_form = _conv(
        "SELECT c.customer_id, CASE c.status WHEN 'A' THEN 'Active' ELSE 'Unknown' END, "
        "to_char(c.created_at, 'DD-Mon-YYYY') FROM customer c "
        "WHERE c.created_at >= to_date(#{fromDate}, 'YYYY-MM-DD')",
        [_t("DECODE", postgres="CASE"), _t("TO_CHAR_FORMAT", postgres="to_char"),
         _t("TO_DATE", postgres="to_date")])
    c("the lower-case PostgreSQL form passes",
      gates.parity_check(pg_form, report)["status"] == gates.PASS,
      gates.parity_check(pg_form, report)["detail"])

    oracle_form = _conv(
        "SELECT c.customer_id, CASE c.status WHEN 'A' THEN 'Active' ELSE 'Unknown' END, "
        "TO_CHAR(c.created_at, 'DD-MON-YYYY') FROM customer c "
        "WHERE c.created_at >= to_date(#{fromDate}, 'YYYY-MM-DD')",
        [_t("DECODE", postgres="CASE"), _t("TO_CHAR_FORMAT", postgres="to_char"),
         _t("TO_DATE", postgres="to_date")])
    g = gates.parity_check(oracle_form, report)
    c("the upper-case Oracle form still fails", g["status"] == gates.FAIL, g["detail"])
    c("the surviving Oracle construct is named", "TO_CHAR" in g["detail"])

    # residue=false: the Oracle spelling is legal PostgreSQL, so it may stay.
    label = byid["customerLabel"]
    # `||` is residue=false: it is legal PostgreSQL, so the Oracle spelling may
    # survive and the defect is semantic. NVL is residue=true and claims
    # translated, so COALESCE must actually be present -- a conversion that
    # rewrites || into concat() and silently drops the NVL is a parity failure,
    # which is what the first draft of this check accidentally asserted was
    # fine.
    g = gates.parity_check(
        _conv("SELECT COALESCE(c.full_name, '') || ' <' || COALESCE(c.email, '') || '>' "
              "FROM customer c WHERE c.customer_id = #{customerId}",
              [_t("CONCAT_PIPE", "not_translated",
                  "|| is legal PostgreSQL; the NULL semantics differ and a person decides"),
               _t("NVL"),
               _t("EMPTY_STRING_NULL", "not_translated", "no rewrite exists")]),
        label)
    c("a residue=false construct need not vanish", g["status"] == gates.PASS, g["detail"])
    c("a residue=true construct in the same statement still needs its form",
      gates.parity_check(
          _conv("SELECT concat(c.full_name, c.email) FROM customer c "
                "WHERE c.customer_id = #{customerId}",
                [_t("CONCAT_PIPE", "not_translated", "concat() ignores NULLs"),
                 _t("NVL"),
                 _t("EMPTY_STRING_NULL", "not_translated", "no rewrite exists")]),
          label)["status"] == gates.FAIL)

    # The contract a stub cannot check: whatever the real target class offers,
    # the gate must be able to drive it. Asserted without connecting -- this is
    # about the interface, not the engine.
    print("\nthe parse gate matches the real target's interface")
    from convert.target import PgTarget
    real = PgTarget.parse("localhost:5432/dbshift", "dbshift", "x")
    c("the real target has no parses() method", not hasattr(real, "parses"),
      "it has one now -- _parses_on should prefer it")
    c("the real target offers connect()", callable(getattr(real, "connect", None)))
    c("the gate has a path for a target without parses()",
      callable(getattr(gates, "_parses_on", None)))
    c("a stub with parses() is still honoured",
      gates._parses_on(_Target(ok=True, detail="stubbed"), "SELECT 1")
      == (True, "stubbed"))
    # An engine error must reach the screen as the engine's own sentence, not
    # pg8000's wire dict.
    _err = Exception({"S": "ERROR", "C": "42P01", "M": 'relation "x" does not exist'})
    c("a pg8000 error dict becomes a readable sentence",
      gates._pg_message(_err) == 'relation "x" does not exist [42P01]',
      gates._pg_message(_err))
    c("a plain exception still reports something",
      "refused" in gates._pg_message(RuntimeError("connection refused")))

    # -------------------------------------------------------------- parse
    print("\nparse: needs an engine, blocked without one")
    c("no target blocks rather than passes",
      gates.parse_check(good, nextid, None)["status"] == gates.BLOCKED)
    c("the block explains what is missing",
      "no PostgreSQL target" in gates.parse_check(good, nextid, None)["detail"])

    t = _Target(ok=True)
    g = gates.parse_check(good, nextid, t)
    c("a target that accepts passes", g["status"] == gates.PASS)
    c("the probe was actually sent", len(t.seen) == 1)

    g = gates.parse_check(good, page, _Target(ok=True))
    c("a dynamic statement says only one branch was checked",
      "one branch" in g["detail"] or not page["dynamic"])

    t2 = _Target(ok=False, detail='column "created_at" does not exist')
    g = gates.parse_check(good, nextid, t2)
    c("a target that rejects fails", g["status"] == gates.FAIL)
    c("the engine's message is kept", "does not exist" in g["detail"])

    # An unrenderable statement is blocked even with a target: guessing a
    # column name would validate a query the application never sends.
    g = gates.parse_check(good, search, _Target(ok=True))
    c("interpolation blocks the parse even with a target", g["status"] == gates.BLOCKED)
    c("the reason names renderability", "not renderable" in g["detail"])

    # ------------------------------------------------------------- result
    print("\nresult: the only gate that catches a rewrite returning different rows")
    c("no comparison blocks rather than passes",
      gates.result_check(good, nextid, None)["status"] == gates.BLOCKED)
    c("the block says why it matters",
      "different rows" in (gates.result_check(good, nextid, None)["remedy"] or ""))

    g = gates.result_check(good, nextid, {"rows_compared": 60000, "mismatches": [],
                                          "expected_differences": []})
    c("identical rows pass", g["status"] == gates.PASS)
    c("the row count is reported", "60000" in g["detail"])

    g = gates.result_check(good, nextid, {
        "rows_compared": 100, "mismatches": [],
        "expected_differences": [{"kind": "empty_string_is_null"}]})
    c("an expected difference still passes", g["status"] == gates.PASS)
    c("the expected difference is reported, not hidden",
      "empty_string_is_null" in g["detail"])

    g = gates.result_check(good, nextid, {
        "rows_compared": 100,
        "mismatches": ["row 7: status A vs C"], "expected_differences": []})
    c("an unexplained difference fails", g["status"] == gates.FAIL)
    c("the first mismatch is shown", "row 7" in g["detail"])

    g = gates.result_check(good, nextid, {"not_comparable": "the source refused the query"})
    c("an unreadable side blocks rather than passing", g["status"] == gates.BLOCKED)
    c("an unread side is never counted as equal",
      "not comparable" in g["detail"])

    # ----------------------------------------------------------- approval
    print("\napproval: a named person, always")
    c("no approver blocks",
      gates.approval_check(good, None)["status"] == gates.BLOCKED)
    c("a named approver passes",
      gates.approval_check(good, "dba@client.example")["status"] == gates.PASS)
    c("the approver is recorded",
      "dba@client.example" in gates.approval_check(good, "dba@client.example")["detail"])

    # ------------------------------------------------------------ runner
    print("\nthe run stops at a failure but not at a block")
    gs = gates.run(good, nextid)
    c("every gate runs when none fails", len(gs) == len(gates.ORDER))
    c("a block does not stop the run",
      [g["gate"] for g in gs] == list(gates.ORDER))

    gs = gates.run(_conv("SELECT nextval('x') FROM DUAL", [_t("DUAL"), _t("SEQ_NEXTVAL")]), nextid)
    c("a failure stops the run", len(gs) == 2)
    c("the gates after a failure are absent",
      not any(g["gate"] in ("parse", "result", "approval") for g in gs))

    print("\nthe outcome never overclaims")
    c("no target means converted but unproven",
      gates.outcome(gates.run(good, nextid)) == "CONVERTED_UNPROVEN")
    c("a residue failure is rejected",
      gates.outcome(gates.run(
          _conv("SELECT nextval('x') FROM DUAL", [_t("DUAL"), _t("SEQ_NEXTVAL")]),
          nextid)) == "REJECTED")

    full = {"rows_compared": 60000, "mismatches": [], "expected_differences": []}
    c("parsed and compared means ready for approval",
      gates.outcome(gates.run(good, nextid, _Target(), full)) == "READY_FOR_APPROVAL")
    c("an approver makes it approved",
      gates.outcome(gates.run(good, nextid, _Target(), full,
                              approved_by="dba@client.example")) == "APPROVED")
    c("a parsed statement with no comparison is still unproven",
      gates.outcome(gates.run(good, nextid, _Target())) == "CONVERTED_UNPROVEN")

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
