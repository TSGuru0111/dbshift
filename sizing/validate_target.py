"""Where an engine recommendation becomes a decision.

`validate.py` does this for the box -- edition, instance class, storage -- and
this does it for the engine. Same contract, same vocabulary: every check returns
PASS, or OVERRIDE with the corrected value and the reason, and **where the
proposer and the rules disagree the rules win and the disagreement is recorded**.

The checks are the ones a model can plausibly get wrong in a way that would
mislead a client:

    target_open          it recommended a path this estate blocks
    confidence_allowed   it claimed more certainty than the evidence tier carries
    reason_cites_evidence it cited a number that does not appear in the evidence
    handwork_acknowledged it recommended PostgreSQL while calling remaining work
                         small, when the evidence says it is not

The last two exist because the failure mode of a model asked to weigh a
commercial trade-off is not a malformed answer -- `propose_target.py` catches
those structurally -- but a *plausible* answer resting on a number nobody
measured. A recommendation that reads well and cites 80% when the evidence says
46% is worse than no recommendation, because it survives being read aloud.

Nothing here judges whether the recommendation is *wise*. Two people can read
46% automatic and disagree about whether to migrate, and that disagreement is
the client's to have. These checks establish only that the recommendation is
consistent with the evidence it claims to rest on.
"""

from __future__ import annotations

import re

from . import propose_target as pt
from . import target as target_mod
from .validate import _check


# Numbers in a reason that are allowed without appearing in the evidence:
# PostgreSQL major versions cited as context, and small ordinals. Kept narrow
# and explicit rather than loosening the check.
_ALLOWED_BARE = {"15", "16", "17", "1", "2", "0"}


def _cited_numbers(reason: str) -> set[str]:
    """Every integer and percentage a reason claims, as written."""
    return set(re.findall(r"\b(\d{1,3})\s*%", reason)) | set(re.findall(r"\b(\d{1,4})\b", reason))


def _evidence_numbers(paths: dict, code: dict) -> set[str]:
    """Every number the evidence actually contains, as a string."""
    nums: set[str] = set()
    for key in ("convertible", "ready", "handwork", "model_tier", "manual",
                "blocked", "excluded_broken_on_source", "pct_automatic", "absorbed"):
        v = code.get(key)
        if isinstance(v, int):
            nums.add(str(v))
    for name in (target_mod.ORACLE, target_mod.POSTGRESQL):
        p = paths.get(name) or {}
        nums.add(str(p.get("effort_points")))
        nums.add(str(len(p.get("effort") or [])))
        nums.add(str(len(p.get("blockers") or [])))
        for item in p.get("effort") or []:
            nums |= set(re.findall(r"\b(\d{1,4})\b", item.get("subject") or ""))
    return {n for n in nums if n != "None"}


