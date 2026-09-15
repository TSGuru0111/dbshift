"""What DMS leaves behind, and who resolves each piece.

DMS moves rows. It does not move sequences, views, materialized views, external
tables, or anything whose data lives outside the database -- and when a table
fails mid-load it suspends it and carries on. Every one of those is real work
that a migration must still do, and the honest thing is to name it rather than
report "migration complete" over a partial result.

Each item is routed the way the rest of this project routes work:

    RULE     one correct answer, derivable from what discovery recorded
    MODEL    needs judgement -- reading a view's SQL and rewriting it
    PERSON   no target equivalent; someone decides what happens instead

**The MODEL tier does not run today.** Bedrock `InvokeModel` is blocked on this
account (no payment instrument), so a model item is answered from a labelled
hand-written stand-in in `bedrock/static/dms/` or, when none exists, it is
reported as MODEL_REQUIRED and nothing is invented. `source` on every item says
which of those happened, and the console shows it. Nothing here is applied.
"""

from __future__ import annotations

import json
from pathlib import Path

RULE = "RULE"
MODEL = "MODEL"
PERSON = "PERSON"

READY = "READY"                    # an answer exists and was produced deterministically
STATIC = "STATIC_FIXTURE"          # answered from a labelled hand-written stand-in
MODEL_REQUIRED = "MODEL_REQUIRED"  # needs the model tier, which is not available
MODEL_CONVERTED = "MODEL_CONVERTED"  # the model answered; nothing is applied
MANUAL = "MANUAL"                  # needs a person; no automatic answer is possible

STATIC_DIR = Path(__file__).resolve().parent.parent / "bedrock" / "static" / "dms"


def _item(*, kind, name, tier, status, what, why, sql=None, source="rule", detail=None):
    return {"kind": kind, "object_name": name, "tier": tier, "status": status,
            "what_is_needed": what, "why": why, "sql": sql, "source": source,
            "detail": detail}


# --- the rule tier -----------------------------------------------------------

def _sequences(rows: list[dict], schema: str, lowercase: bool) -> list[dict]:
    """Sequences are the clearest RULE case, and the most dangerous to skip.

    DMS never migrates a sequence. If nobody sets the target's sequence to the
    source's current value, the first insert on the target reuses a number that
    already exists -- a primary key violation at best, silently duplicated
    business keys at worst. The fix is arithmetic over what discovery recorded,
    so there is exactly one right answer and no judgement involved.
    """
    out = []
    for r in rows or []:
        if r.get("sequence_owner") != schema and r.get("owner") != schema:
            continue
        name = r.get("sequence_name")
        if not name:
            continue
        last = r.get("last_number")
        inc = r.get("increment_by") or 1
        target = f"{schema.lower()}.{name.lower()}" if lowercase else f"{schema}.{name}"
        sql = None
        if last is not None:
            # last_number is the next value Oracle will hand out (cached values
            # included), so it is the right restart point -- not last + 1, which
            # would skip one, and not the cached low-water mark, which would
            # collide.
            sql = f"ALTER SEQUENCE {target} RESTART WITH {last};"
        out.append(_item(
            kind="sequence", name=name, tier=RULE,
            status=READY if sql else MANUAL,
            what="set the target sequence to the source's current value",
            why="DMS does not migrate sequences. Left at 1, the first insert on the target "
                "reuses a key that already exists -- a constraint violation if you are lucky, "
                "duplicate business keys if you are not.",
            sql=sql,
            detail=f"source last_number {last}, increment {inc}" if last is not None
                   else "discovery did not record last_number; read it from the source"))
    return out


def _materialized_views(rows: list[dict], schema: str) -> list[dict]:
    """A materialized view is a query plus its stored result.

    The result is not migrated -- there is no point copying rows that the target
    can compute. The query has to be rewritten, which is the model tier's job,
    and then refreshed once on the target.
    """
    out = []
    for r in rows or []:
        if r.get("owner") != schema:
            continue
        name = r.get("mview_name") or r.get("object_name")
        if not name:
            continue
        out.append(_item(
            kind="materialized_view", name=name, tier=MODEL, status=MODEL_REQUIRED,
            what="rewrite the defining query for PostgreSQL, create the view, refresh it once",
            why="DMS replicates tables, not derived results. The rows would be stale the moment "
                "they landed; the view must be defined and refreshed on the target instead.",
            detail=(r.get("refresh_mode") or "") + " " + (r.get("refresh_method") or "")))
    return out


