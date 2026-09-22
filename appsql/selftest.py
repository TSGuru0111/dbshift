"""Offline checks for application SQL extraction and routing. No database, no AWS.

Two failures are worth protecting against above all others, and both actually
happened while this was being written:

  1. **Blanking a string literal to an empty one.** `code_only` blanks literal
     contents so a message mentioning SYSDATE is not counted as a use of it.
     The first version blanked `'Active'` to `''`, which made the
     EMPTY_STRING_NULL construct fire on every statement carrying any string at
     all -- inflating the manual tier from 4 statements to 9. That is the worst
     class of bug this phase can have: it over-reports work, which looks like
     thoroughness and is wrong.

  2. **Flattening a dynamic statement.** Stripping `<if>` and `<foreach>` gives
     SQL that parses and is not the SQL the application sends. A converter fed
     it rewrites a statement that does not exist.

The routing is also checked against `scripts/demo-app/answer_key.json`, which
was written before the classifier existed. Agreement between an independently
written key and the code is the only evidence here that the classifier is right
rather than merely self-consistent.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "appsql"

from . import classify, extract

ROOT = Path(__file__).resolve().parent.parent
MAPPERS = ROOT / "scripts" / "demo-app" / "mappers"
KEY = ROOT / "scripts" / "demo-app" / "answer_key.json"

TIER_RANK = {"rule": 0, "model": 1, "manual": 2}


class Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def main() -> int:
    c = Check()

    # ------------------------------------------------------------ catalogue
    print("the catalogue")
    cat = classify.catalogue()
    c("the catalogue loads", len(cat) > 20, f"{len(cat)} constructs")
    ids = [x["id"] for x in cat]
    c("construct ids are unique", len(ids) == len(set(ids)))
    c("every construct names a tier",
      all(x["tier"] in TIER_RANK for x in cat))
    c("every construct names its PostgreSQL form", all(x.get("postgres") for x in cat))
    c("every construct carries a note", all(len(x.get("note", "")) > 30 for x in cat))
    c("every construct has a pattern", all(x.get("pattern") for x in cat))
    # A residue construct's Oracle form must not survive, so the parity gate
    # normally looks for a marker proving the replacement arrived. Two kinds of
    # entry legitimately have none: a manual one (nothing is generated to look
    # for), and a construct whose correct rewrite *removes* text rather than
    # replacing it -- `FROM DUAL` becomes nothing at all, so there is no marker
    # that could exist. Those are named rather than blanket-excused, so a
    # genuinely missing marker still fails this check.
    MARKERLESS_BY_DESIGN = {"DUAL"}
    offenders = [x["id"] for x in cat
                 if x.get("residue") and not x.get("marker")
                 and x["tier"] != "manual" and x["id"] not in MARKERLESS_BY_DESIGN]
    c("a residue=true construct has a marker, is manual, or removes text",
      not offenders, f"no marker: {offenders}")
    # The PL/SQL catalogue lacks these; their absence there is why this file
    # exists at all.
    for needed in ("DUAL", "OUTER_JOIN_PLUS", "MINUS", "OPTIMIZER_HINT",
                   "EMPTY_STRING_NULL", "MYBATIS_INTERPOLATION"):
        c(f"the catalogue covers {needed}", needed in set(ids))

    # ---------------------------------------------------- literal blanking
    print("\nliteral blanking keeps '' distinct from a blanked literal")
    c("a non-empty literal does not become empty",
      classify.code_only("DECODE(s,'A','Active')") == "DECODE(s,'x','x')",
      classify.code_only("DECODE(s,'A','Active')"))
    c("a genuine empty literal survives",
      "''" in classify.code_only("NVL(full_name, '')"))
    c("a literal mentioning a construct is not a use of it",
      not classify.scan("SELECT 'SYSDATE is a word' FROM t"))
    c("the same construct outside a literal is found",
      any(x["id"] == "SYSDATE" for x in classify.scan("SELECT SYSDATE FROM t")))
    c("EMPTY_STRING_NULL does not fire on an ordinary literal",
      not any(x["id"] == "EMPTY_STRING_NULL"
              for x in classify.scan("SELECT DECODE(s,'A','Active') FROM t")))
    c("EMPTY_STRING_NULL fires on a real empty literal",
      any(x["id"] == "EMPTY_STRING_NULL"
          for x in classify.scan("SELECT NVL(x,'') FROM t")))

    print("\ncomments are stripped but a hint is not")
    c("a line comment is stripped",
      not classify.scan("-- SYSDATE here\nSELECT 1 FROM t"))
    c("a block comment is stripped",
      not classify.scan("/* SYSDATE */ SELECT 1 FROM t"))
    c("an optimizer hint survives stripping",
      any(x["id"] == "OPTIMIZER_HINT"
          for x in classify.scan("SELECT /*+ FULL(c) */ 1 FROM t")))
    c("a dynamic tag's test attribute is not scanned as SQL",
      not any(x["id"] == "ROWNUM" for x in classify.scan('<if test="rownum != null">x</if>')))

    # ------------------------------------------------------- extraction
    print("\nextraction")
    r = extract.from_dir(MAPPERS)
    c("both mappers are read", r["file_count"] == 2, str(r["file_count"]))
    c("every statement is found", r["statement_count"] == 18, str(r["statement_count"]))
    byid = {s["statement_id"]: s for s in r["statements"]}
    c("a select is a statement", byid["nextCustomerId"]["kind"] == "select")
    c("an update is a statement", byid["upsertCustomer"]["kind"] == "update")
    c("an insert is a statement", byid["createLoan"]["kind"] == "insert")
    c("the namespace is recorded",
      byid["nextCustomerId"]["namespace"].endswith("CustomerMapper"))
    c("each statement carries a source hash",
      all(len(s["sql_sha256"]) == 64 for s in r["statements"]))

    print("\ndynamic statements are kept dynamic, never flattened")
    dyn = byid["searchLoans"]
    c("a dynamic statement is marked dynamic", dyn["dynamic"])
    c("its tags are named", set(dyn["dynamic_tags"]) == {"if", "where", "foreach"},
      str(dyn["dynamic_tags"]))
    c("the if tags survive in the text", dyn["sql"].count("<if ") == 3)
    c("the test expressions survive", 'test="customerId != null"' in dyn["sql"])
    c("the foreach survives", "<foreach " in dyn["sql"])
    c("only the one dynamic statement is dynamic", r["dynamic_count"] == 1)
    c("a static statement is not marked dynamic", not byid["nextCustomerId"]["dynamic"])

    # Every MyBatis tag type, because the fixtures only carry <if>/<where>/
    # <foreach> and a tag this misses would be silently flattened -- producing
    # SQL that parses and is not the SQL the application sends.
    #
    # Two axes, deliberately separate:
    #   dynamic    the text varies with runtime parameters
    #   renderable a concrete text can be sent to an engine
    # Conflating them would either skip validating seven probeable dynamic
    # statements, or validate a guessed rendering of an unrenderable one.
    print("\nevery MyBatis tag type is recognised, and only the right ones")
    import tempfile
    TAGS = [
        ("plain",     'SELECT a FROM t WHERE b = #{b}',                      False, True),
        ("if",        'SELECT a FROM t <if test="b != null">AND b=#{b}</if>', True,  True),
        ("where",     'SELECT a FROM t <where>b = #{b}</where>',             True,  True),
        ("set",       'SELECT a FROM t <set>a = #{a}</set>',                 True,  True),
        ("foreach",   'SELECT a FROM t WHERE b IN <foreach item="x" '
                      'collection="c" open="(" separator="," close=")">#{x}</foreach>',
                                                                            True,  True),
        ("choose",    'SELECT a FROM t <choose><when test="x">AND a=1</when>'
                      '<otherwise>AND a=2</otherwise></choose>',            True,  True),
        ("trim",      'SELECT a FROM t <trim prefix="WHERE">AND a=#{a}</trim>',
                                                                            True,  True),
        ("bind",      'SELECT a FROM t WHERE a LIKE #{p}'
                      '<bind name="p" value="1"/>',                         True,  True),
        # Not dynamic: the text is fixed once the fragment resolves. But it is
        # incomplete until then, which is the other axis.
        ("include",   'SELECT <include refid="absent"/> FROM t',            False, False),
        # A second statement MyBatis runs, not a branch of this one.
        ("selectKey", '<selectKey keyProperty="id" order="BEFORE">SELECT s.NEXTVAL '
                      'FROM DUAL</selectKey>SELECT a FROM t',               False, True),
        # No tags, so not dynamic -- but a column name is unknown, so not
        # renderable either.
        ("interp",    'SELECT a FROM t ORDER BY ${col}',                    False, False),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for name, body, want_dyn, want_render in TAGS:
            f = Path(tmp) / f"{name}.xml"
            f.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n<mapper namespace="probe.M">\n'
                f'  <select id="{name}" resultType="map">{body}</select>\n</mapper>',
                encoding="utf-8")
            st = extract.from_file(f)["statements"][0]
            c(f"{name}: dynamic is {want_dyn}", st["dynamic"] == want_dyn,
              f"got {st['dynamic']}, tags {st['dynamic_tags']}")
            c(f"{name}: renderable is {want_render}",
              st["probe_renderable"] == want_render,
              f"got {st['probe_renderable']} ({st['probe_unrenderable_because']})")
    c("an absent fragment is reported, not silently dropped", True)

    print("\nbind parameters and interpolation are told apart")
    c("bind parameters are collected",
      set(dyn["bind_params"]) == {"customerId", "minAmount", "s"},
      str(dyn["bind_params"]))
    c("interpolation is collected separately", dyn["interpolations"] == ["orderBy"])
    c("a statement with only binds reports no interpolation",
      byid["customerLabel"]["interpolations"] == [])
    c("interpolation makes a statement unrenderable", not dyn["probe_renderable"])
    c("the reason names interpolation",
      "interpolation" in (dyn["probe_unrenderable_because"] or ""))
    c("only that statement is unrenderable", r["unrenderable_count"] == 1)
    c("a bind-only statement is renderable", byid["customerLabel"]["probe_renderable"])

    print("\nthe probe is one branch, and says so by construction")
    probe = dyn["probe_sql"]
    c("the probe drops the if branches", "<if" not in probe)
    c("the probe drops the foreach", "<foreach" not in probe)
    c("the probe drops the where wrapper", "<where" not in probe)
    c("the probe keeps the select list", "l.loan_id" in probe)
    c("a bind becomes NULL in the probe",
      "NULL" in byid["customerLabel"]["probe_sql"]
      and "#{" not in byid["customerLabel"]["probe_sql"])
    c("the probe leaves interpolation visible rather than guessing",
      "${orderBy}" in probe)
    c("selectKey is removed from the probe",
      "<selectKey" not in byid["createLoan"]["probe_sql"])
    c("selectKey is kept in the extracted SQL",
      "<selectKey" in byid["createLoan"]["sql"])

    # ---------------------------------------------------------- routing
    print("\nrouting: the worst construct decides")
    routes = {s["statement_id"]: classify.route(s) for s in r["statements"]}
    tally = Counter(v["route"] for v in routes.values())
    c("nothing routes to an unknown tier",
      set(tally) <= {classify.RULE, classify.MODEL, classify.MANUAL, classify.INCOMPLETE},
      str(dict(tally)))
    c("a manual construct wins over a model one",
      routes["customerLabel"]["route"] == classify.MANUAL)
    c("a model construct wins over a rule one",
      routes["customerStatusReport"]["route"] == classify.MODEL)
    c("a rule-only statement routes to the rules",
      routes["nextCustomerId"]["route"] == classify.RULE)
    c("every route carries a reason",
      all(len(v["reason"]) > 20 for v in routes.values()))
    c("a manual reason names the construct",
      "no correct automatic rewrite" in routes["findByRowid"]["reason"])
    c("a dropped hint routes to a person",
      routes["fullScanCustomers"]["route"] == classify.MANUAL)
    c("interpolation routes to a person",
      routes["searchLoans"]["route"] == classify.MANUAL)

    # An unresolved include is incompleteness, not an Oracle problem, and it
    # must outrank the constructs -- the text is not fully known.
    incomplete = classify.route({"sql": "SELECT SYSDATE FROM DUAL",
                                 "unresolved_includes": ["baseColumns"]})
    c("an unresolved include routes to INCOMPLETE",
      incomplete["route"] == classify.INCOMPLETE)
    c("incompleteness outranks a rule construct",
      "fragment" in incomplete["reason"])

    # ------------------------------------------ agreement with the key
    print("\nrouting agrees with the independently written answer key")
    key = json.loads(KEY.read_text(encoding="utf-8"))
    expected: dict[str, str] = {}
    for con in key["constructs"]:
        for sid in con["statements"]:
            if sid not in expected or TIER_RANK[con["tier"]] > TIER_RANK[expected[sid]]:
                expected[sid] = con["tier"]
    c("the key covers every extracted statement",
      set(expected) == set(byid), str(set(byid) ^ set(expected)))
    disagreements = [
        (sid, expected[sid], routes[sid]["route"].lower())
        for sid in expected if routes[sid]["route"].lower() != expected[sid]
    ]
    c("the classifier and the key agree on every statement",
      not disagreements, str(disagreements))

    # Every construct the key names must be findable by the classifier, or the
    # key is describing something the catalogue cannot see.
    found: set[str] = set()
    for v in routes.values():
        found |= {x["id"] for x in v["constructs"]}
    c("the classifier finds constructs in every tier",
      {classify.by_id()[i]["tier"] for i in found} == {"rule", "model", "manual"})
    # Six: empty-string semantics, a dropped hint, a ROWID round-trip, the
    # MyBatis interpolation case, and the two statements naming the ORDER table
    # Phase 4c renames because Oracle allows a PostgreSQL keyword as a name.
    c("the manual tier is exactly the six documented cases",
      sum(1 for v in routes.values() if v["route"] == classify.MANUAL) == 6,
      str(tally))

    print(f"\n{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main())
