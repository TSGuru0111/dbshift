"""How much stored code would convert, decided without converting any of it.

Phase 3 has to weigh the heterogeneous path before the client has chosen it,
and the honest input to that weighing is the cost of rewriting the stored code.
Phase 4b measures that cost exactly -- it converts every object and compiles
each one on a real PostgreSQL. That is the right answer and the wrong
prerequisite: it needs a PostgreSQL target, which presumes the decision Phase 3
exists to make.

So this module answers the same question from the classifier alone. It routes
every object through `classify.route()` -- the 60-construct catalogue scanned
against the collected source -- and counts where they land. No conversion runs,
no rules fire, no target is contacted, nothing is compiled.

**What comes out is a projection, not a measurement, and it says so in its own
fields.** `basis` is `"classified"` here and `"measured"` or `"proven"` when it
comes from Phase 4b. A projection can be wrong in one direction that matters:
an object routed RULE is one the *classifier* believes the deterministic rules
cover, and the rules may still decline it once they run, or the compile may
fail. So `pct_automatic` here is an upper bound on what 4b will confirm, and
`sizing/target.py` grades its confidence accordingly rather than treating the
two tiers as interchangeable.

The direction of the error is the reason this is safe to show a client: a
projection that over-states automation is corrected *downward* by 4b, and a
recommendation built on it was made with the optimistic number. Phase 3 says
which basis it used, and 4b's measurement supersedes it wherever it exists.
"""

from __future__ import annotations

from pathlib import Path

from . import classify, inventory

# Where a classified route lands in the same vocabulary `sizing/target.py`
# already reads from a 4b plan. Mapping here rather than in sizing keeps the
# translation next to the thing being translated.
#
#   RULE      -> expected to convert by rule, no person
#   MODEL     -> a construct needing judgement; a person reviews whatever drafts it
#   MANUAL    -> no PostgreSQL equivalent; a person writes it
#   ABSORBED  -> a package spec; its body carries the members, so it is not work
#   EXCLUDED  -> broken on the source today; an estate problem on either path
_READY = (classify.RULE,)
_HANDWORK = (classify.MODEL, classify.MANUAL)

# `handwork` is kept as the sum of the two because `sizing/target.py` already
# reads a field of that name from a 4b plan, but the two halves cost very
# different amounts and are reported separately as well:
#
#   MODEL   a construct needing judgement. The model drafts it and the same five
#           gates decide -- including a real compile. A person reviews a draft.
#   MANUAL  no PostgreSQL equivalent exists. A person writes it from nothing.
#
# Collapsing them hides the difference between "six drafts to review" and "six
# rewrites to author", which is the difference the sizing decision turns on.
_MODEL_TIER = (classify.MODEL,)
_MANUAL_TIER = (classify.MANUAL,)


def project(inv: dict, owners: list[str] | None = None) -> dict:
    """Route every stored-code object and count where it lands.

    Returns the same shape `sizing/target._code_items` reads from a 4b plan,
    with `basis: "classified"` so a reader -- and the recommendation -- can tell
    a projection from a compile result.

    **`owners` scopes the projection, and a caller sizing an estate must pass
    it.** A collector run may hold several schemas -- a production schema, the
    writable rehearsal copy of it, an unrelated estate -- and pooling them
    produces an average that describes nothing. On the run this was built
    against, `DBMIG_APP` projects 80% automatic and its own rehearsal copy 20%,
    because Phase 4 applied fixes to the copy that introduced quoted mixed-case
    identifiers; the pooled figure of 58% is a fact about neither. Per-owner
    counts are always returned in `by_owner` so a caller can see the spread
    rather than trusting the total.
    """
    counts: dict[str, int] = {}
    objects: list[dict] = []
    selected = set(owners) if owners else None

    for obj in inv["objects"]:
        if selected is not None and obj["owner"] not in selected:
            continue
        routed = classify.route(obj, inv)
        route = routed["route"]
        counts[route] = counts.get(route, 0) + 1
        objects.append({
            "owner": obj["owner"],
            "object_type": obj["object_type"],
            "object_name": obj["object_name"],
            "route": route,
            "reason": routed["reason"],
            # The constructs that decided it, so a reader can see *why* an
            # object is a person's rather than taking the count on faith.
            "deciding_constructs": [
                {"name": c["name"], "tier": c["tier"], "postgres": c["postgres"]}
                for c in routed["constructs"] if c["tier"] in ("manual", "model")
            ],
        })

    def _tally(group: list[dict]) -> dict:
        c: dict[str, int] = {}
        for o in group:
            c[o["route"]] = c.get(o["route"], 0) + 1
        ready = sum(c.get(r, 0) for r in _READY)
        handwork = sum(c.get(r, 0) for r in _HANDWORK)
        convertible = ready + handwork
        return {
            "convertible": convertible,
            "ready": ready,
            "handwork": handwork,
            "model_tier": sum(c.get(r, 0) for r in _MODEL_TIER),
            "manual": sum(c.get(r, 0) for r in _MANUAL_TIER),
            "excluded_broken_on_source": c.get(classify.EXCLUDED, 0),
            "absorbed": c.get(classify.ABSORBED, 0),
            "pct_automatic": round(100 * ready / convertible) if convertible else None,
            "counts": c,
        }

    by_owner: dict[str, list[dict]] = {}
    for o in objects:
        by_owner.setdefault(o["owner"], []).append(o)

    ready = sum(counts.get(r, 0) for r in _READY)
    handwork = sum(counts.get(r, 0) for r in _HANDWORK)
    excluded = counts.get(classify.EXCLUDED, 0)
    absorbed = counts.get(classify.ABSORBED, 0)
    convertible = ready + handwork

    return {
        "basis": "classified",
        "measured": False,
        "owners": sorted(by_owner),
        # Per-owner, so a pooled total can never hide a schema that projects
        # far worse than the average.
        "by_owner": {owner: _tally(group) for owner, group in sorted(by_owner.items())},
        "scoped_to": sorted(selected) if selected is not None else None,
        "convertible": convertible,
        "ready": ready,
        "handwork": handwork,
        "model_tier": sum(counts.get(r, 0) for r in _MODEL_TIER),
        "manual": sum(counts.get(r, 0) for r in _MANUAL_TIER),
        # Nothing is compiled on this path, so nothing can be blocked *by* a
        # compile. Reported as zero rather than omitted, because the field
        # exists in the measured shape and a missing key reads as unknown.
        "blocked": 0,
        "excluded_broken_on_source": excluded,
        "absorbed": absorbed,
        "pct_automatic": round(100 * ready / convertible) if convertible else None,
        "objects": objects,
        "counts": counts,
    }


def from_run(run_dir: Path, owners: list[str] | None = None) -> dict:
    """Project straight from a collector run directory."""
    return project(inventory.load(run_dir), owners=owners)
