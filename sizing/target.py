"""Which engine the estate should migrate to, and what stands in the way.

Two paths are supported, and **the client chooses between them**:

    ORACLE      Oracle -> Amazon RDS for Oracle       (homogeneous)
    POSTGRESQL  Oracle -> Amazon RDS for PostgreSQL   (heterogeneous)

This module does not pick. It assembles the evidence a person needs to pick,
in the two shapes that matter: what would *block* each path outright, and what
would *cost effort* on it. A blocker is a fact about the estate that the target
engine cannot express at all; an effort item is work with a known shape.

The distinction is load-bearing. "Oracle has 40 features PostgreSQL lacks" is
not a decision input, because most of them are unused. What matters is the
handful this estate actually uses, weighed against the stored code that must
be rewritten -- and Phase 4b already measured that exactly, by compiling it.

**The evidence is assembled here and no model touches it.** Blockers, effort
items and their weights are arithmetic over reviewed rules, and the reason for
every point is recorded. Where Phase 4b has run, its real compile results are
used in preference to any estimate; the `evidence` field on each item says
which.

The *recommendation* over that evidence is a separate matter, and since
2026-09-17 a model may propose it -- `sizing/propose_target.py`, bounded by
`sizing/validate_target.py` exactly as `propose.py` is bounded by
`validate.py`. `assess()` still defaults to the deterministic recommendation,
so nothing changes for a caller that does not ask for the model. Two properties
hold either way: a **blocked path is never put to a model**, because a blocker
is a fact rather than a weighting; and a recommendation may not claim more
confidence than its evidence tier carries.
"""

from __future__ import annotations

ORACLE = "ORACLE"
POSTGRESQL = "POSTGRESQL"
# Added 2026-09-29 with the MySQL source. `TARGETS` is every target this project
# migrates to across ALL sources; which of them are legal from a given source is
# `engines/spec.py`'s decision, not this tuple's -- MySQL does not migrate to RDS
# for Oracle, and nothing here should imply it does.
MYSQL = "MYSQL"
TARGETS = (ORACLE, POSTGRESQL, MYSQL)

LABEL = {
    ORACLE: "Amazon RDS for Oracle",
    POSTGRESQL: "Amazon RDS for PostgreSQL",
    MYSQL: "Amazon RDS for MySQL",
}

# Features whose presence means PostgreSQL cannot host this estate as it stands.
# Each is a capability with no PostgreSQL equivalent that a migration could
# reproduce without redesigning the application.
PG_BLOCKING_FEATURES = {
    "Real Application Clusters (RAC)": (
        "RAC is a shared-storage clustering architecture. PostgreSQL has no "
        "equivalent, and RDS for Oracle does not offer it either -- an estate "
        "depending on RAC belongs on Oracle Database@AWS, which is out of scope."
    ),
    "Oracle Label Security": (
        "Row-level security labels enforced by the database. PostgreSQL has "
        "row-level security policies, but not the label model, so the rules "
        "must be redesigned rather than converted."
    ),
    "Oracle Database Vault": (
        "Separation-of-duty controls enforced inside the database. PostgreSQL "
        "has no equivalent; the controls move to IAM and application design."
    ),
    "Oracle Advanced Queuing": (
        "AQ is a database-resident message broker. PostgreSQL offers LISTEN / "
        "NOTIFY and advisory locks, which do not carry AQ's durability and "
        "dequeue semantics. The queue moves to SQS or the application changes."
    ),
}

# Features that cost effort on PostgreSQL but do not block it. The text says
# what the work actually is, because "unsupported" alone is not actionable.
PG_EFFORT_FEATURES = {
    "Partitioning (user)": (
        "PostgreSQL has declarative partitioning, but the syntax and the "
        "maintenance model differ. Each partitioned table is rewritten, and "
        "interval partitioning has no direct equivalent."
    ),
    "Advanced Compression": (
        "PostgreSQL compresses large values with TOAST automatically and has "
        "no table-level compression option. Storage sizing must be recomputed "
        "rather than carried across."
    ),
    "Transparent Data Encryption": (
        "RDS for PostgreSQL encrypts storage with KMS, which covers data at "
        "rest. Column-level TDE has no equivalent and moves to pgcrypto or "
        "the application."
    ),
    "In-Memory Column Store": (
        "No PostgreSQL equivalent. Queries relying on it need plan review, and "
        "some need an analytical target rather than an OLTP one."
    ),
    "Oracle Spatial and Graph": (
        "PostGIS covers most Locator and Spatial functionality, but it is an "
        "extension with its own types and function names, so spatial SQL is "
        "rewritten."
    ),
    "Spatial": (
        "PostGIS covers most Locator functionality, with its own types and "
        "function names, so spatial SQL is rewritten."
    ),
    "Oracle Multitenant": (
        "PostgreSQL has databases and schemas but no pluggable-database model. "
        "A multi-PDB estate becomes multiple instances or multiple schemas, "
        "which is a topology decision, not a conversion."
    ),
}

