"""The target platforms SCT can assess, per source engine, and which ones DBShift migrates to.

The console renders this as a dropdown. Three things it must not do:

  - **Hide the out-of-scope targets.** SCT assesses Aurora and Redshift, a client
    will ask about both, and a dropdown that silently omits them looks like the
    tool cannot do it. They are listed and marked instead.
  - **Offer them as migration targets.** `docs/02-architecture.md` rules out
    Aurora (a different product from RDS for PostgreSQL), Redshift (analytical
    only, never OLTP) and Oracle Database@AWS. Selecting one produces an SCT
    assessment for comparison, not a migration path.
  - **Offer a pair AWS does not publish.** Added 2026-09-29 with the MySQL
    source: **AWS SCT supports MySQL only to PostgreSQL-family targets.** There is
    no MySQL -> MySQL conversion path, because there is nothing to convert.

So each row carries `in_scope`, and the scope note is the sentence the console
shows next to it. The distinction is the honest bit: "SCT can tell you about
this" and "we will migrate you to this" are different claims -- and since the
MySQL source arrived there is a third: "AWS publishes no such conversion path at
all", which is different again from either.

**The MySQL -> MySQL row is the one to read carefully.** It is in scope as a
*migration* (Phase 7 moves it with DMS) while carrying `sct_conversion: False`,
because SCT produces a same-engine assessment -- compatibility and licence/cost
-- and no conversion action items. Phase 5 must therefore read "no action items"
on that pair as CLEAR with a reason, never as missing evidence. Absent evidence
reporting blocked is the right default everywhere else and would be wrong
exactly here.
"""

from __future__ import annotations

import engines

# `sct_platform` is the string AWS SCT's batch script expects for the target
# database platform. `engine` maps to what the rest of DBShift calls it, so a
# selected target can be handed to sizing, convert and provision unchanged --
# None where there is no such path, which is what keeps an out-of-scope target
# from leaking into Phase 3.
#
# `sct_conversion` says whether SCT reports *conversion work* for the pair, as
# opposed to merely assessing it. False on a homogeneous pair: there is nothing
# to convert, and a gate that waits for action items there waits forever.

ORACLE_TARGETS = [
    {
        "id": "rds-oracle",
        "label": "Amazon RDS for Oracle",
        "sct_platform": "ORACLE",
        # The virtual target SCT maps the source server onto. Assessing needs no
        # live target, and this exact string is what the dropdown changes.
        "sct_virtual_target": "Servers.<Oracle (virtual)>",
        "engine": "oracle",
        "in_scope": True,
        "sct_conversion": False,
        "scope_note": "Homogeneous. SCT reports little or no conversion work, which is itself "
                      "the finding a client is buying.",
    },
    {
        "id": "rds-postgresql",
        "label": "Amazon RDS for PostgreSQL",
        "sct_platform": "POSTGRESQL",
        "sct_virtual_target": "Servers.<PostgreSQL (virtual)>",
        "engine": "postgresql",
        "in_scope": True,
        "sct_conversion": True,
        "scope_note": "Heterogeneous. The path Phases 4b, 4c and 7 are built for.",
    },
    {
        "id": "aurora-postgresql",
        "label": "Amazon Aurora PostgreSQL",
        "sct_platform": "AURORA_POSTGRESQL",
        "sct_virtual_target": "Servers.<Aurora PostgreSQL (virtual)>",
        "engine": None,
        "in_scope": False,
        "sct_conversion": True,
        "scope_note": "SCT assesses it, DBShift does not migrate to it. Aurora is a different "
                      "product from RDS for PostgreSQL and is out of scope by decision.",
    },
    {
        "id": "redshift",
        "label": "Amazon Redshift",
        "sct_platform": "REDSHIFT",
        "sct_virtual_target": "Servers.<Amazon Redshift (virtual)>",
        "engine": None,
        "in_scope": False,
        "sct_conversion": True,
        "scope_note": "SCT assesses it, DBShift does not migrate to it. Redshift is an analytical "
                      "warehouse and never a target for an OLTP estate.",
    },
]

