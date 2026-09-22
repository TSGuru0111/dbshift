"""Where an AWS SCT action item must be fixed, and who may fix it.

SCT grades an action item by **effort** -- simple, medium, complex, decision --
and stops there. That is not enough to act on, because effort does not say
*where the work lands* or *who is allowed to do it*:

  - `5639` (install postgres_fdw) is `simple` and belongs entirely to the
    **target**: one extension, nobody touches Oracle.
  - `5984` (no precision on NUMBER) is `decision` but is a **source** defect --
    the columns are wrong in Oracle and stay wrong after migrating.
  - `9994` (queuing objects unconvertible) is `complex` and belongs to
    **neither**: no SQL exists, somebody must choose an architecture.

Routing is therefore a separate axis from complexity, and it is **data, not
code** -- one row per SCT issue code, the same reason `assess/rules.json` is a
table. Adding a code SCT introduces tomorrow is inserting a row, and every
decision is diffable and reviewable.

**An unmapped code routes to a person, with a reason.** Never to "automatic".
This mirrors `blocker/policy.BLOCKS`, where an unrecognised critical blocks
everything rather than being quietly assumed harmless -- the same instinct, for
the same reason: silence must not read as safety.

**The model never decides any of this.** It drafts the fix *text* for items
routed to it; where a fix belongs, who may apply it, and whether it blocks a
phase stay entirely here. Two runs of the same estate must agree.
"""

from __future__ import annotations

# ------------------------------------------------------------------ where
SOURCE = "source"        # fix Oracle before migrating; it stays broken otherwise
TARGET = "target"        # the PostgreSQL target absorbs it -- DDL, extension, setting
DECISION = "decision"    # no SQL exists; somebody must choose
HUMAN = "human"          # a person writes code, on either side

WHERE_LABEL = {
    SOURCE: "Fix in the source",
    TARGET: "Absorb in the target",
    DECISION: "Needs a decision",
    HUMAN: "Needs a person to write it",
}

WHERE_MEANING = {
    SOURCE: "A defect in the Oracle estate. Migrating does not fix it, and on a "
            "CDC migration some of these degrade replication rather than fail it.",
    TARGET: "Nothing is wrong with the source. The target needs DDL, an extension "
            "or a setting, applied when the target is built.",
    DECISION: "There is no statement that fixes this. Someone must choose an "
              "approach before the work can be estimated.",
    HUMAN: "Code must be rewritten by a person. A model may draft it; it is "
           "reviewed and compiled before anything is kept.",
}

# ------------------------------------------------------------------ who
AUTO = "auto"            # deterministic template, no model
MODEL = "model"          # the model drafts it; the same gates decide
PERSON = "person"        # no generated statement is offered at all

WHO_LABEL = {
    AUTO: "Automatic",
    MODEL: "AI-drafted, gated",
    PERSON: "Human only",
}

# The same two facts for a table cell rather than a card. The long forms above
# read well with room around them; in a column they were ellipsed to "Needs a
# person to writ…" and "AI-drafted,", which is worse than a shorter label that
# fits. The console shows these in the grid and the long form on hover.
WHERE_SHORT = {
    SOURCE: "Source",
    TARGET: "Target",
    DECISION: "Decision",
    HUMAN: "Engineer",
}

WHO_SHORT = {
    AUTO: "Automatic",
    MODEL: "AI + approval",
    PERSON: "Engineer",
}


def _r(where, who, why, clears_when, blocks=(), how=(), verify=()):
    """One routing row.

    `how` and `verify` are the two things a remediation screen has to answer and
    a finding never does: **what is the proposed solution**, and **what must a
    person confirm before it is applied**. They are written here, reviewed like
    the rest of the table, rather than generated -- a model-written method
    would change between runs, and a client reading a plan twice must see the
    same plan.
    """
    return {"where": where, "who": who, "why": why,
            "clears_when": clears_when, "blocks": list(blocks),
            "how": list(how), "verify": list(verify)}