# Views Oracle creates behind a feature. AQ$LOAN_EVENT_QTAB and AQ$_..._F are
# the queue's own interface, not application views: they disappear with the
# queue rather than being rewritten, and listing them as work to do would
# overstate the job by the number of queues in the estate.
INTERNAL_VIEW_PREFIXES = ("AQ$", "MVIEW$", "SYS_", "DR$")


def _views(rows: list[dict], schema: str) -> list[dict]:
    out = []
    for r in rows or []:
        if r.get("owner") != schema:
            continue
        name = r.get("view_name") or r.get("object_name")
        if not name:
            continue
        if name.upper().startswith(INTERNAL_VIEW_PREFIXES):
            continue
        out.append(_item(
            kind="view", name=name, tier=MODEL, status=MODEL_REQUIRED,
            what="rewrite the view's SQL for PostgreSQL and create it",
            why="A view is a stored query, not data. DMS has nothing to copy, and Oracle SQL "
                "does not always mean the same thing in PostgreSQL -- NVL, DECODE, ROWNUM, "
                "outer-join syntax and date arithmetic all differ."))
    return out


def _external_tables(rows: list[dict], schema: str) -> list[dict]:
    out = []
    for r in rows or []:
        if r.get("owner") != schema:
            continue
        name = r.get("table_name")
        if not name:
            continue
        out.append(_item(
            kind="external_table", name=name, tier=PERSON, status=MANUAL,
            what="decide where the file lives and how the target reads it",
            why="The data is in a file on the database server, and a managed target has no such "
                "filesystem. PostgreSQL has no external table: the choice is a foreign data "
                "wrapper, a load into an ordinary table, or moving the feed elsewhere entirely. "
                "That is a design decision, not a conversion -- finding RDS-004."))
    return out


def _suspended(table_stats: list[dict]) -> list[dict]:
    """A table DMS gave up on mid-load.

    The task keeps going by design -- `TableErrorPolicy: SUSPEND_TABLE` -- so a
    run can finish "successfully" with a table holding partial rows. That is the
    failure mode most worth surfacing, because nothing else in the pipeline
    looks wrong.
    """
    out = []
    for t in table_stats or []:
        if t.get("state") not in ("Table error", "Error"):
            continue
        name = t.get("table")
        out.append(_item(
            kind="suspended_table", name=name, tier=MODEL, status=MODEL_REQUIRED,
            what="diagnose why the load failed and repair it",
            why=f"DMS suspended this table after {t.get('full_load_errors', 0)} row error(s) and "
                "carried on with the rest. The table holds a partial copy, and no later phase "
                "would call that a failure without this entry.",
            detail=f"{t.get('full_load_rows', 0)} row(s) loaded, "
                   f"{t.get('full_load_errors', 0)} error(s)"))
    return out


# --- the model tier, while it cannot run -------------------------------------

def _static_answers(estate: str) -> dict:
    """Hand-written stand-ins, loaded from disk and clearly labelled.

    Same contract as `bedrock/static/convert/`: an entry is keyed on kind and
    object name, and it is never presented as model output.
    """
    path = STATIC_DIR / f"{estate}.json"
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {(o.get("kind"), (o.get("object_name") or "").upper()): o
            for o in doc.get("outputs", [])}


def apply_static(items: list[dict], estate: str) -> list[dict]:
    """Answer what the model tier would have answered, from the fixtures.

    An item with no fixture keeps MODEL_REQUIRED. Nothing is guessed, and a
    fixture is never relabelled as model output -- `source` says
    `static_fixture` and `model_id` stays null, which is what the Phase 10
    report reads.
    """
    answers = _static_answers(estate)
    out = []
    for i in items:
        if i["status"] != MODEL_REQUIRED:
            out.append(i)
            continue
        fixture = answers.get((i["kind"], (i["object_name"] or "").upper()))
        if not fixture:
            out.append(i)
            continue
        out.append({**i, "status": STATIC, "source": "static_fixture", "model_id": None,
                    "sql": fixture.get("sql"),
                    "detail": fixture.get("explain") or i.get("detail"),
                    "caveat": fixture.get("caveat")})
    return out


# --- assembly ----------------------------------------------------------------