# Oracle-side effort. The homogeneous path is not free either, and saying so
# is what keeps the comparison honest.
ORACLE_EFFORT_FEATURES = {
    "Real Application Clusters (RAC)": (
        "RDS for Oracle does not offer RAC. A RAC source becomes a single "
        "instance, so the availability design changes even on the homogeneous "
        "path."
    ),
}


def _feature_names(features_detected: list[dict]) -> set[str]:
    return {f["name"] for f in features_detected or [] if f.get("currently_used") == "TRUE"}


def _item(kind: str, subject: str, detail: str, evidence: str, weight: int = 1) -> dict:
    """One piece of evidence against one target.

    kind      blocker | effort
    subject   what it is about, in the estate's own vocabulary
    evidence  where the fact came from, so a reader can check it
    weight    effort points; blockers do not carry weight, they stop the path
    """
    return {"kind": kind, "subject": subject, "detail": detail,
            "evidence": evidence, "weight": weight}


# Stored-code statuses that mean a person must write PostgreSQL by hand.
_HANDWORK = ("MANUAL", "MODEL_REQUIRED", "REJECTED")


def _code_items(conversion: dict | None, object_count: int | None) -> tuple[list[dict], dict]:
    """Effort from stored code, measured where possible and estimated otherwise.

    Three evidence tiers, in descending order of authority:

      proven/measured  a Phase 4b plan (`conversion["totals"]`). Objects were
                       converted, and on the proven tier compiled for real.
      classified       a `convert.project` projection. Objects were routed
                       against the construct catalogue; nothing was converted.
      absent           nothing is known, and that is said rather than guessed.

    The classified tier exists so Phase 3 can weigh the heterogeneous path
    without first running a conversion against a PostgreSQL target -- which
    would presume the decision this phase makes.
    """
    items: list[dict] = []

    # A projection arrives in the summary shape already, carrying its own
    # basis. Passed through with effort items that name it a projection.
    if conversion and conversion.get("basis") == "classified":
        ready = conversion.get("ready") or 0
        handwork = conversion.get("handwork") or 0
        model_tier = conversion.get("model_tier") or 0
        manual = conversion.get("manual") or 0
        excluded = conversion.get("excluded_broken_on_source") or 0
        if ready:
            items.append(_item(
                "effort", f"{ready} stored objects project as automatic",
                "Every construct in these objects is in the deterministic tier, so the "
                "rules are expected to convert them without a model. Nothing has been "
                "converted or compiled yet -- Phase 4b confirms or corrects this.",
                "convert.project classification", weight=0))
        if model_tier:
            items.append(_item(
                "effort", f"{model_tier} stored objects need a drafted rewrite",
                "Each uses a construct whose faithful translation needs judgement rather "
                "than substitution. A model drafts it and the same five gates decide, "
                "including a real compile; a person reviews the draft.",
                "convert.project classification", weight=2 * model_tier))
        if manual:
            items.append(_item(
                "effort", f"{manual} stored objects have no PostgreSQL equivalent",
                "A person writes these from nothing. This is the most expensive kind of "
                "stored-code work and the least reducible by tooling.",
                "convert.project classification", weight=3 * manual))
        if excluded:
            items.append(_item(
                "effort", f"{excluded} stored objects are broken on the source",
                "These do not compile on Oracle today, so they are excluded rather than "
                "converted. They are an estate problem on either path.",
                "collector invalid_objects", weight=0))
        return items, dict(conversion)

    if conversion and conversion.get("totals"):
        totals = conversion["totals"]
        ready = totals.get("READY_FOR_APPROVAL", 0)
        handwork = sum(totals.get(k, 0) for k in _HANDWORK)
        blocked = totals.get("BLOCKED", 0)
        excluded = totals.get("EXCLUDED_BROKEN_ON_SOURCE", 0)
        convertible = ready + handwork + blocked
        summary = {
            "measured": True,
            # A plan with nothing blocked on the compile gate was compiled for
            # real; one with blocked objects never reached a target.
            "basis": "measured" if blocked else "proven",
            "convertible": convertible,
            "ready": ready,
            "handwork": handwork,
            # The same split the classified tier reports, so a reader and the
            # recommendation see "drafts to review" apart from "rewrites to
            # author" on either tier.
            "model_tier": totals.get("MODEL_REQUIRED", 0),
            "manual": totals.get("MANUAL", 0) + totals.get("REJECTED", 0),
            "blocked": blocked,
            "excluded_broken_on_source": excluded,
            "pct_automatic": round(100 * ready / convertible) if convertible else None,
        }
        if ready:
            items.append(_item(
                "effort", f"{ready} stored objects convert automatically",
                "Phase 4b rewrote these as PL/pgSQL by rule and compiled every one on "
                "PostgreSQL inside a transaction that was rolled back. They still need a "
                "named approver before anything is applied.",
                "convert/output/conversion_plan.json", weight=0))
        if handwork:
            items.append(_item(
                "effort", f"{handwork} stored objects need a person",
                "No deterministic rewrite exists for these, so each is hand-written and "
                "reviewed. This is the real cost of the heterogeneous path, and it is "
                "counted rather than estimated.",
                "convert/output/conversion_plan.json", weight=3 * handwork))
        if blocked:
            items.append(_item(
                "effort", f"{blocked} stored objects are not yet proven",
                "Converted, but no PostgreSQL compile target was configured when Phase 4b "
                "ran, so none is proven to compile. Run Phase 4b against a target before "
                "reading this decision as final.",
                "convert/output/conversion_plan.json", weight=blocked))
        if excluded:
            items.append(_item(
                "effort", f"{excluded} stored objects are broken on the source",
                "These do not compile on Oracle today, so they are excluded rather than "
                "converted. They are an estate problem on either path.",
                "collector invalid_objects", weight=0))
        return items, summary

    # Phase 4b has not run. Say so rather than inventing a number.
    summary = {"measured": False, "object_count": object_count}
    items.append(_item(
        "effort", "Stored code has not been assessed for PostgreSQL",
        "Phase 4b converts PL/SQL to PL/pgSQL and compiles it for real, which is the "
        "only honest way to size this. Until it runs, the effort on the PostgreSQL "
        "path is unknown -- not zero.",
        "convert/ has no plan for this run", weight=0))
    return items, summary