# One row per SCT issue code. `blocks` names the downstream phases the item
# actually stands in front of, using `blocker/policy.DOWNSTREAM` names -- empty
# means it blocks nothing and is reported as work, not as a halt.
#
# Every row below was written against a **real SCT run** on DBMIG_APP and
# DBMIG_TELCO (2026-09-17), using SCT's own recommendation text. Codes not seen
# on those estates are deliberately absent rather than guessed.
ROUTES: dict[str, dict] = {
    # ---- source defects: wrong in Oracle, still wrong after migrating -------
    "5659": _r(
        SOURCE, MODEL,
        "A table with no primary key cannot be identified row by row, so DMS CDC "
        "cannot apply updates or deletes to it -- the full load copies it and every "
        "later change is silently lost. This is the same defect the rules engine "
        "raises as DQ-001.",
        "A primary key or unique index exists on the table.",
        blocks=["migrate_cdc"],
        how=[
            'Identify the natural key: the column or columns that already uniquely identify a row in the table.',
            "ALTER TABLE ... ADD CONSTRAINT ... PRIMARY KEY on those columns, or a UNIQUE index if a primary key would change the table's semantics.",
            'If no natural key exists, add a surrogate key and backfill it before the migration window -- not during it.',
        ],
        verify=[
            'The chosen columns are genuinely unique today: run the duplicate count before adding the constraint, not after.',
            'Adding the constraint does not lock the table beyond the maintenance window agreed for it.',
            'The application does not rely on inserting the duplicate rows the constraint will now refuse.',
        ]
    ),
    "5984": _r(
        SOURCE, MODEL,
        "NUMBER without precision or scale carries no width information, so SCT "
        "cannot choose an optimised PostgreSQL type and falls back to an arbitrary "
        "numeric. Declaring the precision in Oracle improves the conversion and is "
        "correct regardless of migration.",
        "The source columns declare precision and scale.",
        how=[
            'For each column, find the precision and scale actually in use: SELECT MAX(LENGTH(TRUNC(col))), MAX(SCALE) over the real data.',
            'ALTER TABLE ... MODIFY col NUMBER(p,s) with those values, leaving headroom for growth.',
            'Re-run SCT afterwards: the action item should disappear and the PostgreSQL type mapping becomes numeric(p,s) instead of an unbounded numeric.',
        ],
        verify=[
            'The precision chosen is not narrower than the data: a MODIFY that truncates is rejected by Oracle, but one that fits today can still be too small next year.',
            'The column is not a surrogate key whose generator assumes an unbounded NUMBER.',
            'The application does not depend on storing values wider than the declared precision.',
        ]
    ),

    # ---- target absorbs it: nothing wrong with the source ------------------
    "5639": _r(
        TARGET, AUTO,
        "An Oracle database link becomes a foreign-data wrapper on PostgreSQL. "
        "Nothing is wrong in the source; the target needs the postgres_fdw "
        "extension, which is one statement and available on RDS for PostgreSQL.",
        "CREATE EXTENSION postgres_fdw has run on the target.",
        how=[
            "CREATE EXTENSION postgres_fdw on the target -- available on RDS for PostgreSQL, and the whole of this item's automated part.",
            'For each Oracle database link, CREATE SERVER naming the remote host, then CREATE USER MAPPING with the remote credentials.',
            'IMPORT FOREIGN SCHEMA, or CREATE FOREIGN TABLE for the specific remote objects the code uses.',
        ],
        verify=[
            'The remote credentials are supplied outside the DDL -- never in the statement text, which ends up in logs and in this plan.',
            "The remote database is reachable from the target's VPC, which is a network change and not a database one.",
            'A foreign table is an acceptable substitute for the link in terms of transaction semantics: an FDW does not join the remote transaction.',
        ]
    ),
    "5581": _r(
        TARGET, MODEL,
        "An index-organized table is an Oracle storage choice, not a schema "
        "requirement. On PostgreSQL the same table is an ordinary table with its "
        "primary-key index, optionally CLUSTERed. The data model is unchanged.",
        "The table exists on the target as an ordinary table with its PK index.",
        how=[
            'Create the table on the target as an ordinary table with its primary key -- PostgreSQL has no index-organized storage, and the data model is unchanged.',
            'Optionally CLUSTER the table on the primary-key index if reads are range scans on that key.',
            'Note that CLUSTER is a one-off reorganisation, not a maintained property: schedule it if the access pattern needs it.',
        ],
        verify=[
            "Queries relying on the IOT's physical ordering have been re-planned and re-timed on the target.",
            'The primary key is the one the IOT was organised by, not a different unique constraint.',
        ]
    ),
    "5326": _r(
        TARGET, MODEL,
        "Oracle carries an enabled/disabled status inside CREATE for constraints and "
        "triggers; PostgreSQL has no such clause. The object is created and then "
        "altered, so this is a target DDL ordering matter rather than a source "
        "defect.",
        "The target DDL creates the object, then sets its state separately.",
        how=[
            'Create the constraint or trigger on the target without a status clause -- PostgreSQL has none in CREATE.',
            'Then set its state separately: ALTER TABLE ... DROP/ADD for a constraint, or ALTER TABLE ... DISABLE TRIGGER.',
            'Order matters in the DDL: the object must exist before its state can be changed.',
        ],
        verify=[
            "The object's intended state matches the source -- a constraint that was DISABLED in Oracle and arrives ENABLED will reject data the application currently writes.",
            'The migration does not depend on the constraint being disabled during the load and enabled afterwards; if it does, that ordering is explicit in Phase 7 rather than assumed here.',
        ]
    ),
    "5208": _r(
        TARGET, PERSON,
        "A domain index is an Oracle extensibility feature with no PostgreSQL "
        "equivalent. SCT's advice is to use simple indexes, but which index "
        "actually preserves the query's behaviour depends on what the domain index "
        "was doing -- usually Oracle Text. That is a person's judgement, on the "
        "target.",
        "An equivalent access path exists on the target and the queries using it "
        "have been re-tested.",
        how=[
            'Establish what the domain index actually does -- on these estates it is Oracle Text, so the queries using it are CONTAINS or CATSEARCH.',
            'Choose the PostgreSQL equivalent: a GIN index over tsvector for full-text search, or pg_trgm for fuzzy matching.',
            'Rewrite the queries: CONTAINS has no direct translation, and the ranking behaviour differs.',
        ],
        verify=[
            'The chosen index preserves the *results* the application expects, not just the performance: text search ranking is not portable.',
            'The queries have been re-tested against real data volumes, because a GIN index behaves differently from Oracle Text at scale.',
        ]
    ),

    # ---- a decision, not a statement ---------------------------------------
    "5200": _r(
        DECISION, PERSON,
        "An external table reads a file from a database directory. RDS has no "
        "filesystem to read, so the choice is between S3 integration, loading the "
        "data into an ordinary table, or moving the feed outside the database. This "
        "is the same blocker the rules engine raises as RDS-004 -- and SCT finds "
        "more occurrences of it.",
        "A loading approach is chosen and recorded, and the external table no "
        "longer sits in the migration path.",
        blocks=["migrate_full_load", "migrate_cdc"],
        how=[
            "Decide the loading approach. Three real options: S3 integration with an RDS directory, loading the file's contents into an ordinary table before the migration, or moving the feed out of the database entirely.",
            'Record the decision -- this is a blocker until it is made, and the gate reports it as one.',
            'Remove the external table from the migration scope once the feed has another home.',
        ],
        verify=[
            'Whoever owns the upstream feed has agreed to the new arrangement: this is not a database-only change.',
            'The data is still arriving during the migration window, or the gap is understood and accepted.',
        ]
    ),
    "9994": _r(
        DECISION, PERSON,
        "SCT cannot convert the object at all -- on these estates, Oracle Advanced "
        "Queuing. PostgreSQL has no AQ; the replacement is a queue table with "
        "SKIP LOCKED, or an external broker such as SQS. No SQL can be drafted "
        "until that choice is made. Phase 4b already routes AQ to a person, so "
        "this is reported rather than halting twice.",
        "A queuing approach is chosen and the objects are re-planned under it.",
        how=[
            'Decide the queuing approach. A queue table with SELECT ... FOR UPDATE SKIP LOCKED covers most AQ usage; an external broker (SQS) is the alternative where ordering and retry policy matter.',
            'Re-plan the dependent objects under that choice -- the enqueue and dequeue procedures are rewritten, not translated.',
            'Phase 4b routes AQ objects to a person already; this is the same decision seen from the assessment.',
        ],
        verify=[
            "Message ordering, retry and dead-letter behaviour match what the application relies on -- AQ's guarantees are not SKIP LOCKED's.",
            'Any in-flight messages at cutover are accounted for.',
        ]
    ),

    # ---- a person writes the code ------------------------------------------
    "5550": _r(
        HUMAN, MODEL,
        "ROWID is Oracle's physical row address. PostgreSQL's ctid is not a stable "
        "equivalent, so code depending on ROWID needs rewriting around a real key. "
        "A model can draft that rewrite; it is reviewed and compiled before it is "
        "kept.",
        "The converted code no longer references ROWID and compiles on the target.",
        how=[
            'Find what the code uses ROWID for. Usually it is a self-referencing UPDATE or a duplicate-row cleanup.',
            "Replace it with the table's real key. If none exists, that is SCT 5659 on the same table and must be fixed first.",
            "PostgreSQL's ctid is *not* a substitute: it changes on UPDATE and is invalid across vacuum.",
        ],
        verify=[
            'The replacement key identifies exactly the same rows the ROWID did -- a near-unique key silently updates the wrong row.',
            'The converted code compiles and its behaviour has been tested on real data, not just parsed.',
        ]
    ),
    "5034": _r(
        HUMAN, MODEL,
        "COMMIT or ROLLBACK inside a procedure behaves differently on PostgreSQL, "
        "where a function runs inside the caller's transaction. The fix is usually "
        "a PROCEDURE rather than a FUNCTION, or moving the commit to the caller -- "
        "both rewrites, both reviewable.",
        "The converted code compiles and its transaction boundaries have been "
        "confirmed by a person.",
        how=[
            'Convert the object as a PROCEDURE, not a FUNCTION: PostgreSQL allows transaction control only in a procedure called with CALL.',
            'If the caller needs a return value, split it -- a procedure that commits, and a function that reads.',
            'Alternatively move the COMMIT to the caller, which is usually the more honest design.',
        ],
        verify=[
            'The transaction boundaries are the ones the application expects: moving a COMMIT changes what is atomic.',
            'Nothing calls this from inside another transaction that would then fail on the commit.',
        ]
    ),
    "5028": _r(
        HUMAN, MODEL,
        "SCT emitted a method stub for a data type it cannot convert. A stub "
        "compiles and does nothing, which is more dangerous than a failure -- it "
        "must be filled in or removed deliberately.",
        "Every stub is either implemented or removed, and the result compiles.",
        how=[
            'SCT emitted a method stub for a type it cannot convert. Find each stub -- they compile and do nothing, which is worse than failing.',
            'Implement it, or remove it deliberately and fix the callers.',
            'Do not leave a stub in place: a function that silently returns NULL is a defect that surfaces in production.',
        ],
        verify=[
            'Every stub is accounted for -- implemented or removed, none left compiling and returning nothing.',
            'The callers handle the new behaviour.',
        ]
    ),
    "5584": _r(
        HUMAN, PERSON,
        "SCT converted the function but flagged it for review -- typically time-zone "
        "handling, where Oracle and PostgreSQL differ in what a bare timestamp "
        "means. A model cannot know the intended zone; only the code's owner can.",
        "A person has confirmed the converted function's semantics.",
        how=[
            "Read the converted function against the original, specifically for time-zone handling: Oracle's DATE and PostgreSQL's timestamp differ in what a bare value means.",
            'Decide whether the column should be timestamptz, and if so what zone the existing data is in.',
            'Test with data either side of a daylight-saving boundary.',
        ],
        verify=[
            'The intended time zone is confirmed by whoever owns the data -- this cannot be inferred from the code.',
            'Existing stored values are interpreted the same way after the migration as before it.',
        ]
    ),
    "9997": _r(
        HUMAN, PERSON,
        "SCT could not resolve an object the code references. Either it is genuinely "
        "missing from the source -- which is a defect worth knowing about on its own "
        "-- or the collector account cannot see it. Both need a person to look.",
        "The referenced object is confirmed present and visible, or the reference "
        "is removed.",
        how=[
            'Check whether the referenced object exists in the source at all: it may be genuinely missing, which is a defect worth knowing about independently of the migration.',
            "If it exists, check the collector account can see it -- a grant gap looks identical to a missing object from SCT's side.",
            'Either grant the visibility and re-run SCT, or remove the dead reference from the code.',
        ],
        verify=[
            'The distinction has been established: missing object, or missing grant. They have different fixes and only one is a code change.',
        ]
    ),
    "9996": _r(
        HUMAN, PERSON,
        "An internal SCT converter error. SCT's own advice is to report it to AWS. "
        "There is nothing to draft: the tool failed on this object, so it must be "
        "converted by hand and the failure raised with AWS.",
        "The object is converted manually and compiles on the target.",
        how=[
            'An internal SCT converter error -- the tool failed on this object, so there is nothing to review.',
            'Convert it by hand, using the source as the specification.',
            "Report the failure to AWS, as SCT's own advice says: a converter bug that nobody reports stays.",
        ],
        verify=[
            'The hand-written conversion compiles on the target and has been tested, because no tool checked it.',
        ]
    ),
}

