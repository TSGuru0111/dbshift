"""Offline checks for Phase 4 over AWS SCT's action items. No model, no database.

What is being checked, in order of how much it matters:

  1. **The Oracle allow-list was not widened.** A target-side fix needs
     statements the source policy must never permit. If admitting them loosened
     `remediate/policy.py`, this whole path would have made a client's
     production Oracle less safe -- which is the one outcome that would make it
     not worth having.
  2. **Nothing is ever auto-applied to the source**, and every target change
     needs a named approver -- even the automatic route, because an extension on
     a client's database is their DBA's decision.
  3. **The routing decides, not the model.** A drafted statement is gated
     against the engine its route names, and the route comes from a reviewed
     table. Two runs of the same estate must agree.
  4. Nothing here executes. A plan says `applied: false` and means it.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "remediate"

from sct import route as sct_route

from . import pg_policy, policy, sct_generate, sct_plan

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [XX] {label}\n       got  {got!r}\n       want {want!r}")


def truthy(label, got):
    check(label, bool(got), True)


def _item(code, **kw):
    base = {
        "issue_code": code, "title": f"item {code}", "complexity": "simple",
        "occurrences": 1, "owner": "DBMIG_APP", "object_name": "SOME_OBJECT",
        "object_type": "Tables", "objects": ["SOME_OBJECT"],
        "recommendation": "do the thing",
    }
    base.update(kw)
    return base


def test_policies_stay_apart():
    print("the two policies stay apart")
    # The assertion this module exists for.
    src = (Path(__file__).resolve().parent / "policy.py").read_text(encoding="utf-8")
    check("the Oracle policy does not admit CREATE EXTENSION",
          "CREATE EXTENSION" in src, False)
    check("the Oracle policy does not admit CREATE TABLE",
          "CREATE\\s+TABLE" in src or "CREATE TABLE" in src, False)
    check("the Oracle policy still refuses GRANT",
          bool(policy.check_statement("GRANT ALL ON t TO app")), True)
    check("the Oracle policy still refuses a bare SELECT",
          bool(policy.check_statement("SELECT 1")), True)

    # ...and neither imports the other's rules.
    pg_src = (Path(__file__).resolve().parent / "pg_policy.py").read_text(encoding="utf-8")
    check("the target policy does not import the Oracle one",
          "from . import policy" in pg_src, False)
    check("the Oracle policy does not import the target one",
          "pg_policy" in src, False)
    truthy("the target policy says why it is separate",
           "production Oracle" in pg_policy.describe()["why_separate"])

    # A statement legal on the target must still be refused on the source.
    ext = "CREATE EXTENSION IF NOT EXISTS postgres_fdw"
    check("CREATE EXTENSION is allowed on the target",
          pg_policy.check_statement(ext), [])
    truthy("CREATE EXTENSION is refused on the source", policy.check_statement(ext))


def test_target_policy():
    print("the target allow-list")
    for sql in ("CREATE EXTENSION IF NOT EXISTS postgres_fdw",
                "ALTER TABLE t ADD CONSTRAINT pk_t PRIMARY KEY (id)",
                "CREATE TABLE s.t (id int primary key)",
                "CREATE UNIQUE INDEX ix ON t (c)",
                "CLUSTER t USING ix"):
        check(f"allowed: {sql[:38]}", pg_policy.check_statement(sql), [])

    for sql, why in (("DROP TABLE customer", "destructive"),
                     ("TRUNCATE t", "destructive"),
                     ("DELETE FROM t", "destructive"),
                     ("UPDATE t SET x = 1", "changes rows"),
                     ("GRANT ALL ON t TO app", "privileges"),
                     ("REVOKE ALL ON t FROM app", "privileges"),
                     ("ALTER ROLE app SUPERUSER", "account"),
                     ("CREATE EXTENSION plpython3u", "untrusted language"),
                     ("COPY t FROM PROGRAM 'curl x'", "shell"),
                     ("SELECT 1", "not an allowed shape"),
                     ("", "no statement")):
        truthy(f"refused ({why}): {sql[:34] or 'empty'}", pg_policy.check_statement(sql))

    # A function body is Phase 4b's, which compiles and gates code properly.
    truthy("a dollar-quoted body is refused",
           pg_policy.check_statement(
               "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql"))

    # The rollback exemption is exactly as narrow as Oracle's.
    check("a rollback may drop the extension its fix created",
          pg_policy.rollback_undoes_own_object(
              "CREATE EXTENSION postgres_fdw", "DROP EXTENSION IF EXISTS postgres_fdw"), True)
    check("a rollback may not drop a different extension",
          pg_policy.rollback_undoes_own_object(
              "CREATE EXTENSION postgres_fdw", "DROP EXTENSION postgis"), False)
    check("a rollback may drop the constraint its fix added",
          pg_policy.rollback_undoes_own_object(
              "ALTER TABLE t ADD CONSTRAINT pk_t PRIMARY KEY (id)",
              "ALTER TABLE t DROP CONSTRAINT pk_t"), True)
    check("a rollback may not drop a different constraint",
          pg_policy.rollback_undoes_own_object(
              "ALTER TABLE t ADD CONSTRAINT pk_t PRIMARY KEY (id)",
              "ALTER TABLE t DROP CONSTRAINT fk_other"), False)


def test_template():
    print("templates")
    fix = sct_generate.template_fix(_item("5639"))
    truthy("SCT 5639 has a template", fix)
    truthy("it installs postgres_fdw", "postgres_fdw" in fix["sql"])
    truthy("it carries a rollback", fix["rollback_sql"])
    check("the template is allowed on the target",
          pg_policy.check_statement(fix["sql"]), [])
    truthy("it warns the extension alone is not the whole job",
           "not recreate the link" in fix["caveat"] or "CREATE SERVER" in fix["caveat"])
    check("an untemplated code returns nothing",
          sct_generate.template_fix(_item("9994")), None)

    # A template must win over the model: free, instant, identical every run.
    got, source = sct_generate.build_fix(
        _item("5639"), sct_route.route("5639"), model_mode="live")
    check("a template is preferred even with the model live", source, "template")
    check("...and reports no model id", got["model_id"], None)

    # With the model off, an untemplated item drafts nothing rather than guessing.
    got2, source2 = sct_generate.build_fix(
        _item("5581"), sct_route.route("5581"), model_mode="off")
    check("model off drafts nothing", got2, None)
    check("...and says so", source2, "none")


def test_prompts_differ_by_engine():
    print("prompts")
    s, t = sct_generate.SOURCE_PROMPT, sct_generate.TARGET_PROMPT
    truthy("the source prompt names Oracle", "Oracle" in s)
    truthy("the target prompt names PostgreSQL", "PostgreSQL" in t)
    truthy("the target prompt says not to write Oracle",
           "not Oracle" in t or "Write **PostgreSQL**" in t)
    truthy("the source prompt forbids DROP", "DROP" in s and "never" in s)
    truthy("the target prompt forbids DROP TABLE", "DROP TABLE" in t)
    truthy("the target prompt keeps function bodies out", "dollar-quoted" in t)
    # Both must tell the model an empty answer is correct -- otherwise it
    # invents something to be helpful, and the gates reject it anyway.
    # Match on whitespace-collapsed text: these prompts are wrapped source
    # strings, so a substring spanning a line break never matches literally.
    # The first version of this assertion failed for that reason alone, which is
    # a test bug that would have hidden a real one.
    import re as _re
    for name, p in (("source", s), ("target", t)):
        flat = _re.sub(r"\s+", " ", p)
        truthy(f"the {name} prompt says an empty answer is correct",
               "correct answer, not a failure" in flat)
        truthy(f"the {name} prompt says the output is gated",
               "Nothing you return is trusted" in flat)
        truthy(f"the {name} prompt gives the JSON shape it must reply in",
               '"rollback_sql"' in flat and '"caveat"' in flat)


def test_routing_decides():
    print("routing decides, not the model")
    # A decision route never drafts, whatever the model tier.
    e = sct_plan.plan_item(_item("9994"), model_mode="live")
    check("a decision is not drafted", e["status"], sct_plan.DECISION_REQUIRED)
    check("...and carries no statement", e["sql"], None)

    # A person route never drafts either.
    e2 = sct_plan.plan_item(_item("9996"), model_mode="live")
    check("a person-only item is not drafted", e2["status"], sct_plan.HUMAN_AUTHORED)
    check("...and carries no statement", e2["sql"], None)

    # An unmapped code lands on a person, never automatic.
    e3 = sct_plan.plan_item(_item("99999"), model_mode="live")
    check("an unmapped code goes to a person", e3["status"], sct_plan.HUMAN_AUTHORED)
    check("...and is flagged unmapped", e3["route_mapped"], False)

    # The engine a fix is gated against comes from its route.
    e4 = sct_plan.plan_item(_item("5639"))
    check("a target item is gated as postgresql", e4["engine"], "postgresql")
    gate_names = [g["gate"] for g in e4["gates"]]
    truthy("...against the target policy", "pg_policy" in gate_names)
    check("...and not against Oracle's", "policy" in gate_names, False)

    # Every plan entry explains itself without needing the routing table.
    for key in ("where", "who", "route_why", "clears_when", "sct_recommendation"):
        truthy(f"the entry carries {key}", key in e4)


def test_gates_and_approval():
    print("gates and approval")
    fix = sct_generate.template_fix(_item("5639"))

    # The Oracle static gate would reject this -- CREATE EXTENSION cannot name
    # the database link it replaces. That bug rejected the one fully automatable
    # item, so the target path has its own static check.
    check("the target static gate accepts a statement that names no object",
          sct_plan.pg_static_check(fix, _item("5639"))["status"], "pass")
    truthy("...while the Oracle gate would have refused it",
           "does not reference" in
           __import__("remediate.gates", fromlist=["x"]).static_check(
               fix, {"object_name": "DBMIG_LOOPBACK_LNK",
                     "remediation_level": "L2"})["detail"])

    # No statement, or no rollback, is still refused.
    check("no statement is refused",
          sct_plan.pg_static_check({"sql": "", "rollback_sql": "x"},
                                   _item("5639"))["status"], "fail")
    check("no rollback is refused",
          sct_plan.pg_static_check({"sql": "CREATE EXTENSION x", "rollback_sql": ""},
                                   _item("5639"))["status"], "fail")

    # Without a target the dry run blocks, and says what is missing. A gate that
    # passed vacuously here would let an unproven statement look approved.
    g = sct_plan.pg_dry_run(fix, None)
    check("no target blocks the dry run", g["status"], "blocked")
    truthy("...and says how to provide one", "run_pg.ps1" in (g["remedy"] or ""))

    # Approval is required even for the automatic route.
    no_approver = sct_plan.run_target_gates(fix, _item("5639"), None, None)
    approval = [x for x in no_approver if x["gate"] == "approval"][0]
    check("the automatic route still needs a named approver", approval["status"], "blocked")
    truthy("...and says why", "DBA's decision" in (approval["remedy"] or ""))

    with_approver = sct_plan.run_target_gates(fix, _item("5639"), None, "you@example.com")
    approval2 = [x for x in with_approver if x["gate"] == "approval"][0]
    check("a named approver satisfies it", approval2["status"], "pass")
    truthy("...recorded against the person", "you@example.com" in approval2["detail"])


def test_model_call_contract():
    print("the model call contract")
    import inspect

    from bedrock.client import BedrockClient

    # **The bug this guards.** `complete(tier, prompt)` takes the tier first,
    # positionally. Calling it `complete(prompt, tier=...)` raises TypeError,
    # which a broad `except Exception` then reported as "the model call failed"
    # -- so every item came back undrafted on a run where Bedrock was verified
    # working. A code defect wearing an outage's clothes.
    params = list(inspect.signature(BedrockClient.complete).parameters)
    check("complete() still takes tier first", params[:3], ["self", "tier", "prompt"])

    src = (Path(__file__).resolve().parent / "sct_generate.py").read_text(encoding="utf-8")
    truthy("the model is called positionally, tier first",
           'client.complete("reasoning", prompt)' in src)
    # Comments deliberately quote the wrong call to explain the bug, so search
    # code lines only -- otherwise the documentation fails the test it justifies.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    check("the old keyword call is gone in code", 'complete(prompt, tier=' in code, False)

    # A wrong call or a changed client contract must surface, not be swallowed.
    truthy("TypeError and AttributeError are re-raised, not converted",
           "except (TypeError, AttributeError):" in src)

    # The prompts are built with str.format(), so the JSON example's braces must
    # stay doubled. Single braces raise KeyError: '"sql"'.
    for name, prompt in (("source", sct_generate.SOURCE_PROMPT),
                         ("target", sct_generate.TARGET_PROMPT)):
        try:
            out = prompt.format(item="{}", route_why="w", clears_when="c")
            check(f"the {name} prompt formats without KeyError", True, True)
            check("...and leaks no doubled braces", "{{" in out or "}}" in out, False)
            truthy("...and still shows the JSON shape", '"rollback_sql"' in out)
        except KeyError as exc:
            check(f"the {name} prompt formats without KeyError", f"KeyError {exc}", "no error")


def test_reply_parsing():
    print("reply parsing")
    # The client returns {"text": ..., "model_id": ...}. A reply that is not
    # JSON, or JSON that is not an object, must route to a person rather than
    # producing a half-built fix.
    class FakeClient:
        def __init__(self, text):
            self._text = text

        def complete(self, tier, prompt, **kw):
            return {"text": self._text, "model_id": "test-model"}

    item = _item("5581")
    r = sct_route.route("5581")

    good = FakeClient('{"sql": "CREATE INDEX ix ON t (c)", "rollback_sql": '
                      '"DROP INDEX ix", "explain": "e", "caveat": "c"}')
    fix = sct_generate.bedrock_fix(item, r, client=good)
    check("a JSON reply is parsed", fix["sql"], "CREATE INDEX ix ON t (c)")
    check("the model id is recorded", fix["model_id"], "test-model")

    # Models fence JSON despite being told not to; tolerate it rather than
    # discarding an otherwise good answer.
    fenced = FakeClient(
        "```json\n"
        '{"sql": "ANALYZE t", "rollback_sql": "ANALYZE t"}\n'
        "```"
    )
    check("a fenced reply is still parsed",
          sct_generate.bedrock_fix(item, r, client=fenced)["sql"], "ANALYZE t")

    # **An empty sql is a correct answer**, not a failure -- it is what the
    # prompt asks for when no safe statement exists, and the live run produced
    # it for three of four items.
    empty = FakeClient('{"sql": "", "explain": "needs the column list first"}')
    got = sct_generate.bedrock_fix(item, r, client=empty)
    check("an empty statement is returned, not raised", got["sql"], "")
    truthy("...with the reason kept", got["explain"])

    for bad, why in ((FakeClient("not json at all"), "not JSON"),
                     (FakeClient('["a", "list"]'), "not an object")):
        try:
            sct_generate.bedrock_fix(item, r, client=bad)
            check(f"a reply that is {why} is refused", "accepted", "GenerationUnavailable")
        except sct_generate.GenerationUnavailable:
            check(f"a reply that is {why} is refused with a reason", True, True)


def test_plan_record():
    print("the plan record")
    items = [_item(c) for c in ("5639", "5984", "9994", "9996", "99999")]
    plan = sct_plan.build({"issues": items, "target": {"id": "rds-postgresql"}})

    check("nothing was applied", plan["applied"], False)
    check("the findings are SCT's", plan["source_of_findings"], "aws-sct")
    check("every item is planned", plan["totals"]["items"], len(items))
    truthy("the record names both policies", plan["policies"]["oracle"]
           and plan["policies"]["postgresql"])
    truthy("...and says the Oracle one is unchanged",
           "unchanged" in plan["policies"]["oracle"])
    truthy("grouped by where the work lands", plan["totals"]["by_where"])
    truthy("counts what needs a person", plan["totals"]["needs_a_person"] >= 3)

    # Nothing in this package may execute from the plan path.
    src = (Path(__file__).resolve().parent / "sct_plan.py").read_text(encoding="utf-8")
    check("the planner never commits", "conn.commit()" in src, False)
    truthy("the planner rolls back its dry run", "conn.rollback()" in src)


def main() -> int:
    print("remediate/sct selftest -- no model, no database, no AWS\n")
    test_policies_stay_apart()
    test_target_policy()
    test_template()
    test_prompts_differ_by_engine()
    test_routing_decides()
    test_gates_and_approval()
    test_model_call_contract()
    test_reply_parsing()
    test_plan_record()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed" + (f", {FAIL} FAILED" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
