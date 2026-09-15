"""The proposal step -- the one place a model is allowed a judgement call.

The proposer reads raw facts and suggests an edition, an instance class and a
storage allocation, with a rationale. It is deliberately naive about licensing
nuance: it reports what the evidence appears to say. `validate.py` then applies
policy and can overrule it. That split is the point -- a proposal is a
suggestion, and only the rules engine produces the decision.

Two implementations:
  heuristic -- deterministic, runs today, no AWS account
  bedrock   -- the real proposer, wired but not reachable until an account exists

Whichever ran is recorded in `source`, so a reader always knows whether a model
was involved. Nothing here pretends a model ran when it did not.
"""

from __future__ import annotations

from . import instances, policy, utilization

# Without utilization data the only defensible input is capacity. These are
# floors derived from data volume, not recommendations derived from load.
MIN_VCPU = 2
MIN_MEMORY_GIB = 4
MEMORY_PER_DATA_GB = 0.25


def heuristic_proposal(facts: dict) -> dict:
    detected = [f["name"] for f in facts["features_detected"]]
    # Naive read: anything the feature-usage view reports as used is treated as
    # licence-relevant. This is what the raw evidence says on its face, and it is
    # exactly the reading validate.py exists to check.
    apparent_forcing = [
        n for n in detected if n in policy.HARD_EE_FEATURES or n in policy.CONTEXTUAL_FEATURES
    ]

    data_gb = facts["segment_gb"]
    util = facts["utilization"]
    measured = util.get("measured") if util else None

    if util and util.get("basis") == "measured" and measured:
        # Load-derived: size to what the workload actually consumed.
        req = utilization.requirements(measured)
        vcpu = max(MIN_VCPU, req["required_vcpu"] or MIN_VCPU)
        memory = max(MIN_MEMORY_GIB, req["required_memory_gib"] or MIN_MEMORY_GIB)
        pick = instances.smallest_meeting(vcpu, memory)
        sizing_basis = req
        basis_text = (
            f"Measured load over {measured['window_days']} days from "
            f"{measured['source']}: {req['percentile']} of {req['cpu_cores_observed']} cores "
            f"and {req['memory_gb_observed']} GB, plus {req['headroom']}x headroom, "
            f"requires {vcpu} vCPU and {memory} GiB, giving {pick['class']}. "
        )
    else:
        # Capacity-derived floor: the only defensible input without measured load.
        memory = max(MIN_MEMORY_GIB, int(-(-(data_gb * MEMORY_PER_DATA_GB) // 1)))
        pick = instances.smallest_meeting(MIN_VCPU, memory)
        sizing_basis = {"basis": "capacity", "percentile": None, "headroom": None}
        basis_text = (
            f"{data_gb} GB of segments across {facts['table_count']} tables. "
            f"No measured load, so a capacity floor of {memory} GiB memory and "
            f"{MIN_VCPU} vCPU gives {pick['class']}. "
        )

    return {
        "source": "heuristic",
        "model_id": None,
        "edition": "EE" if apparent_forcing else "SE2",
        "apparent_forcing_features": apparent_forcing,
        "instance_class": pick["class"],
        "storage_gb": policy.storage_floor_gb(facts["segment_bytes"]),
        "sizing_basis": sizing_basis,
        "rationale": (
            basis_text
            + (
                "Feature usage reports " + ", ".join(apparent_forcing) + " in use, "
                "which appears to require Enterprise Edition."
                if apparent_forcing
                else "No licensed feature usage detected, so SE2 appears viable."
            )
        ),
    }


PROMPT = """You are sizing an Oracle database for migration to Amazon RDS.

Propose an edition, an instance class and storage. You are proposing, not
deciding: a deterministic rules engine checks every value you give and overrides
you where the evidence disagrees. Say what the evidence supports and no more.

EVIDENCE
{evidence}

INSTANCE CLASSES AVAILABLE
{classes}

Reply with JSON only, no prose and no code fence:
{{"edition": "EE" or "SE2",
  "apparent_forcing_features": ["exact feature names from the evidence that you
      believe force Enterprise Edition; [] if none"],
  "instance_class": "one of the classes listed above",
  "storage_gb": integer,
  "rationale": "two or three sentences: what the numbers are and what they imply"}}

Rules you must follow:
- Cite a feature as edition-forcing only if it appears in the evidence.
- Do not invent an instance class. Choose from the list.
- Storage must cover the current segment size with room to grow.
"""


def bedrock_proposal(facts: dict, model_id: str, client=None) -> dict:
    """Ask the model to size the target. The rules engine still decides.

    This is the one place in the project where a model makes a judgement call
    rather than narrating a computed fact, and the whole design around it
    exists to bound that: `sizing/validate.py` recomputes every value from the
    evidence and records each disagreement as an OVERRIDE. A wrong answer here
    is caught and logged, never silently used.

    Anything the model gets structurally wrong -- a class that does not exist,
    a malformed reply -- falls back to the heuristic rather than failing the
    phase, and the fallback is visible in `source`.
    """
    import json

    from bedrock.client import BedrockClient, BedrockError

    evidence = {
        "segment_gb": facts["segment_gb"],
        "table_count": facts["table_count"],
        "object_count": facts.get("object_count"),
        "character_set": facts.get("character_set"),
        "features_currently_used": [f["name"] for f in facts.get("features_detected", [])
                                    if f.get("currently_used") == "TRUE"],
        "structural": facts.get("structural"),
        "utilization": {"basis": (facts.get("utilization") or {}).get("basis"),
                        "reason": (facts.get("utilization") or {}).get("reason")},
    }
    catalogue = [f"{i['class']} ({i['vcpu']} vCPU, {i['memory_gib']} GiB)"
                 for i in instances.CATALOGUE]

    client = client or BedrockClient()
    tier = "reasoning"
    try:
        reply = client.complete(
            tier,
            PROMPT.format(evidence=json.dumps(evidence, indent=2),
                          classes="\n".join(catalogue)),
            max_tokens=700)
    except BedrockError as exc:
        out = heuristic_proposal(facts)
        out["source"] = "heuristic_after_model_error"
        out["model_error"] = str(exc)[:200]
        return out

    # complete() returns a record, not a string: the text plus the model id and
    # token counts, so the proposal can say which model actually answered.
    text = (reply.get("text") or "").strip()
    # A model sometimes wraps JSON in a fence however firmly it is told not to.
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, KeyError) as exc:
        out = heuristic_proposal(facts)
        out["source"] = "heuristic_after_unparseable_reply"
        out["model_error"] = f"reply was not JSON: {str(exc)[:80]}"
        return out

    # Structural validation only. Whether the *answer* is right is validate.py's
    # job, and it is better at it than any check here would be.
    known = {i["class"] for i in instances.CATALOGUE}
    if parsed.get("instance_class") not in known:
        out = heuristic_proposal(facts)
        out["source"] = "heuristic_after_unknown_class"
        out["model_error"] = f"proposed {parsed.get('instance_class')!r}, which is not a class"
        return out

    pick = instances.get(parsed["instance_class"])
    return {
        "source": "bedrock",
        "model_id": reply.get("model_id"),
        "tokens": {"in": reply.get("input_tokens"), "out": reply.get("output_tokens")},
        "edition": parsed.get("edition") if parsed.get("edition") in ("EE", "SE2") else "SE2",
        "apparent_forcing_features": list(parsed.get("apparent_forcing_features") or []),
        "instance_class": pick["class"],
        "storage_gb": int(parsed.get("storage_gb") or policy.storage_floor_gb(facts["segment_bytes"])),
        "sizing_basis": {"basis": (facts.get("utilization") or {}).get("basis"),
                         "percentile": None, "headroom": None},
        "rationale": str(parsed.get("rationale") or "").strip()[:800],
    }


def propose(facts: dict, use_bedrock: bool = False, model_id: str | None = None,
            client=None) -> dict:
    if use_bedrock:
        return bedrock_proposal(facts, model_id or "", client=client)
    return heuristic_proposal(facts)
