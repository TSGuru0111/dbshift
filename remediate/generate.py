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


def bedrock_fix(finding: dict, model_tier: str = "reasoning") -> dict:
    """Not reachable yet. Present so the seam is explicit rather than implied.

    When it is wired it must return the same shape a template does -- sql,
    rollback_sql, explain, caveat -- and it passes exactly the same five gates.
    A model-authored fix gets no shortcut.
    """
    raise GenerationUnavailable(
        "Bedrock generation is not wired. InvokeModel is currently blocked on this account "
        "by an AWS Marketplace subscription gap -- see docs/05-aws-services.md. Findings "
        "without a template are routed to a human until it is available."
    )


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
