"""Self-test for Phase 3 on a MySQL source: RDS for MySQL or RDS for PostgreSQL.

Offline. The facts are built by hand and the model is a stub, so nothing here
needs a database or Bedrock. What is worth proving is the reasoning, and three
parts of it differ from the Oracle path in substance rather than in labels:

  1. **There is no licence saving.** From Oracle, PostgreSQL's conversion effort
     is weighed against ending an Oracle bill. MySQL and PostgreSQL are both open
     source, so the recommender must not reach for that argument -- and a model
     prompt must be told so, or it will.
  2. **The evidence is MySQL's own.** MyISAM, utf8mb3, ENUM/SET, unsigned BIGINT,
     case-insensitive collations. Oracle's feature-usage catalogue plays no part.
  3. **RDS for Oracle is not a path.** A known target is not a legal one.

    python -m sizing.selftest_mysql
"""

from __future__ import annotations

import json
import sys

from sizing import propose, propose_target as pt, target, validate, validate_target


class _Check:
    def __init__(self):
        self.n = self.ok = 0

    def __call__(self, name, cond, detail=""):
        self.n += 1
        self.ok += bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class _Stub:
    """A Bedrock client that answers with a fixed payload and records the prompt."""

    def __init__(self, payload=None, raise_error=None):
        self.payload, self.raise_error, self.calls, self.prompt = payload, raise_error, 0, ""

    def complete(self, tier, prompt, max_tokens=None):
        self.calls += 1
        self.prompt = prompt
        if self.raise_error:
            from bedrock.client import BedrockError
            raise BedrockError(self.raise_error)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return {"text": text, "model_id": "stub-model", "input_tokens": 1, "output_tokens": 1}


def _facts(**mysql):
    """MySQL facts shaped like sizing/facts.extract() produces them."""
    m = {"non_innodb_tables": 1, "utf8mb3_columns": 2, "case_insensitive_columns": 57,
         "unsigned_bigint_columns": 29, "unsigned_bigint_values": 6,
         "unsigned_bigint_value_names": ["ledger_entry.balance_minor"],
         "enum_set_columns": 5, "json_columns": 1, "tables_without_pk": 1,
         "routines": 5, "triggers": 2, "events": 1, "fulltext_indexes": 1,
         "unrecreatable_definers": 6}
    m.update(mysql)
    return {
        "source_engine": "MYSQL", "mysql": m,
        "segment_bytes": 37_000_000, "segment_gb": 0.035,
        "table_count": 20, "object_count": 62,
        "features_detected": [], "structural": {"partitioned_tables": 1},
        "character_set": "utf8mb4", "supplemental_logging": "YES",
        "utilization": {"available": False, "basis": "capacity",
                        "reason": "MySQL keeps no workload history", "measured": None},
    }