VIEW_PROMPT = """Rewrite this Oracle view definition for PostgreSQL.

OBJECT   {kind} {schema}.{name}
ORACLE SQL
{sql}

The target schema is `{target_schema}` -- every identifier is lower case there,
because the data migration folds names down. Qualify the tables you reference
with that schema so the view resolves regardless of who queries it.

Reply with JSON only, no prose and no code fence:
{{"sql": "the CREATE statement(s), semicolon-separated",
  "explain": "one or two sentences on what you changed and why",
  "caveat": "what a reviewer must check, or the empty string"}}

Rules:
- Translate Oracle functions that differ: NVL, DECODE, SYSDATE, TO_DATE, ROWNUM,
  outer-join (+) syntax.
- A materialized view should be created WITH NO DATA and refreshed separately,
  so creating it does not block on a long scan.
- If you cannot translate it safely, return an empty "sql" and say why. That is
  a correct answer.
"""


def _model_answer(item: dict, source_sql: str, schema: str, client=None) -> dict | None:
    """Ask the model to rewrite one view. Returns None when it cannot.

    Nothing here is applied. The SQL is shown for review alongside the item,
    exactly as a hand-written stand-in would be, and `source` says the model
    wrote it.
    """
    import json

    from bedrock.client import BedrockClient, BedrockError

    if not source_sql:
        return None
    client = client or BedrockClient()
    try:
        reply = client.complete(
            "reasoning",
            VIEW_PROMPT.format(kind=item["kind"].replace("_", " "), schema=schema,
                               name=item["object_name"], sql=source_sql[:6000],
                               target_schema=schema.lower()),
            max_tokens=1200)
    except BedrockError:
        return None

    text = (reply.get("text") or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, KeyError):
        return None
    if not (parsed.get("sql") or "").strip():
        return None

    return {**item, "status": MODEL_CONVERTED, "source": "bedrock",
            "model_id": reply.get("model_id"),
            "sql": parsed["sql"].strip(),
            "detail": str(parsed.get("explain") or "").strip()[:500] or item.get("detail"),
            "caveat": str(parsed.get("caveat") or "").strip()[:500] or None}


def apply_model(items: list[dict], schema: str, datasets: dict, client=None) -> list[dict]:
    """Answer the MODEL_REQUIRED items with the model, where it can."""
    text_by_name = {}
    for row in (datasets.get("views") or []):
        if row.get("owner") == schema and row.get("view_name"):
            text_by_name[row["view_name"].upper()] = row.get("text") or ""
    for row in (datasets.get("materialized_views") or []):
        if row.get("owner") == schema and row.get("mview_name"):
            text_by_name[row["mview_name"].upper()] = row.get("query") or ""

    out = []
    for i in items:
        if i["status"] != MODEL_REQUIRED:
            out.append(i)
            continue
        answered = _model_answer(i, text_by_name.get((i["object_name"] or "").upper(), ""),
                                 schema, client=client)
        out.append(answered or i)
    return out


def build(*, schema: str, datasets: dict, lowercase: bool,
          table_stats: list[dict] | None = None, model_available: bool = False,
          client=None) -> dict:
    """Everything the DMS run did not finish, routed and counted."""
    items: list[dict] = []
    items += _sequences(datasets.get("sequences") or [], schema, lowercase)
    items += _views(datasets.get("views") or [], schema)
    items += _materialized_views(datasets.get("materialized_views") or [], schema)
    items += _external_tables(datasets.get("external_tables") or [], schema)
    items += _suspended(table_stats or [])

    if model_available:
        items = apply_model(items, schema, datasets, client=client)
        # Anything the model could not answer still falls back to a labelled
        # stand-in, so a transient model failure does not lose a known answer.
        items = apply_static(items, schema)
    else:
        items = apply_static(items, schema)

    by_status: dict[str, int] = {}
    for i in items:
        by_status[i["status"]] = by_status.get(i["status"], 0) + 1

    return {
        "items": sorted(items, key=lambda i: (i["kind"], i["object_name"] or "")),
        "total": len(items),
        "by_status": by_status,
        "by_tier": {t: sum(1 for i in items if i["tier"] == t) for t in (RULE, MODEL, PERSON)},
        "model_available": model_available,
        "model_note": (
            "Bedrock InvokeModel is blocked on this account, so no model ran. Items needing "
            "judgement are answered from labelled hand-written stand-ins where one exists, and "
            "reported as MODEL_REQUIRED where none does. Nothing is invented, and no stand-in is "
            "presented as model output."
            if not model_available else
            "The model tier is live. Items needing judgement were rewritten by the model and "
            "are labelled MODEL_CONVERTED with the model id; nothing is applied, and anything "
            "it could not answer fell back to a labelled stand-in or stayed MODEL_REQUIRED."),
        "nothing_applied": True,
    }
