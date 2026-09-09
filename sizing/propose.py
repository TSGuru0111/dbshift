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

from . import instances, policy

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
    memory = max(MIN_MEMORY_GIB, int(-(-(data_gb * MEMORY_PER_DATA_GB) // 1)))
    pick = instances.smallest_meeting(MIN_VCPU, memory)

    return {
        "source": "heuristic",
        "model_id": None,
        "edition": "EE" if apparent_forcing else "SE2",
        "apparent_forcing_features": apparent_forcing,
        "instance_class": pick["class"],
        "storage_gb": policy.storage_floor_gb(facts["segment_bytes"]),
        "rationale": (
            f"{data_gb} GB of segments across {facts['table_count']} tables. "
            f"Capacity floor of {memory} GiB memory and {MIN_VCPU} vCPU gives "
            f"{pick['class']}. "
            + (
                "Feature usage reports " + ", ".join(apparent_forcing) + " in use, "
                "which appears to require Enterprise Edition."
                if apparent_forcing
                else "No licensed feature usage detected, so SE2 appears viable."
            )
        ),
    }


def bedrock_proposal(facts: dict, model_id: str) -> dict:
    """Not reachable yet -- no AWS account. Present so the seam is explicit."""
    raise NotImplementedError(
        "Bedrock proposer requires an AWS account and bedrock:InvokeModel. "
        "Run with the heuristic proposer until then; see docs/05-aws-services.md."
    )


def propose(facts: dict, use_bedrock: bool = False, model_id: str | None = None) -> dict:
    if use_bedrock:
        return bedrock_proposal(facts, model_id or "")
    return heuristic_proposal(facts)
