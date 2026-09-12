"""The five gates every converted object passes before a person sees it as ready.

    static -> policy -> parity -> compile on PostgreSQL -> approval

Parity is the gate that does not exist in Phase 4 and matters most here: a
conversion that compiles but silently dropped a construct is the failure mode
of every text-level translator, including a model. Every construct detected in
the source must be accounted for -- translated, with its PostgreSQL marker
present in the output, or not_translated with a reason -- and no Oracle form
may remain. Deterministic; the rules win over whatever produced the text."""

from __future__ import annotations

import re

from . import classify, policy

PASS, FAIL, BLOCKED = "pass", "fail", "blocked"


def _gate(name, status, detail, remedy=None):
    return {"gate": name, "status": status, "detail": detail, "remedy": remedy}


def static_check(conv: dict, obj: dict, expected: set[str]) -> dict:
    problems = []
    stmts = conv.get("statements") or []
    if not stmts or not "".join(stmts).strip():
        problems.append("no statements")
    created = [n.lower() for n in policy.created_names("\n".join(stmts))] if stmts else []
    if not created:
        problems.append("creates nothing")
    stray = [n for n in created if n not in expected]
    if stray:
        problems.append(f"creates {', '.join(stray)}, which is not a name this object may produce "
                        f"(allowed: {', '.join(sorted(expected))})")
    if not isinstance(conv.get("constructs"), list):
        problems.append("no construct accounting")
    if problems:
        return _gate("static", FAIL, "; ".join(problems),
                     "A conversion creates exactly the object it was asked to convert, under the "
                     "names that object may take, and accounts for every construct it met.")
    return _gate("static", PASS, f"creates {', '.join(created)}; {len(conv['constructs'])} construct(s) accounted for")


def policy_check(conv: dict, obj: dict) -> dict:
    violations = policy.check(conv.get("ddl") or "\n".join(conv.get("statements") or []))
    if violations:
        return _gate("policy", FAIL, "; ".join(violations),
                     "A converted object may only CREATE itself. It never drops, grants, alters or "
                     "escalates, whatever produced it.")
    return _gate("policy", PASS, "only CREATEs of the converted object; no privilege or data statements")


def parity_check(conv: dict, obj: dict, found: list[dict]) -> dict:
    """Every construct in the source is accounted for, and none of Oracle's forms survive."""
    catalogue = classify.by_id()
    ddl = conv.get("ddl") or "\n".join(conv.get("statements") or [])
    code = classify.code_only(ddl)
    handled = {c.get("oracle"): c for c in conv.get("constructs") or []}
    problems = []

    for c in found:
        if c["tier"] not in ("rule", "model"):
            continue
        h = handled.get(c["id"])
        if h is None:
            problems.append(f"{c['name']} is in the source but not accounted for")
            continue
        entry = catalogue[c["id"]]
        if h.get("handling") == "translated":
            marker = entry.get("marker")
            if marker and not re.search(marker, code, re.IGNORECASE):
                problems.append(f"{c['name']} claimed translated but its PostgreSQL form "
                                f"({entry['postgres']}) is not in the output")
        elif h.get("handling") == "not_translated":
            if not (h.get("note") or "").strip():
                problems.append(f"{c['name']} marked not_translated without a reason")
        else:
            problems.append(f"{c['name']} has no valid handling")

    for entry in catalogue.values():
        if entry.get("residue") and entry["tier"] == "rule":
            if re.search(entry["pattern"], code, re.IGNORECASE | re.MULTILINE):
                problems.append(f"Oracle form of {entry['name']} still present in the output")

    fn_bodies = re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION[\s\S]*?\$\$([\s\S]*?)\$\$", ddl, re.IGNORECASE)
    for body in fn_bodies:
        if re.search(r"^\s*(COMMIT|ROLLBACK)\s*;", classify.code_only(body), re.IGNORECASE | re.MULTILINE):
            problems.append("a function body contains COMMIT/ROLLBACK, which PostgreSQL forbids in functions")
    if obj["object_type"] == "TRIGGER":
        if not re.search(r"RETURNS\s+trigger", ddl, re.IGNORECASE):
            problems.append("trigger conversion has no trigger function")
        elif not re.search(r"\bRETURN\s+(NEW|OLD|NULL)\b", code, re.IGNORECASE):
            problems.append("trigger function never RETURNs, so the row change would be cancelled")

    if problems:
        return _gate("parity", FAIL, "; ".join(problems),
                     "Every construct must be translated (with its PostgreSQL form present) or "
                     "not_translated (with a reason). Behaviour is never dropped silently.")
    n = sum(1 for c in found if c["tier"] in ("rule", "model"))
    not_tr = [h["oracle"] for h in handled.values() if h.get("handling") == "not_translated"]
    detail = f"{n} construct(s) accounted for, no Oracle residue"
    if not_tr:
        detail += f"; declared not translated: {', '.join(not_tr)}"
    return _gate("parity", PASS, detail)


def compile_check(result: dict | None, target_detail: str | None) -> dict:
    if result is None:
        return _gate("compile", BLOCKED, "no PostgreSQL target configured",
                     "Start a local PostgreSQL (scripts/postgres-target/run_pg.ps1) and set DBSHIFT_PG_DSN "
                     "and DBSHIFT_PG_PASSWORD. Until then no conversion is proven to compile, so none is ready.")
    if result.get("shadow_failed"):
        return _gate("compile", BLOCKED, f"the compile scaffold for this owner could not be built: {result['message']}",
                     "Nothing under this owner was compiled, so nothing is proven either way. Usually a "
                     "referenced table needs a type this run did not convert; the shadow gives such columns "
                     "a TEXT placeholder, so a remaining failure is worth reading in full.")
    if result.get("ok"):
        detail = f"created and rolled back: {result['checked']} (on {target_detail})"
        check = result.get("plpgsql_check") or {}
        if check.get("findings"):
            detail += "; plpgsql_check warnings: " + " | ".join(check["findings"][:3])
        return _gate("compile", PASS, detail)
    if result.get("plpgsql_check", {}).get("findings"):
        return _gate("compile", FAIL, "plpgsql_check: " + " | ".join(result["plpgsql_check"]["findings"][:3]),
                     "The embedded SQL does not resolve against the shadow schema.")
    where = f" (statement {result.get('statement')}" + (f", position {result['position']}" if result.get("position") else "") + ")"
    msg = result.get("message") or "unknown error"
    return _gate("compile", FAIL, f"PostgreSQL {result.get('sqlstate') or ''}: {msg}{where}",
                 "It would have failed on the real target too. Rejected here instead."
                 + (f" Hint: {result['hint']}" if result.get("hint") else ""))


def approval(approved_by: str | None) -> dict:
    if approved_by:
        return _gate("approval", PASS, f"approved by {approved_by}")
    return _gate("approval", BLOCKED, "converted business logic needs a named human approver",
                 "There is no auto-apply level for converted code. A person reads it, then approves it "
                 "against their identity.")


def verdict(results: list[dict]) -> str:
    if any(g["status"] == FAIL for g in results):
        return "REJECTED"
    blocked = [g["gate"] for g in results if g["status"] == BLOCKED]
    if blocked == ["approval"]:
        return "READY_FOR_APPROVAL"
    if blocked:
        return "BLOCKED"
    return "APPROVED"