def _summarise(items: list[dict]) -> dict:
    blockers = [i for i in items if i["kind"] == "blocker"]
    effort = [i for i in items if i["kind"] == "effort"]
    return {
        "possible": not blockers,
        "blockers": blockers,
        "effort": effort,
        "effort_points": sum(i["weight"] for i in effort),
    }


def _assess_mysql(facts: dict, conversion: dict | None = None, use_bedrock: bool = False,
                  model_id: str | None = None, client=None) -> dict:
    """The two paths from a MySQL source: RDS for MySQL and RDS for PostgreSQL.

    **What is different from the Oracle case, and why it changes the answer.**
    Oracle -> PostgreSQL is weighed against a licence: every point of conversion
    effort buys the end of an Oracle bill. MySQL and PostgreSQL are both open
    source, so there is **no licence saving on either side**. Conversion effort on
    the PostgreSQL path is therefore pure cost unless the client has a reason
    outside this assessment -- a platform standard, or a feature MySQL lacks.

    The evidence is MySQL's own: storage engines, character sets, column types,
    definer rights. None of it appears in Oracle's feature-usage catalogue.

    Items that apply to BOTH paths (MyISAM, utf8mb3, missing primary keys) are
    listed on both, because they are pre-migration work whichever target is
    chosen -- leaving them off the homogeneous path would make it look free.
    """
    m = facts.get("mysql") or {}
    my: list[dict] = []
    pg: list[dict] = []

    def both(subject, detail, evidence, weight):
        my.append(_item("effort", subject, detail, evidence, weight))
        pg.append(_item("effort", subject, detail, evidence, weight))

    # -- pre-migration work on the SOURCE, whichever target -----------------
    if m.get("non_innodb_tables"):
        n = m["non_innodb_tables"]
        both(f"{n} non-InnoDB table(s)",
             "Converted to InnoDB before migrating. A MyISAM table has no "
             "transactions and no crash recovery, and DMS change data capture cannot "
             "replicate it.", "information_schema.TABLES.ENGINE", n)
    if m.get("utf8mb3_columns"):
        n = m["utf8mb3_columns"]
        both(f"{n} utf8mb3 column(s)",
             "Legacy 3-byte utf8 cannot hold a 4-byte character, so emoji and some CJK "
             "were already truncated on the source. Convert to utf8mb4 and check what "
             "was lost -- the move cannot restore it.",
             "information_schema.COLUMNS.CHARACTER_SET_NAME", 1)
    if m.get("tables_without_pk"):
        n = m["tables_without_pk"]
        both(f"{n} table(s) without a primary key",
             "InnoDB hides a clustered index DMS cannot address, so change data capture "
             "cannot locate a row for UPDATE or DELETE. A full load is unaffected.",
             "information_schema.TABLE_CONSTRAINTS", n)

    # -- the homogeneous path ------------------------------------------------
    if m.get("unrecreatable_definers"):
        n = m["unrecreatable_definers"]
        my.append(_item(
            "effort", f"{n} definer-rights object(s) owned by root or localhost",
            "RDS grants no SUPER privilege, so a routine or view defined by "
            "root@localhost cannot be recreated as-is and fails at runtime. Redefine "
            "each under an account that exists on the target.",
            "information_schema.ROUTINES / VIEWS", n))
    if m.get("events"):
        my.append(_item(
            "effort", f"{m['events']} scheduled event(s)",
            "RDS runs the event scheduler only when event_scheduler is set in the "
            "parameter group. Otherwise the events migrate and never fire.",
            "information_schema.EVENTS", 1))
    my.append(_item(
        "effort", "Data moves with AWS DMS",
        "Same engine, so the schema and stored code move unchanged. DMS reads the "
        "binlog, so a full load can be followed by change data capture for a short "
        "cutover. The replication instance bills while it runs.",
        "architecture", weight=0))

    # -- the heterogeneous path ----------------------------------------------
    code_items, code_summary = _code_items(conversion, facts.get("object_count"))
    if conversion:
        pg += code_items
    elif m.get("routines") or m.get("triggers"):
        # No Phase 4b evidence yet, so the stored code is counted, not costed --
        # and labelled that way rather than given a guessed weight.
        n = (m.get("routines") or 0) + (m.get("triggers") or 0)
        pg.append(_item(
            "effort", f"{n} stored routine(s) and trigger(s) to rewrite",
            "SQL/PSM to PL/pgSQL. Not yet converted, so this is a count, not an "
            "estimate: GROUP_CONCAT, ON DUPLICATE KEY UPDATE, SIGNAL and LAST_INSERT_ID "
            "each take a different shape on PostgreSQL. Phase 4b measures it.",
            "information_schema.ROUTINES / TRIGGERS", n))
    if m.get("enum_set_columns"):
        n = m["enum_set_columns"]
        pg.append(_item(
            "effort", f"{n} ENUM or SET column(s)",
            "ENUM becomes a CHECK constraint or an enum type; SET has no PostgreSQL "
            "equivalent and becomes an array or a join table -- an application change, "
            "not a type mapping.", "information_schema.COLUMNS", n))
    if m.get("unsigned_bigint_values"):
        n = m["unsigned_bigint_values"]
        ids = (m.get("unsigned_bigint_columns") or 0) - n
        pg.append(_item(
            "effort", f"{n} unsigned BIGINT value column(s)",
            "PostgreSQL has no unsigned types and its bigint stops at 2^63-1. A value "
            "column may already hold more, with nowhere to land, so these map to numeric "
            "-- wider, slower to index, and a change every reader must accept."
            + (f" Check each: {', '.join(m.get('unsigned_bigint_value_names') or [])}."
               if m.get("unsigned_bigint_value_names") else "")
            + (f" The other {ids} are identities or foreign keys to one -- they count up "
               "from 1, map to bigint safely, and are not weighed." if ids > 0 else ""),
            "information_schema.COLUMNS.COLUMN_TYPE", n))
    if m.get("case_insensitive_columns"):
        pg.append(_item(
            "effort", f"{m['case_insensitive_columns']} case-insensitive column(s)",
            "MySQL's _ci collations make 'A' = 'a'; PostgreSQL's default does not. "
            "Uniqueness and joins change behaviour, not just rendering -- each needs a "
            "nondeterministic collation, citext, or an application change.",
            "information_schema.COLUMNS.COLLATION_NAME", 2))
    if m.get("fulltext_indexes"):
        n = m["fulltext_indexes"]
        pg.append(_item(
            "effort", f"{n} FULLTEXT index(es)",
            "Rebuilt as tsvector columns with GIN indexes, and every MATCH ... AGAINST "
            "query rewritten. Ranking will differ.", "information_schema.STATISTICS", n))
    if (facts.get("structural") or {}).get("partitioned_tables"):
        n = facts["structural"]["partitioned_tables"]
        pg.append(_item(
            "effort", f"{n} partitioned table(s)",
            "Rewritten as PostgreSQL declarative partitioning. The keys carry across; "
            "the DDL does not.", "information_schema.PARTITIONS", n))
    if m.get("events"):
        pg.append(_item(
            "effort", f"{m['events']} scheduled event(s)",
            "PostgreSQL has no event scheduler. Each becomes a pg_cron job or moves "
            "out of the database.", "information_schema.EVENTS", m["events"]))
    pg.append(_item(
        "effort", "Data moves with AWS DMS",
        "The only route across engines. A replication instance bills while it runs.",
        "architecture", weight=0))

    paths = {MYSQL: _summarise(my), POSTGRESQL: _summarise(pg)}
    for name, path in paths.items():
        path["target"] = name
        path["label"] = LABEL[name]

    return {
        "source_engine": "MYSQL",
        "paths": paths,
        "stored_code": code_summary,
        # The same bounded route as Oracle: heuristic by default, a model when
        # asked, and validate_target re-checking whatever the model said.
        "recommended": _recommend(paths, code_summary, use_bedrock=use_bedrock,
                                  model_id=model_id, client=client),
    }


