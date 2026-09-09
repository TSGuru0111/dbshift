"""Deterministic edition and sizing policy.

No model participates in anything here. The model proposes an instance class in
propose.py; everything in this file either confirms or overrules it.
"""

from __future__ import annotations

# Features unavailable on RDS for Oracle SE2 at any price. Detected use of any of
# these forces Enterprise Edition, and EE on RDS is BYOL only -- there is no
# license-included Enterprise option.
HARD_EE_FEATURES = {
    "Partitioning (user)": "Partitioning is not available on SE2 at any price",
    "Advanced Compression": "Advanced Compression is an EE option",
    "Transparent Data Encryption": "TDE requires EE plus the Advanced Security option",
    "Oracle Advanced Security": "Advanced Security is an EE option",
    "Diagnostic Pack": "Diagnostic Pack requires EE",
    "Tuning Pack": "Tuning Pack requires EE",
    "In-Memory Column Store": "Database In-Memory requires EE",
    "Oracle Label Security": "Label Security requires EE",
    "Oracle Database Vault": "Database Vault requires EE",
    "Active Data Guard": "Active Data Guard requires EE",
    "Real Application Clusters (RAC)": "RAC requires EE and is unavailable on RDS entirely",
    "Parallel Query": "SE2 has no parallel execution",
}

# Detected, but NOT edition-forcing on their own. Each needs a threshold or a
# context that raw feature-usage statistics do not carry.
#
# Multitenant is the one that matters here: Oracle XE and RDS both run as a
# container database with a single PDB, which is included in every edition.
# DBA_FEATURE_USAGE_STATISTICS records that as "Oracle Multitenant" used. Reading
# it naively flips the licence verdict on an estate that owes nothing.
CONTEXTUAL_FEATURES = {
    "Oracle Multitenant": (
        "A single PDB is included in every edition, and RDS for Oracle runs a "
        "container database with one PDB by default. Only PDB counts above the "
        "included allowance require the Multitenant option."
    ),
    "Oracle Spatial and Graph": (
        "Locator functionality is included in all editions; only full Spatial "
        "requires the option. Usage statistics do not separate the two."
    ),
    "Spatial": (
        "Locator functionality is included in all editions; only full Spatial "
        "requires the option."
    ),
}

# RDS caps SE2 at this many vCPU. Anything larger must be EE.
SE2_MAX_VCPU = 16

# RDS for Oracle will not create an instance below this allocation.
MIN_STORAGE_GB = 20

# Free space multiple applied to current segment bytes. Covers index rebuilds,
# temp, redo and ordinary growth between sizing and cutover.
STORAGE_HEADROOM = 2.5

# AWS counts two vCPU as one Oracle processor licence for EE BYOL.
VCPU_PER_PROCESSOR_LICENCE = 2


def edition_verdict(feature_rows: list[dict], structural: dict) -> dict:
    """Decide SE2 vs EE from evidence, and say exactly what forced it.

    Two independent sources are used because either alone can mislead: feature
    usage statistics can be stale or sampled before the feature was used, and
    structural evidence (a partitioned table exists) can be present without the
    feature having been exercised. Either is sufficient to force EE.
    """
    forcing, dismissed = [], []

    for row in feature_rows:
        name = row.get("name")
        used = (row.get("detected_usages") or 0) > 0
        if not used:
            continue
        if name in HARD_EE_FEATURES:
            forcing.append(
                {
                    "feature": name,
                    "evidence": "feature_usage",
                    "detected_usages": row.get("detected_usages"),
                    "currently_used": row.get("currently_used"),
                    "why": HARD_EE_FEATURES[name],
                }
            )
        elif name in CONTEXTUAL_FEATURES:
            dismissed.append(
                {
                    "feature": name,
                    "evidence": "feature_usage",
                    "detected_usages": row.get("detected_usages"),
                    "why_not_forcing": CONTEXTUAL_FEATURES[name],
                }
            )

    named = {f["feature"] for f in forcing}
    if structural.get("partitioned_tables", 0) > 0 and "Partitioning (user)" not in named:
        forcing.append(
            {
                "feature": "Partitioning (structural)",
                "evidence": "dba_part_tables",
                "detected_usages": structural["partitioned_tables"],
                "currently_used": "TRUE",
                "why": HARD_EE_FEATURES["Partitioning (user)"],
            }
        )
    if structural.get("bitmap_indexes", 0) > 0:
        forcing.append(
            {
                "feature": "Bitmap index (structural)",
                "evidence": "dba_indexes",
                "detected_usages": structural["bitmap_indexes"],
                "currently_used": "TRUE",
                "why": "Bitmap indexes are an EE feature",
            }
        )
    if structural.get("compressed_tables", 0) > 0:
        forcing.append(
            {
                "feature": "Table compression (structural)",
                "evidence": "dba_tables",
                "detected_usages": structural["compressed_tables"],
                "currently_used": "TRUE",
                "why": HARD_EE_FEATURES["Advanced Compression"],
            }
        )

    edition = "EE" if forcing else "SE2"
    return {
        "edition": edition,
        "licence_model": "BYOL" if edition == "EE" else "license-included",
        "forced_by": forcing,
        "dismissed": dismissed,
        "reason": (
            "No edition-forcing feature detected; SE2 license-included is viable."
            if not forcing
            else f"{len(forcing)} edition-forcing feature(s) in use. "
            "There is no license-included Enterprise Edition on RDS, so EE means BYOL."
        ),
    }


def storage_floor_gb(segment_bytes: int) -> int:
    used = segment_bytes / 1024**3
    return max(MIN_STORAGE_GB, int(-(-(used * STORAGE_HEADROOM) // 1)))


def processor_licences(vcpu: int) -> int:
    return -(-vcpu // VCPU_PER_PROCESSOR_LICENCE)
