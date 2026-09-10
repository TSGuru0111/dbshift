"""Where a fix comes from.

Three sources, tried in order:

  template  deterministic, no model, no cost. Preferred wherever a finding has
            exactly one correct remedy.
  bedrock   a model proposes SQL for findings with no template. **Not wired.**
  none      no fix is proposed; the finding is routed to a human.

Whichever produced a fix is recorded on it, so a reader always knows whether a
model was involved. Nothing here ever claims a model ran when it did not.
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


def build_fix(finding: dict, allow_model: bool = False) -> tuple[dict | None, str]:
    """Return (fix, source). `fix` is None when nobody could produce one."""
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

    if not allow_model:
        return None, "no_template_and_model_disabled"

    try:
        return bedrock_fix(finding), "bedrock"
    except GenerationUnavailable as exc:
        return None, f"model_unavailable: {exc}"
