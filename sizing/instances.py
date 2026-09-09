"""RDS for Oracle instance classes, as data.

No prices here. An hourly rate depends on region, term, edition and licence
model, and a wrong number quoted to a client is worse than no number. Licence
*counts* are derivable and are computed; licence *cost* is not.
"""

from __future__ import annotations

# class, vCPU, memory GiB, burstable, notes
CATALOGUE = [
    {"class": "db.t3.small", "vcpu": 2, "memory_gib": 2, "burstable": True},
    {"class": "db.t3.medium", "vcpu": 2, "memory_gib": 4, "burstable": True},
    {"class": "db.t3.large", "vcpu": 2, "memory_gib": 8, "burstable": True},
    {"class": "db.t3.xlarge", "vcpu": 4, "memory_gib": 16, "burstable": True},
    {"class": "db.t3.2xlarge", "vcpu": 8, "memory_gib": 32, "burstable": True},
    {"class": "db.m5.large", "vcpu": 2, "memory_gib": 8, "burstable": False},
    {"class": "db.m5.xlarge", "vcpu": 4, "memory_gib": 16, "burstable": False},
    {"class": "db.m5.2xlarge", "vcpu": 8, "memory_gib": 32, "burstable": False},
    {"class": "db.m5.4xlarge", "vcpu": 16, "memory_gib": 64, "burstable": False},
    {"class": "db.m5.8xlarge", "vcpu": 32, "memory_gib": 128, "burstable": False},
    {"class": "db.r5.large", "vcpu": 2, "memory_gib": 16, "burstable": False},
    {"class": "db.r5.xlarge", "vcpu": 4, "memory_gib": 32, "burstable": False},
    {"class": "db.r5.2xlarge", "vcpu": 8, "memory_gib": 64, "burstable": False},
    {"class": "db.r5.4xlarge", "vcpu": 16, "memory_gib": 128, "burstable": False},
]

BY_CLASS = {i["class"]: i for i in CATALOGUE}


def get(instance_class: str) -> dict | None:
    return BY_CLASS.get(instance_class)


def smallest_meeting(min_vcpu: int, min_memory_gib: int, max_vcpu: int | None = None) -> dict:
    candidates = [
        i
        for i in CATALOGUE
        if i["vcpu"] >= min_vcpu
        and i["memory_gib"] >= min_memory_gib
        and (max_vcpu is None or i["vcpu"] <= max_vcpu)
    ]
    if not candidates:
        return CATALOGUE[-1]
    return sorted(candidates, key=lambda i: (i["vcpu"], i["memory_gib"], i["burstable"]))[0]
