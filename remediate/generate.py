"""Where a fix comes from.

Sources, tried in order:

  template        deterministic, no model, no cost. Preferred wherever a finding
                  has exactly one correct remedy.
  static_fixture  hand-written output standing in for the model while Bedrock
                  invoke is blocked (model_mode="static"). See bedrock/static.py.
  bedrock         a model proposes SQL for findings with no template
                  (model_mode="live"). **Not wired.**
  none            no fix is proposed; the finding is routed to a human.

Whichever produced a fix is recorded on it, so a reader always knows whether a
model was involved. Nothing here ever claims a model ran when it did not -- a
static fixture reports model_id=None, because no model produced it.
"""

from __future__ import annotations

import logging

from . import policy, templates

log = logging.getLogger("remediate.generate")


class GenerationUnavailable(RuntimeError):
    """No source could produce a fix for this finding."""


FIX_PROMPT = """You are proposing a remediation for one finding on an Oracle
database that is about to be migrated to Amazon RDS.

FINDING
{finding}

Write the SQL that fixes it on the source database, and the SQL that undoes
that fix. You are proposing, not applying: every statement you write is checked
against a policy allow-list, parsed, run against a rehearsal copy, and approved
by a named person before it touches anything. Nothing you return is trusted.

Reply with JSON only, no prose and no code fence:
{{"sql": "one or more statements, semicolon-separated",
  "rollback_sql": "the statements that undo it",
  "explain": "one or two sentences on what this does and why it fixes the finding",
  "caveat": "what a reviewer must check before approving, or the empty string"}}

Hard rules. Breaking any of these makes the fix useless, because the policy
gate will reject it and the finding goes to a person anyway:
- Never DROP, TRUNCATE or DELETE. A remediation that destroys data is not a
  remediation.
- Never GRANT or REVOKE. Privileges are a security decision, not a fix.
- Never write DDL against a table you were not told about.
- If the finding cannot be fixed safely in SQL, return an empty "sql" and say
  why in "explain". That is a correct answer, not a failure.
"""


def bedrock_fix(finding: dict, model_tier: str = "reasoning", client=None) -> dict:
    """Ask the model for a fix. It gets no shortcut through the gates.

    Returns the same shape a template does -- sql, rollback_sql, explain,
    caveat -- and the caller runs it through exactly the same five gates:
    static check, policy allow-list, syntax, dry run on the rehearsal copy, and
    a named approval. A model-authored fix that fails any of them is rejected
    the way a template's would be.

    Raises GenerationUnavailable when the model cannot answer or answers with
    something unusable, so the finding routes to a person rather than to a
    guess.
    """
    import json

    from bedrock.client import BedrockClient, BedrockError

    detail = {
        "rule_id": finding.get("rule_id"),
        "title": finding.get("title"),
        "severity": finding.get("severity"),
        "remediation_level": finding.get("remediation_level"),
        "object": finding.get("object_name"),
        "object_type": finding.get("object_type"),
        "owner": finding.get("owner"),
        "detail": finding.get("detail"),
        "recommendation": finding.get("recommendation"),
    }

    client = client or BedrockClient()
    try:
        reply = client.complete(
            model_tier, FIX_PROMPT.format(finding=json.dumps(detail, indent=2)),
            max_tokens=900)
    except BedrockError as exc:
        raise GenerationUnavailable(f"the model could not answer: {exc}") from exc

    text = (reply.get("text") or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, KeyError) as exc:
        raise GenerationUnavailable(
            f"the model's reply was not usable JSON: {str(exc)[:80]}") from exc

    sql = (parsed.get("sql") or "").strip()
    if not sql:
        # A model saying "this cannot be fixed in SQL" is a correct answer, and
        # routing it to a person is the right outcome -- not an error to hide.
        raise GenerationUnavailable(
            "the model found no safe SQL fix: " + str(parsed.get("explain") or "")[:200])

    return {
        "sql": sql,
        "rollback_sql": (parsed.get("rollback_sql") or "").strip() or None,
        "explain": str(parsed.get("explain") or "").strip()[:600],
        "caveat": str(parsed.get("caveat") or "").strip()[:600] or None,
        "source": "bedrock",
        "model_id": reply.get("model_id"),
        "tokens": {"in": reply.get("input_tokens"), "out": reply.get("output_tokens")},
    }


MODEL_MODES = ("off", "static", "live")


def static_fix(finding: dict) -> tuple[dict | None, str]:
    """The hand-written stand-in for this exact finding, if one was authored.

    It may carry SQL (then it goes through all five gates like any fix) or only
    advice (then a person acts on it). A stale entry is refused, not served --
    it answers a question the estate is no longer asking.
    """
    from bedrock import static

    entry = static.lookup(finding)
    if entry is None:
        return None, "no_static_output"
    if entry["stale"]:
        return None, (
            f"static_output_stale: {entry['fixture_id']} was written against different "
            "evidence than this finding now reports"
        )
    return {
        "sql": entry.get("sql"),
        "rollback_sql": entry.get("rollback_sql"),
        "explain": entry.get("explain"),
        "caveat": entry.get("caveat"),
        "advice": entry.get("advice"),
        "artefact": entry.get("artefact"),
        "evidence": entry.get("evidence"),
        "fixture_id": entry["fixture_id"],
        "source": static.SOURCE,
        "model_id": None,
    }, static.SOURCE


def build_fix(finding: dict, model_mode: str = "off") -> tuple[dict | None, str]:
    """Return (fix, source). `fix` is None when nobody could produce one.

    A fix from the static source may carry advice and no SQL; the caller
    decides what that means for routing.
    """
    if model_mode not in MODEL_MODES:
        raise ValueError(f"model_mode must be one of {MODEL_MODES}, not {model_mode!r}")
    stance = policy.classify(finding["remediation_level"])

    if stance == "never_fix":
        return None, "never_fix"
    if stance == "human_authored":
        return None, "human_authored"

    try:
        fix = templates.build(finding)
    except templates.TemplateError as exc:
        # A template declining is a routing decision, not an error.
        log.info("template declined %s: %s", finding["rule_id"], exc)
        return None, f"template_declined: {exc}"

    if fix:
        return fix, "template"

    if model_mode == "off":
        return None, "no_template_and_model_disabled"
    if model_mode == "static":
        return static_fix(finding)

    try:
        return bedrock_fix(finding), "bedrock"
    except GenerationUnavailable as exc:
        return None, f"model_unavailable: {exc}"