# What happens to a code nobody has mapped yet. Not "automatic", deliberately.
UNMAPPED = _r(
    HUMAN, PERSON,
    "This AWS SCT action item is not in DBShift's routing table, so nothing is "
    "known about where it must be fixed or whether it is safe to automate. It is "
    "routed to a person on purpose: an unmapped item treated as automatic is the "
    "one failure mode this table exists to prevent.",
    "The item is reviewed, acted on, and a row is added to sct/route.py so the "
    "next run routes it without a person having to decide again.",
    how=[
        "Read the item in AWS SCT's own report -- the PDF and CSV carry SCT's "
        "full description and recommended action.",
        "Decide where the fix belongs and who may make it, then add a row to "
        "sct/route.py so the next run does not need this decision again.",
    ],
    verify=[
        "Nothing was automated on the strength of an unmapped item: it is here "
        "precisely because this build knows nothing about it.",
    ],
)


def route(issue_code: str) -> dict:
    """Where and by whom one SCT action item must be fixed.

    Always returns a route. An unknown code gets `UNMAPPED`, flagged so the
    console and the gate can show it as unmapped rather than as a decision
    somebody made.
    """
    code = str(issue_code or "").strip()
    row = ROUTES.get(code)
    if row is None:
        return {**UNMAPPED, "issue_code": code, "mapped": False}
    return {**row, "issue_code": code, "mapped": True}