def assess(facts: dict, conversion: dict | None = None, use_bedrock: bool = False,
           model_id: str | None = None, client=None) -> dict:
    """Evidence for and against each target. Chooses nothing.

    Dispatches on the source engine, because the two sources are weighed on
    different evidence and -- more importantly -- against different stakes: an
    Oracle licence on one side, nothing on the other.
    """
    if str(facts.get("source_engine") or "").upper() == "MYSQL":
        return _assess_mysql(facts, conversion, use_bedrock=use_bedrock,
                             model_id=model_id, client=client)
    used = _feature_names(facts.get("features_detected", []))
    structural = facts.get("structural") or {}

    pg: list[dict] = []
    ora: list[dict] = []

    for name in sorted(used):
        if name in PG_BLOCKING_FEATURES:
            pg.append(_item("blocker", name, PG_BLOCKING_FEATURES[name],
                            "DBA_FEATURE_USAGE_STATISTICS"))
        elif name in PG_EFFORT_FEATURES:
            pg.append(_item("effort", name, PG_EFFORT_FEATURES[name],
                            "DBA_FEATURE_USAGE_STATISTICS", weight=2))
        if name in ORACLE_EFFORT_FEATURES:
            ora.append(_item("effort", name, ORACLE_EFFORT_FEATURES[name],
                             "DBA_FEATURE_USAGE_STATISTICS", weight=2))

    # Structural facts the feature statistics do not carry.
    if structural.get("partitioned_tables"):
        n = structural["partitioned_tables"]
        pg.append(_item(
            "effort", f"{n} partitioned table(s)",
            "Each is rewritten as PostgreSQL declarative partitioning. The partition "
            "keys carry across; the DDL and the maintenance jobs do not.",
            "DBA_TAB_PARTITIONS", weight=n))
    if structural.get("bitmap_indexes"):
        n = structural["bitmap_indexes"]
        pg.append(_item(
            "effort", f"{n} bitmap index(es)",
            "PostgreSQL builds bitmaps during query execution but has no bitmap index "
            "type. Each becomes a B-tree, and the plans that relied on it need review.",
            "DBA_INDEXES", weight=n))

    code_items, code_summary = _code_items(conversion, facts.get("object_count"))
    pg += code_items

    # Data movement differs by path, and this is where the DMS bill enters.
    pg.append(_item(
        "effort", "Data moves with AWS DMS, not Data Pump",
        "Data Pump writes an Oracle-only format, so the heterogeneous path cannot use "
        "it. DMS does a full load and can then keep the target current with change "
        "data capture, which is what makes a short cutover window possible. A "
        "replication instance bills for as long as it runs.",
        "architecture", weight=0))
    ora.append(_item(
        "effort", "Data moves with Data Pump over S3",
        "Homogeneous, so the dump loads directly. Faster than DMS at this size and "
        "nothing bills after it finishes -- but it is a full load, so the cutover needs "
        "an outage window that covers it.",
        "architecture", weight=0))

    def summarise(items: list[dict]) -> dict:
        blockers = [i for i in items if i["kind"] == "blocker"]
        effort = [i for i in items if i["kind"] == "effort"]
        return {
            "possible": not blockers,
            "blockers": blockers,
            "effort": effort,
            "effort_points": sum(i["weight"] for i in effort),
        }

    paths = {ORACLE: summarise(ora), POSTGRESQL: summarise(pg)}
    for name, p in paths.items():
        p["target"] = name
        p["label"] = LABEL[name]

    return {
        "paths": paths,
        "stored_code": code_summary,
        "recommended": _recommend(paths, code_summary, use_bedrock=use_bedrock,
                                  model_id=model_id, client=client),
    }


