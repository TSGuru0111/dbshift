"""The target platforms SCT can assess, and which ones DBShift actually migrates to.

The console renders this as a dropdown. Two things it must not do:

  - **Hide the out-of-scope targets.** SCT assesses Aurora and Redshift, a client
    will ask about both, and a dropdown that silently omits them looks like the
    tool cannot do it. They are listed and marked instead.
  - **Offer them as migration targets.** `docs/02-architecture.md` rules out
    Aurora (a different product from RDS for PostgreSQL), Redshift (analytical
    only, never OLTP), Oracle Database@AWS and SQL Server as a source. Selecting
    one produces an SCT assessment for comparison, not a migration path.

So each row carries `in_scope`, and the scope note is the sentence the console
shows next to it. The distinction is the honest bit: "SCT can tell you about
this" and "we will migrate you to this" are different claims.
"""

from __future__ import annotations

# `sct_platform` is the string AWS SCT's batch script expects for the target
# database platform. `engine` maps to what the rest of DBShift calls it, so a
# selected target can be handed to sizing, convert and provision unchanged --
# None where there is no such path, which is what keeps an out-of-scope target
# from leaking into Phase 3.
TARGETS = [
    {
        "id": "rds-oracle",
        "label": "Amazon RDS for Oracle",
        "sct_platform": "ORACLE",
        # The virtual target SCT maps the source server onto. Assessing needs no
        # live target, and this exact string is what the dropdown changes.
        "sct_virtual_target": "Servers.<Oracle (virtual)>",
        "engine": "oracle",
        "in_scope": True,
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
        "scope_note": "Heterogeneous. The path Phases 4b, 4c and 7 are built for.",
    },
    {
        "id": "aurora-postgresql",
        "label": "Amazon Aurora PostgreSQL",
        "sct_platform": "AURORA_POSTGRESQL",
        "sct_virtual_target": "Servers.<Aurora PostgreSQL (virtual)>",
        "engine": None,
        "in_scope": False,
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
        "scope_note": "SCT assesses it, DBShift does not migrate to it. Redshift is an analytical "
                      "warehouse and never a target for an OLTP estate.",
    },
]

DEFAULT_TARGET_ID = "rds-postgresql"

_BY_ID = {t["id"]: t for t in TARGETS}


class UnknownTarget(KeyError):
    pass


def get(target_id: str) -> dict:
    """One target row, or a refusal naming what is valid."""
    try:
        return _BY_ID[target_id]
    except KeyError:
        raise UnknownTarget(
            f"unknown target {target_id!r}; valid ids: " + ", ".join(sorted(_BY_ID))
        ) from None


def in_scope_ids() -> list[str]:
    return [t["id"] for t in TARGETS if t["in_scope"]]


def for_console() -> list[dict]:
    """The dropdown payload. In-scope targets first, each keeping its scope note."""
    ordered = sorted(TARGETS, key=lambda t: (not t["in_scope"], t["label"]))
    return [
        {
            "id": t["id"],
            "label": t["label"],
            "in_scope": t["in_scope"],
            "scope_note": t["scope_note"],
            "default": t["id"] == DEFAULT_TARGET_ID,
        }
        for t in ordered
    ]