def main(argv=None) -> int:
    c = _Check()

    print("the two paths from MySQL")
    a = target.assess(_facts())
    c("the paths are RDS for MySQL and RDS for PostgreSQL, and nothing else",
      set(a["paths"]) == {"MYSQL", "POSTGRESQL"}, str(list(a["paths"])))
    c("RDS for Oracle is NOT assessed from a MySQL source", "ORACLE" not in a["paths"])
    c("the assessment records its source engine", a.get("source_engine") == "MYSQL")
    my, pg = a["paths"]["MYSQL"], a["paths"]["POSTGRESQL"]
    c("both paths are open on this estate", my["possible"] and pg["possible"])
    c("the homogeneous path costs less", my["effort_points"] < pg["effort_points"],
      f"{my['effort_points']} vs {pg['effort_points']}")

    subj = lambda p: " | ".join(i["subject"] for i in p["effort"])   # noqa: E731
    for thing in ("non-InnoDB", "utf8mb3", "without a primary key"):
        c(f"'{thing}' is pre-migration work on BOTH paths -- leaving it off the "
          "homogeneous one would make that path look free",
          thing in subj(my) and thing in subj(pg))
    c("definer-rights objects cost the MySQL path (RDS grants no SUPER)",
      "definer-rights" in subj(my))
    for thing in ("ENUM or SET", "unsigned BIGINT", "case-insensitive", "FULLTEXT",
                  "stored routine"):
        c(f"'{thing}' costs only the PostgreSQL path", thing in subj(pg) and thing not in subj(my))
    c("no Oracle feature-usage item appears on either path",
      "DBA_FEATURE_USAGE" not in json.dumps(a))

    print()
    print("unsigned BIGINT is weighed by VALUE columns, not every identity")
    ub = [i for i in pg["effort"] if "BIGINT" in i["subject"]][0]
    c("the weight is the value-column count (6), not all unsigned BIGINTs (29)",
      ub["weight"] == 6, str(ub["weight"]))
    c("the finding names the columns to check", "ledger_entry.balance_minor" in ub["detail"])
    c("and says why the identities are not weighed", "identities" in ub["detail"])
    none = target.assess(_facts(unsigned_bigint_values=0, unsigned_bigint_value_names=[]))
    c("an estate whose unsigned BIGINTs are all identities carries no such item",
      not any("BIGINT" in i["subject"] for i in none["paths"]["POSTGRESQL"]["effort"]))

    print()
    print("the recommendation: no licence saving, so the homogeneous path by default")
    r = a["recommended"]
    c("recommends RDS for MySQL", r["target"] == "MYSQL", str(r["target"]))
    c("with high confidence -- recommending it rests on converting nothing",
      r["confidence"] == pt.HIGH)
    c("the reason says there is no licence saving", "no licence" in r["reason"])
    c("the reason NEVER claims PostgreSQL ends a licence",
      "ends the Oracle" not in r["reason"] and "licence saving" not in r["reason"].replace("no licence saving", ""))
    c("the reason names what WOULD justify PostgreSQL, so a client with such a "
      "reason knows it was not weighed", "platform standard" in r["reason"])
    blocked = dict(a["paths"])
    blocked["MYSQL"] = dict(my, possible=False,
                            blockers=[{"subject": "a blocker", "kind": "blocker"}])
    rb = pt.heuristic_target_proposal_mysql(blocked, a["stored_code"])
    c("a blocked MySQL path recommends PostgreSQL", rb["target"] == "POSTGRESQL")

    print()
    print("choosing")
    ch = target.choose("POSTGRESQL", a, chosen_by="t@example.com")
    c("PostgreSQL may be chosen AGAINST the recommendation", ch["target"] == "POSTGRESQL")
    c("...and the disagreement is recorded, not hidden",
      ch["agreed_with_recommendation"] is False)
    refused = ""
    try:
        target.choose("ORACLE", a)
    except ValueError as exc:
        refused = str(exc)
    c("RDS for Oracle is refused as not a path from this source",
      "not a migration path" in refused, refused[:80])

    print()
    print("sizing RDS for MySQL: capacity only, no licence arithmetic")
    prop = propose.propose(_facts())
    c("the heuristic proposal carries NO edition on MySQL", prop["edition"] is None)
    d = validate.validate(prop, _facts(), engine="MYSQL")
    c("the decision is for RDS for MySQL", d["engine"] == "MYSQL")
    c("no edition, no licence model, no processor licences",
      d["edition"] is None and d["licence_model"] is None and d["processor_licences"] == 0)
    ed = [x for x in d["checks"] if x["check"] == "edition"][0]
    c("the edition check says why there is none", "no editions" in ed["detail"])
    enc = [x for x in d["checks"] if x["check"] == "encoding"][0]
    c("utf8mb3 columns WARN even on a utf8mb4 server -- those are the ones that "
      "already truncated", enc["verdict"] == "WARN" and "utf8mb3" in enc["detail"])
    clean = validate.validate(prop, _facts(utf8mb3_columns=0), engine="MYSQL")
    c("a clean utf8mb4 estate passes the encoding check",
      [x for x in clean["checks"] if x["check"] == "encoding"][0]["verdict"] == "PASS")
    c("storage has a floor", d["storage_gb"] >= 20)

    print()
    print("sizing RDS for PostgreSQL from MySQL")
    dp = validate.validate(prop, _facts(), engine="POSTGRESQL")
    encp = [x for x in dp["checks"] if x["check"] == "encoding"][0]
    c("utf8mb4 maps to PostgreSQL UTF8 cleanly -- no false transcoding warning",
      encp["verdict"] == "PASS", encp["detail"][:70])
    c("no Oracle wording leaks into the PostgreSQL decision",
      "Oracle" not in json.dumps(dp["checks"]))

    print()
    print("the model tier, bounded exactly as on Oracle")
    stub = _Stub({"target": "MYSQL", "confidence": "high",
                  "reason": "RDS for MySQL carries 10 effort points against 26."})
    out = pt.propose_target(a["paths"], a["stored_code"], use_bedrock=True, client=stub)
    c("the model is consulted", stub.calls == 1)
    c("its answer is used", out["source"] == "bedrock" and out["target"] == "MYSQL")
    c("the prompt tells the model there is NO licence saving -- otherwise it reaches "
      "for the Oracle argument", "ends no licence" in stub.prompt)
    c("the prompt offers MYSQL and POSTGRESQL, not ORACLE",
      '"MYSQL" or "POSTGRESQL"' in stub.prompt and "ORACLE" not in stub.prompt.split("EVIDENCE")[0])

    bad = pt.propose_target(a["paths"], a["stored_code"], use_bedrock=True,
                            client=_Stub({"target": "ORACLE", "confidence": "high", "reason": "x"}))
    c("a model answering ORACLE falls back to the rules -- it is not a path from MySQL",
      bad["source"] == "heuristic_after_unknown_target" and bad["target"] == "MYSQL")
    err = pt.propose_target(a["paths"], a["stored_code"], use_bedrock=True,
                            client=_Stub(raise_error="throttled"))
    c("a model error falls back, visibly", err["source"] == "heuristic_after_model_error")
    junk = pt.propose_target(a["paths"], a["stored_code"], use_bedrock=True,
                             client=_Stub("not json at all"))
    c("an unparseable reply falls back, visibly",
      junk["source"] == "heuristic_after_unparseable_reply")
    blocked_stub = _Stub({"target": "MYSQL", "confidence": "high", "reason": "x"})
    pt.propose_target(blocked, a["stored_code"], use_bedrock=True, client=blocked_stub)
    c("a blocked path never reaches the model", blocked_stub.calls == 0)

    print()
    print("validate_target works on MySQL paths")
    v = validate_target.validate_target(
        {"target": "MYSQL", "confidence": "high", "evidence_basis": None,
         "reason": "RDS for MySQL carries 10 effort points against 26.", "source": "bedrock"},
        a)
    checks = {x["check"]: x["verdict"] for x in v["checks"]}
    c("recommending the homogeneous path keeps HIGH confidence with no conversion "
      "evidence -- it rests on converting nothing", checks["confidence_allowed"] == "PASS")
    c("MySQL effort figures count as evidence, not inventions",
      checks["reason_cites_evidence"] == "PASS", str(checks))
    v2 = validate_target.validate_target(
        {"target": "POSTGRESQL", "confidence": "high", "evidence_basis": None,
         "reason": "PostgreSQL.", "source": "bedrock"}, a)
    c("a PostgreSQL recommendation from MySQL is STILL limited by conversion "
      "evidence -- the exemption is for the homogeneous path only",
      {x["check"]: x["verdict"] for x in v2["checks"]}["confidence_allowed"] == "OVERRIDE")
    v3 = validate_target.validate_target(
        {"target": "MYSQL", "confidence": "high", "evidence_basis": None,
         "reason": "It costs 999 points.", "source": "bedrock"}, a)
    c("an invented number is still caught",
      {x["check"]: x["verdict"] for x in v3["checks"]}["reason_cites_evidence"] == "WARN")

    print()
    print("the MySQL sizing proposal from the model")
    sstub = _Stub({"instance_class": "db.t3.small", "storage_gb": 20, "rationale": "Small."})
    sp = propose.propose(_facts(), use_bedrock=True, client=sstub)
    c("uses the model's class", sp["source"] == "bedrock" and sp["instance_class"] == "db.t3.small")
    c("carries no edition, whatever the model says", sp["edition"] is None)
    c("the prompt says there is no edition to choose", "NO edition" in sstub.prompt)
    c("the prompt does not ask for an edition field", '"edition"' not in sstub.prompt)
    unk = propose.propose(_facts(), use_bedrock=True,
                          client=_Stub({"instance_class": "db.x99.huge", "storage_gb": 1}))
    c("an unknown instance class falls back", unk["source"] == "heuristic_after_unknown_class")

    print()
    print(f"{c.ok}/{c.n} checks passed")
    return 0 if c.ok == c.n else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