def _recommend(paths: dict, code: dict, use_bedrock: bool = False,
               model_id: str | None = None, client=None) -> dict:
    """A recommendation with its reasoning, which the client may ignore.

    The cascade that used to live here is now
    `propose_target.heuristic_target_proposal` -- unchanged in what it decides,
    except that it recommends on *projected* evidence with the confidence
    labelled a projection, where it previously refused to answer at all. That
    refusal is what made Phase 3 depend on Phase 4b having run against a
    PostgreSQL target, which put the target decision behind work that presumed
    it.

    With `use_bedrock`, a model proposes instead and `validate_target` checks
    it. The validated result is returned, carrying its own `checks` so a reader
    sees every place the rules corrected the model.
    """
    from . import propose_target, validate_target

    proposal = propose_target.propose_target(
        paths, code, use_bedrock=use_bedrock, model_id=model_id, client=client)
    if not use_bedrock:
        # Nothing to bound: the heuristic *is* the rules. Returned in the
        # historical shape so existing readers are unaffected.
        return {k: proposal[k] for k in ("target", "confidence", "reason")} | {
            "evidence_basis": proposal.get("evidence_basis"),
            "source": proposal["source"],
        }

    decided = validate_target.validate_target(proposal, {"paths": paths, "stored_code": code})
    return {
        "target": decided["target"],
        "confidence": decided["confidence"],
        "reason": decided["reason"],
        "evidence_basis": decided["evidence_basis"],
        "source": decided["source"],
        "model_id": decided.get("model_id"),
        "model_error": decided.get("model_error"),
        "checks": decided["checks"],
        "override_count": decided["override_count"],
        "warning_count": decided["warning_count"],
        "agreed_with_proposal": decided["agreed_with_proposal"],
    }