def validate_target(proposal: dict, assessment: dict) -> dict:
    """Turn an engine recommendation into a decision, recording every override.

    `proposal` comes from `propose_target.propose_target` -- heuristic or
    Bedrock. `assessment` is `target.assess()`'s output, which carries both
    paths and the stored-code summary the proposal was built from.
    """
    paths = assessment["paths"]
    code = assessment.get("stored_code") or {}
    checks: list[dict] = []

    target = proposal.get("target")
    confidence = proposal.get("confidence")
    reason = proposal.get("reason") or ""
    basis = proposal.get("evidence_basis", pt._basis(code))

    decided_target = target
    decided_confidence = confidence
    decided_reason = reason

    # ---------------------------------------------------------- target_open
    #
    # A blocker is a fact, not a weighting. `propose_target` already falls back
    # when a model recommends a blocked path, so reaching this check means
    # something bypassed that -- a hand-written proposal, or a future caller.
    if target and not paths[target]["possible"]:
        names = ", ".join(b["subject"] for b in paths[target]["blockers"])
        fallback = pt.heuristic_target_proposal(paths, code)
        checks.append(_check(
            "target_open", "OVERRIDE",
            f"{target_mod.LABEL[target]} is blocked by {names}. A blocker is not a "
            f"weighting, so the recommendation is replaced by the rules'.",
            target, fallback["target"]))
        decided_target = fallback["target"]
        decided_confidence = fallback["confidence"]
        decided_reason = fallback["reason"]
    else:
        checks.append(_check(
            "target_open", "PASS",
            "The recommended path is open." if target
            else "No path was recommended, so none can be blocked.",
            target, decided_target))

    # --------------------------------------------------- confidence_allowed
    #
    # The rule that keeps a projection from reading as a measurement.
    allowed = pt.ALLOWED_BY_BASIS.get(basis, (pt.INSUFFICIENT,))
    if decided_confidence not in allowed:
        corrected = pt.PROJECTION if basis == "classified" else pt.JUDGEMENT
        if corrected not in allowed:
            corrected = allowed[0]
        checks.append(_check(
            "confidence_allowed", "OVERRIDE",
            f"{basis or 'absent'} evidence cannot carry {decided_confidence!r}: the "
            f"stored code was "
            + ("routed by construct, never converted or compiled"
               if basis == "classified" else
               "converted but never compiled" if basis == "measured" else
               "not assessed at all" if basis is None else
               "converted and compiled")
            + f". Corrected to {corrected!r}.",
            decided_confidence, corrected))
        decided_confidence = corrected
    else:
        checks.append(_check(
            "confidence_allowed", "PASS",
            f"{decided_confidence!r} is available to {basis or 'absent'} evidence.",
            decided_confidence, decided_confidence))

    # ----------------------------------------------- reason_cites_evidence
    #
    # A fabricated number is the failure this phase cannot afford, because it
    # is the one that reads as authoritative.
    cited = _cited_numbers(reason)
    known = _evidence_numbers(paths, code) | _ALLOWED_BARE
    invented = sorted(cited - known, key=lambda s: (len(s), s))
    if invented:
        checks.append(_check(
            "reason_cites_evidence", "WARN",
            "The reason cites " + ", ".join(invented) + ", which does not appear in the "
            "evidence. Read the numbers from the evidence rather than the prose.",
            reason[:200], None))
    else:
        checks.append(_check(
            "reason_cites_evidence", "PASS",
            "Every number in the reason appears in the evidence."))

    # ------------------------------------------------ handwork_acknowledged
    #
    # Recommending the heterogeneous path outright while work remains is the
    # specific over-reach worth catching: it turns a commercial judgement into
    # a technical verdict the evidence does not support.
    handwork = code.get("handwork") or 0
    if decided_target == target_mod.POSTGRESQL and handwork:
        checks.append(_check(
            "handwork_acknowledged", "OVERRIDE",
            f"{handwork} stored object(s) still need a person, so this is a commercial "
            f"judgement rather than a recommendation. The rules make no recommendation "
            f"where handwork remains.",
            decided_target, None))
        fallback = pt.heuristic_target_proposal(paths, code)
        decided_target = None
        decided_confidence = fallback["confidence"]
        decided_reason = fallback["reason"]
    else:
        checks.append(_check(
            "handwork_acknowledged", "PASS",
            "No stored-code handwork remains, or no engine was recommended.",
            decided_target, decided_target))

    overrides = [c for c in checks if c["verdict"] == "OVERRIDE"]
    warnings = [c for c in checks if c["verdict"] == "WARN"]

    return {
        "target": decided_target,
        "label": target_mod.LABEL[decided_target] if decided_target else None,
        "confidence": decided_confidence,
        "reason": decided_reason,
        "evidence_basis": basis,
        "source": proposal.get("source"),
        "model_id": proposal.get("model_id"),
        "model_error": proposal.get("model_error"),
        "checks": checks,
        "override_count": len(overrides),
        "warning_count": len(warnings),
        "agreed_with_proposal": not overrides,
    }
