"""Validation: where a proposal becomes a decision.

Every check returns PASS, or OVERRIDE with the corrected value and the reason.
Where the proposer and the rules disagree, **the rules win and the disagreement
is recorded** -- that logged disagreement is the evidence that the AI is bounded,
so it is a first-class output rather than something quietly reconciled.
"""

from __future__ import annotations

from . import instances, policy, target as target_mod


def _check(name, verdict, detail, proposed=None, decided=None):
    return {
        "check": name,
        "verdict": verdict,
        "detail": detail,
        "proposed": proposed,
        "decided": decided,
    }


def validate(proposal: dict, facts: dict, engine: str = target_mod.ORACLE) -> dict:
    """Turn a proposal into a decision for one target engine.

    `engine` decides which arithmetic applies. The Oracle path computes an
    edition, a licence model and processor licences from feature evidence.
    The PostgreSQL path has none of those -- the engine is open source, so
    there is nothing to license and nothing to override. Both paths size the
    instance the same way, from the same facts, so the numbers stay comparable.
    """
    if engine == target_mod.POSTGRESQL:
        return _validate_postgresql(proposal, facts)
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
    util = facts["utilization"]
    measured = util.get("measured")

    if not util["available"]:
        detail = (
            f"No usable utilization data -- {util['reason']}. This sizing is a "
            "capacity-derived floor, not a load-derived recommendation. It must be validated "
            "against measured production load before cutover, and nothing here should be "
            "presented as measured headroom."
        )
        if measured and not measured.get("usable_for_sizing"):
            detail += f" A utilization file was supplied but rejected: {measured['reason']}."
        else:
            detail += (
                " An AWS OLA / DB OLA, Migration Evaluator export or vendor monitoring feed "
                "supplies this; see sizing/samples/utilization_example.csv for the format."
            )
        checks.append(_check("utilization_evidence", "WARN", detail))
    else:
        checks.append(
            _check(
                "utilization_evidence",
                "PASS",
                f"Sizing is load-derived -- {util['reason']}. Measured over "
                f"{measured['window_days']} days, minimum {measured['min_samples']} samples "
                f"per metric.",
            )
        )

    # 8 -- Headroom against the observed peak, not just the sizing percentile.
    # A class that satisfies p95 but sits under the observed maximum will throttle
    # at exactly the moment the business notices.
    if util["available"] and measured:
        peak_cpu = measured["metrics"].get("cpu_cores_used", {}).get("max")
        peak_mem = measured["metrics"].get("memory_used_gb", {}).get("max")
        shortfalls = []
        if peak_cpu is not None and spec["vcpu"] < peak_cpu:
            shortfalls.append(f"{spec['vcpu']} vCPU against an observed peak of {peak_cpu} cores")
        if peak_mem is not None and spec["memory_gib"] < peak_mem:
            shortfalls.append(
                f"{spec['memory_gib']} GiB against an observed peak of {peak_mem} GB"
            )
        if shortfalls:
            checks.append(
                _check(
                    "peak_headroom",
                    "WARN",
                    f"{instance_class} is sized on {measured['sizing_percentile']} and sits below "
                    "the observed maximum: " + "; ".join(shortfalls) + ". Acceptable if the peak "
                    "is a tolerable brief degradation; not acceptable if it coincides with "
                    "month-end or batch.",
                )
            )
        else:
            checks.append(
                _check(
                    "peak_headroom",
                    "PASS",
                    f"{instance_class} covers the observed maximum on every sizing metric.",
                )
            )

    overrides = [c for c in checks if c["verdict"] == "OVERRIDE"]
    warnings = [c for c in checks if c["verdict"] == "WARN"]

    return {
        "engine": target_mod.ORACLE,
        "engine_label": target_mod.LABEL[target_mod.ORACLE],
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


def _validate_postgresql(proposal: dict, facts: dict) -> dict:
    """The PostgreSQL decision: capacity only, no licence arithmetic.

    Deliberately a separate function rather than branches threaded through the
    Oracle path. Half the Oracle checks -- edition, the rationale behind it,
    the SE2 vCPU ceiling, processor licences -- have no PostgreSQL meaning at
    all, and a shared function full of `if engine ==` would read as though
    they did.
    """
    checks: list[dict] = []

    checks.append(_check(
        "edition", "PASS",
        "RDS for PostgreSQL has no editions and no licence model: the engine is open "
        "source and AWS charges for the instance alone. Nothing about the estate's "
        "Oracle feature usage can change that, so there is no edition to decide and "
        "no processor licence to count.",
        None, None))

    # Instance class. The same catalogue and the same floors as the Oracle path,
    # because the workload is the workload -- only the licensing differs.
    spec = instances.get(proposal["instance_class"])
    if spec is None:
        spec = instances.smallest_meeting(policy.__dict__.get("MIN_VCPU", 2), 4)
        checks.append(_check(
            "instance_class", "OVERRIDE",
            f"Proposed class {proposal['instance_class']!r} is not in the catalogue; "
            f"{spec['class']} is the smallest that meets the floor.",
            proposal["instance_class"], spec["class"]))
    else:
        checks.append(_check(
            "instance_class", "PASS",
            f"{spec['class']} -- {spec['vcpu']} vCPU, {spec['memory_gib']} GiB.",
            spec["class"], spec["class"]))
    instance_class = spec["class"]

    if spec["class"].startswith("db.t"):
        checks.append(_check(
            "burstable", "WARN",
            f"{instance_class} is burstable. Adequate for migration rehearsal and demo; "
            "validate CPU credit behaviour before steady production use."))

    # Storage. PostgreSQL stores the same data differently, so the Oracle
    # segment total is adjusted before the usual headroom is applied.
    storage_gb = policy.pg_storage_floor_gb(facts["segment_bytes"])
    oracle_equivalent = policy.storage_floor_gb(facts["segment_bytes"])
    if proposal.get("storage_gb", 0) < storage_gb:
        checks.append(_check(
            "storage", "OVERRIDE",
            f"{storage_gb} GB. PostgreSQL has no segment compression, uses 8 KB pages with "
            f"their own fill factor, and builds larger indexes than Oracle for the same "
            f"columns, so the Oracle segment total is raised by "
            f"{int((policy.PG_SEGMENT_MULTIPLE - 1) * 100)}% before the usual "
            f"{policy.STORAGE_HEADROOM}x free-space headroom. The same estate on Oracle "
            f"would take {oracle_equivalent} GB.",
            proposal.get("storage_gb"), storage_gb))
    else:
        checks.append(_check(
            "storage", "PASS",
            f"{storage_gb} GB covers the estate with headroom on PostgreSQL.",
            proposal.get("storage_gb"), storage_gb))

    # Character set. AL32UTF8 maps to UTF8; anything else needs a conversion decision.
    cs = facts.get("character_set")
    if cs in ("AL32UTF8", "UTF8"):
        checks.append(_check(
            "encoding", "PASS",
            f"Source character set {cs} maps to PostgreSQL UTF8 without transcoding.",
            cs, "UTF8"))
    else:
        checks.append(_check(
            "encoding", "WARN",
            f"Source character set {cs} does not map cleanly to UTF8. Every character "
            "column is transcoded during the load, and characters with no UTF-8 "
            "representation fail rather than convert silently. Confirm the source data "
            "before the full load.",
            cs, "UTF8"))

    # The same utilization honesty as the Oracle path: capacity floor is not a
    # measured recommendation, and saying so is the point.
    util = facts["utilization"]
    measured = util.get("measured")
    if not util["available"]:
        detail = (
            f"No usable utilization data -- {util['reason']}. This sizing is a "
            "capacity-derived floor, not a load-derived recommendation. PostgreSQL's "
            "execution model differs from Oracle's, so Oracle's measured load does not "
            "transfer directly either: rehearse before cutover."
        )
        if measured and not measured.get("usable_for_sizing"):
            detail += f" A utilization file was supplied but rejected: {measured['reason']}."
        checks.append(_check("utilization_evidence", "WARN", detail))
    else:
        checks.append(_check(
            "utilization_evidence", "PASS",
            f"Sizing is load-derived -- {util['reason']}. Measured over "
            f"{measured['window_days']} days. Treat it as an upper bound on PostgreSQL: "
            "the engines schedule work differently."))

    overrides = [c for c in checks if c["verdict"] == "OVERRIDE"]
    warnings = [c for c in checks if c["verdict"] == "WARN"]

    return {
        "engine": target_mod.POSTGRESQL,
        "engine_label": target_mod.LABEL[target_mod.POSTGRESQL],
        "edition": None,
        "licence_model": None,
        "instance_class": instance_class,
        "vcpu": spec["vcpu"],
        "memory_gib": spec["memory_gib"],
        "storage_gb": storage_gb,
        "storage_type": "gp3",
        "character_set": "UTF8",
        "source_character_set": cs,
        "processor_licences": 0,
        "forced_by": [],
        "dismissed": [],
        "checks": checks,
        "override_count": len(overrides),
        "warning_count": len(warnings),
        "agreed_with_proposal": not overrides,
    }