def choose(target: str, assessment: dict, chosen_by: str | None = None) -> dict:
    """Record the client's choice, refusing a blocked path.

    A choice is not a preference: it decides the engine every later phase
    provisions, migrates into and validates against. So it names who made it.
    """
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; expected one of {', '.join(TARGETS)}")
    # A known target is not necessarily a path FROM THIS SOURCE: RDS for Oracle
    # exists, but not from MySQL. The assessment only holds the legal ones.
    if target not in assessment["paths"]:
        legal = ", ".join(LABEL[t] for t in assessment["paths"])
        raise ValueError(
            f"{LABEL[target]} is not a migration path from this source. "
            f"The paths assessed are: {legal}.")
    path = assessment["paths"][target]
    if not path["possible"]:
        names = ", ".join(b["subject"] for b in path["blockers"])
        raise ValueError(
            f"refusing {LABEL[target]}: blocked by {names}. A blocker is not a warning -- "
            "the estate cannot run there as it stands.")
    return {
        "target": target,
        "label": LABEL[target],
        "chosen_by": chosen_by,
        # None when the rules made no recommendation: there is nothing to agree
        # or disagree with, and calling that a disagreement misreads the screen.
        "agreed_with_recommendation": (
            None if assessment["recommended"]["target"] is None
            else assessment["recommended"]["target"] == target),
        "effort_points": path["effort_points"],
    }