def annotate(issues: list[dict]) -> list[dict]:
    """Attach a route to each parsed SCT issue, leaving SCT's own fields alone."""
    out = []
    for issue in issues:
        r = route(issue.get("issue_code"))
        out.append({
            **issue,
            "where": r["where"],
            "where_label": WHERE_LABEL[r["where"]],
            "where_short": WHERE_SHORT[r["where"]],
            "who": r["who"],
            "who_label": WHO_LABEL[r["who"]],
            "who_short": WHO_SHORT[r["who"]],
            "route_why": r["why"],
            "clears_when": r["clears_when"],
            "blocks": r["blocks"],
            "how": r.get("how") or [],
            "verify": r.get("verify") or [],
            "route_mapped": r["mapped"],
        })
    return out


def segregate(issues: list[dict]) -> dict:
    """Group annotated issues by where the work lands, worst first within each.

    The four groups are the answer to "who has to do what": the source team, the
    target build, an architecture decision, and code somebody must write.
    """
    annotated = annotate(issues) if issues and "where" not in issues[0] else list(issues)
    groups = {w: [] for w in (SOURCE, TARGET, DECISION, HUMAN)}
    for issue in annotated:
        groups[issue["where"]].append(issue)

    return {
        "groups": [
            {
                "where": w,
                "label": WHERE_LABEL[w],
                "meaning": WHERE_MEANING[w],
                "items": groups[w],
                "item_count": len(groups[w]),
                "occurrence_count": sum(i.get("occurrences", 0) for i in groups[w]),
                # Who can act, within this group.
                "by_who": {
                    who: sum(1 for i in groups[w] if i["who"] == who)
                    for who in (AUTO, MODEL, PERSON)
                },
            }
            for w in (SOURCE, TARGET, DECISION, HUMAN)
        ],
        "unmapped": [i for i in annotated if not i.get("route_mapped", True)],
        "totals": {
            "items": len(annotated),
            "occurrences": sum(i.get("occurrences", 0) for i in annotated),
            "automatable": sum(1 for i in annotated if i["who"] == AUTO),
            "ai_draftable": sum(1 for i in annotated if i["who"] == MODEL),
            "human_only": sum(1 for i in annotated if i["who"] == PERSON),
        },
    }


def blocking(issues: list[dict], phases: list[str] | None = None) -> list[dict]:
    """Annotated issues that stand in front of a downstream phase.

    `phases` narrows to the phases actually in scope for this migration -- on a
    full-load run, a CDC-only blocker blocks nothing, which is the same rule
    Phase 2 applies to the rules engine's CDC-only findings.
    """
    annotated = annotate(issues) if issues and "where" not in issues[0] else list(issues)
    out = []
    for issue in annotated:
        hits = issue.get("blocks") or []
        if phases is not None:
            hits = [p for p in hits if p in phases]
        if hits:
            out.append({**issue, "blocks_in_scope": hits})
    return out
