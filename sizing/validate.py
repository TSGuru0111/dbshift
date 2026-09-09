"""Validation: where a proposal becomes a decision.

Every check returns PASS, or OVERRIDE with the corrected value and the reason.
Where the proposer and the rules disagree, **the rules win and the disagreement
is recorded** -- that logged disagreement is the evidence that the AI is bounded,
so it is a first-class output rather than something quietly reconciled.
"""

from __future__ import annotations

from . import instances, policy


def _check(name, verdict, detail, proposed=None, decided=None):
    return {
        "check": name,
        "verdict": verdict,
        "detail": detail,
        "proposed": proposed,
        "decided": decided,
    }


def validate(proposal: dict, facts: dict) -> dict:
    checks: list[dict] = []

    # 1 -- Edition. Recomputed from evidence rather than trusted.
    verdict = policy.edition_verdict(facts["features_detected"], facts["structural"])
    edition = verdict["edition"]

    if proposal["edition"] != edition:
        checks.append(
            _check(
                "edition",
                "OVERRIDE",
                f"Proposal said {proposal['edition']}; policy says {edition}. {verdict['reason']}",
                proposal["edition"],
                edition,
            )
        )
    else:
        checks.append(
            _check("edition", "PASS", f"{edition} confirmed. {verdict['reason']}", edition, edition)
        )

    # 2 -- Did the proposal justify the edition with features that do not force it?
    # The verdict can be right while the reasoning is wrong, and in a licence
    # negotiation the reasoning is what gets audited.
    dismissed = {d["feature"] for d in verdict["dismissed"]}
    wrongly_cited = sorted(set(proposal.get("apparent_forcing_features", [])) & dismissed)
    if wrongly_cited:
        checks.append(
            _check(
                "edition_rationale",
                "OVERRIDE",
                "Proposal cited "
                + ", ".join(wrongly_cited)
                + " as edition-forcing. "
                + " ".join(
                    d["why_not_forcing"] for d in verdict["dismissed"] if d["feature"] in wrongly_cited
                )
                + " Removed from the justification.",
                ", ".join(proposal.get("apparent_forcing_features", [])),
                ", ".join(f["feature"] for f in verdict["forced_by"]) or "none",
            )
        )
    else:
        checks.append(
            _check("edition_rationale", "PASS", "Every cited feature genuinely forces the edition.")
        )

    # 3 -- Instance exists in the catalogue.
    spec = instances.get(proposal["instance_class"])
    instance_class = proposal["instance_class"]
    if spec is None:
        spec = instances.smallest_meeting(2, 4)
        instance_class = spec["class"]
        checks.append(
            _check(
                "instance_known",
                "OVERRIDE",
                f"{proposal['instance_class']} is not a known RDS for Oracle class.",
                proposal["instance_class"],
                instance_class,
            )
        )
    else:
        checks.append(_check("instance_known", "PASS", f"{instance_class} is a valid class."))

    # 4 -- SE2 vCPU ceiling.
    if edition == "SE2" and spec["vcpu"] > policy.SE2_MAX_VCPU:
        capped = instances.smallest_meeting(2, spec["memory_gib"], max_vcpu=policy.SE2_MAX_VCPU)
        checks.append(
            _check(
                "se2_vcpu_ceiling",
                "OVERRIDE",
                f"SE2 is capped at {policy.SE2_MAX_VCPU} vCPU on RDS; "
                f"{instance_class} has {spec['vcpu']}.",
                instance_class,
                capped["class"],
            )
        )
        spec, instance_class = capped, capped["class"]
    else:
        checks.append(
            _check(
                "se2_vcpu_ceiling",
                "PASS",
                f"{spec['vcpu']} vCPU is within limits for {edition}.",
            )
        )

    # 5 -- Storage floor.
    floor = policy.storage_floor_gb(facts["segment_bytes"])
    storage_gb = proposal["storage_gb"]
    if storage_gb < floor:
        checks.append(
            _check(
                "storage_floor",
                "OVERRIDE",
                f"{storage_gb} GB is below the floor of {floor} GB "
                f"({facts['segment_gb']} GB of segments, {policy.STORAGE_HEADROOM}x headroom, "
                f"{policy.MIN_STORAGE_GB} GB engine minimum).",
                storage_gb,
                floor,
            )
        )
        storage_gb = floor
    else:
        checks.append(
            _check("storage_floor", "PASS", f"{storage_gb} GB meets the {floor} GB floor.")
        )

    # 6 -- Burstable classes are fine for a demo and wrong for steady production.
    if spec["burstable"]:
        checks.append(
            _check(
                "burstable_class",
                "WARN",
                f"{instance_class} is burstable. Adequate for migration rehearsal and demo; "
                "validate CPU credit behaviour before steady production use.",
            )
        )

    # 7 -- Sizing rests on capacity alone when there is no measured load.
    if not facts["utilization"]["available"]:
        checks.append(
            _check(
                "utilization_evidence",
                "WARN",
                f"No usable utilization data -- {facts['utilization']['reason']}. This sizing is "
                "a capacity-derived floor, not a load-derived recommendation. It must be "
                "validated against measured production load before cutover, and nothing here "
                "should be presented as measured headroom.",
            )
        )
    else:
        checks.append(
            _check(
                "utilization_evidence",
                "PASS",
                f"Utilization data usable -- {facts['utilization']['reason']}.",
            )
        )

    overrides = [c for c in checks if c["verdict"] == "OVERRIDE"]
    warnings = [c for c in checks if c["verdict"] == "WARN"]

    return {
        "edition": edition,
        "licence_model": verdict["licence_model"],
        "instance_class": instance_class,
        "vcpu": spec["vcpu"],
        "memory_gib": spec["memory_gib"],
        "storage_gb": storage_gb,
        "storage_type": "gp3",
        "character_set": facts["character_set"],
        "processor_licences": (
            policy.processor_licences(spec["vcpu"]) if verdict["licence_model"] == "BYOL" else 0
        ),
        "forced_by": verdict["forced_by"],
        "dismissed": verdict["dismissed"],
        "checks": checks,
        "override_count": len(overrides),
        "warning_count": len(warnings),
        "agreed_with_proposal": not overrides,
    }