MYSQL_TARGETS = [
    {
        "id": "rds-mysql",
        "label": "Amazon RDS for MySQL",
        # SCT's own platform string for a MySQL target. Used for the same-engine
        # assessment only -- see sct_conversion below.
        "sct_platform": "MYSQL",
        "sct_virtual_target": "Servers.<MySQL (virtual)>",
        "engine": "mysql",
        "in_scope": True,
        # AWS publishes NO MySQL -> MySQL conversion path, because there is
        # nothing to convert. SCT still produces a same-engine assessment
        # (compatibility, and the licence/cost comparison it is documented for),
        # which is a weaker but real claim.
        "sct_conversion": False,
        "scope_note": "Homogeneous. AWS publishes no MySQL-to-MySQL conversion path -- there is "
                      "nothing to convert -- so SCT reports a same-engine assessment only, with "
                      "no conversion action items. Phase 7 still moves the data with DMS.",
    },
    {
        "id": "rds-postgresql",
        "label": "Amazon RDS for PostgreSQL",
        "sct_platform": "POSTGRESQL",
        "sct_virtual_target": "Servers.<PostgreSQL (virtual)>",
        "engine": "postgresql",
        "in_scope": True,
        "sct_conversion": True,
        "scope_note": "Heterogeneous. SCT's documented MySQL path, and the one that produces "
                      "conversion action items.",
    },
    {
        "id": "aurora-postgresql",
        "label": "Amazon Aurora PostgreSQL",
        "sct_platform": "AURORA_POSTGRESQL",
        "sct_virtual_target": "Servers.<Aurora PostgreSQL (virtual)>",
        "engine": None,
        "in_scope": False,
        "sct_conversion": True,
        "scope_note": "SCT assesses it, DBShift does not migrate to it. Aurora is a different "
                      "product from RDS for PostgreSQL and is out of scope by decision.",
    },
    {
        "id": "aurora-mysql",
        "label": "Amazon Aurora MySQL",
        "sct_platform": "AURORA_MYSQL",
        "sct_virtual_target": "Servers.<Aurora MySQL (virtual)>",
        "engine": None,
        "in_scope": False,
        "sct_conversion": False,
        "scope_note": "SCT assesses it, DBShift does not migrate to it. Aurora is a clustered "
                      "product with its own sizing and failover model, out of scope by decision.",
    },
]

BY_SOURCE = {
    engines.ORACLE: ORACLE_TARGETS,
    engines.MYSQL: MYSQL_TARGETS,
}

# Per source, the target the console selects when nothing was chosen. PostgreSQL
# on both: it is the pair with conversion work to show, which is what a client is
# actually buying an assessment for.
DEFAULT_TARGET_ID_BY_SOURCE = {
    engines.ORACLE: "rds-postgresql",
    engines.MYSQL: "rds-postgresql",
}

# Kept for callers that predate the source-engine flag. Oracle's list and
# Oracle's default, so nothing that never heard of MySQL changes behaviour.
TARGETS = ORACLE_TARGETS
DEFAULT_TARGET_ID = DEFAULT_TARGET_ID_BY_SOURCE[engines.ORACLE]


class UnknownTarget(KeyError):
    pass


def for_source(source_engine: str | None = None) -> list[dict]:
    """Every target SCT can assess from this source, in-scope and not."""
    return BY_SOURCE[engines.normalize(source_engine)]


def default_target_id(source_engine: str | None = None) -> str:
    return DEFAULT_TARGET_ID_BY_SOURCE[engines.normalize(source_engine)]


def get(target_id: str, source_engine: str | None = None) -> dict:
    """One target row, or a refusal naming what is valid.

    The refusal names the valid ids **for this source**, because "unknown target"
    and "not a target from this source" are different mistakes and the second is
    the one a caller is more likely to make: `rds-oracle` is a real target id
    that simply does not exist from a MySQL source.
    """
    rows = {t["id"]: t for t in for_source(source_engine)}
    try:
        return rows[target_id]
    except KeyError:
        src = engines.LABEL[engines.normalize(source_engine)]
        raise UnknownTarget(
            f"unknown target {target_id!r} for a {src} source; valid ids: "
            + ", ".join(sorted(rows))
        ) from None


def in_scope_ids(source_engine: str | None = None) -> list[str]:
    return [t["id"] for t in for_source(source_engine) if t["in_scope"]]


def converts(target_id: str, source_engine: str | None = None) -> bool:
    """Does SCT report conversion work for this pair, or only assess it?

    Phase 5 reads this. On a pair where SCT publishes no conversion path, zero
    action items is a complete result and the gate must say so -- rather than
    treating the absence as evidence it has not collected yet.
    """
    return bool(get(target_id, source_engine)["sct_conversion"])


def for_console(source_engine: str | None = None) -> list[dict]:
    """The dropdown payload. In-scope targets first, each keeping its scope note."""
    rows = for_source(source_engine)
    default = default_target_id(source_engine)
    ordered = sorted(rows, key=lambda t: (not t["in_scope"], t["label"]))
    return [
        {
            "id": t["id"],
            "label": t["label"],
            "in_scope": t["in_scope"],
            "sct_conversion": t["sct_conversion"],
            "scope_note": t["scope_note"],
            "default": t["id"] == default,
        }
        for t in ordered
    ]
