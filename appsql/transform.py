"""Where a converted application SQL statement comes from.

Sources, tried in order -- the same order `convert/plan.py` and
`remediate/sct_generate.py` use, for the same reason:

    rule      deterministic, no model, no cost. Preferred wherever a statement
              is entirely inside the covered subset. Two runs produce the same
              bytes.
    bedrock   the model drafts the rewrite for statements the rules decline. It
              is given the construct catalogue's own note for each construct it
              must handle, told what the gates will check, and told plainly
              that its output is gated.
    none      nothing is drafted, and the statement routes to a person.

Whichever produced a conversion is recorded on it. **Nothing here ever claims a
model ran when it did not**, and nothing here decides whether a conversion may
be applied -- that is `gates.run`, and the gates do not negotiate.

**The manual tier is never sent to the model.** A construct with no correct
automatic rewrite does not become one because a model is fluent: asking for a
rewrite of Oracle's empty-string semantics produces confident SQL that is
wrong, and a dropped optimizer hint needs reporting rather than converting. The
model is only asked about the model tier, which is the tier defined as "a
faithful rewrite exists and needs judgement".

That distinction is the whole reason the catalogue carries three tiers rather
than "automatic" and "hard".
"""

from __future__ import annotations

import json
import logging

from . import classify, rules

log = logging.getLogger("appsql.transform")


class TransformUnavailable(RuntimeError):
    """No source could draft a conversion for this statement."""


# Constructs that must never be sent to the model, with what to do instead.
# Keyed by construct id so it is obvious which the rule covers.
NEVER_DRAFTED = {
    "EMPTY_STRING_NULL":
        "Oracle stores '' as NULL and PostgreSQL stores a zero-length string. No SQL "
        "rewrite fixes this: every IS NULL test, unique constraint and NOT NULL column "
        "over the affected column behaves differently, so it is an application "
        "redesign. validate/crossengine.py reports the same difference on the data side.",
    "OPTIMIZER_HINT":
        "A hint is a comment, so PostgreSQL ignores it silently rather than failing. It "
        "must be reported as dropped so somebody re-plans the query -- a dropped hint "
        "nobody saw is a performance incident after cutover, not a conversion defect.",
    "ROWID":
        "ctid changes on UPDATE and VACUUM, so there is no stable equivalent. An "
        "application that round-trips a ROWID to identify a row is redesigned around "
        "the primary key; rewriting the SQL alone cannot be correct.",
    "MYBATIS_INTERPOLATION":
        "${} must stay interpolated, which means there is nothing to rewrite. It is "
        "recorded so a reviewer sees the injection site, and left exactly as written.",
    "RESERVED_WORD_OBJECT":
        "Phase 4c renamed the target object because its Oracle name is a PostgreSQL "
        "keyword -- DBMIG_APP.ORDER becomes order_col, and so does its ORDER column. "
        "Only 4c knows the name it chose, so a model asked to rewrite this would invent "
        "one: confident SQL naming an object that does not exist. The statement is "
        "reported with the object so a person applies 4c's own mapping.",
}


PROMPT = """You are rewriting one **Oracle** SQL statement so it runs on \
**PostgreSQL 16**. The statement is embedded in a MyBatis mapper file in a Java \
application, and the application will send your version instead of the original.

THE ORACLE STATEMENT
{sql}

THE CONSTRUCTS THAT MUST CHANGE
{constructs}

{dynamic_note}

Reply with JSON and nothing else:

{{"sql": "the rewritten PostgreSQL statement",
  "constructs": [
    {{"id": "the construct id exactly as given above",
      "state": "translated" or "not_translated",
      "postgres": "what you used, when translated",
      "reason": "why not, when not_translated"}}
  ],
  "caveat": "one sentence on anything a reviewer must check, or null"}}

Rules you must follow, because a gate checks each one and will reject your reply:

- Account for **every** construct listed above, by its exact id. A construct you \
translated is "translated" and its PostgreSQL form must actually appear in your \
SQL. One you could not is "not_translated" with a reason. A construct you do not \
mention is treated as silently dropped and the conversion is rejected.
- Keep the statement the **same kind**. A SELECT stays a SELECT. Never turn a \
SELECT into an UPDATE or DELETE.
- Keep **every** `#{{name}}` bind parameter exactly as written, including the \
braces. Dropping one silently stops the statement filtering on it.
- Keep **every** `${{name}}` exactly as written. It is MyBatis string \
interpolation for a column or table name. Rewriting it to `#{{name}}` turns a \
column name into a quoted literal and the query fails at runtime.
- Keep every MyBatis tag -- `<if>`, `<where>`, `<foreach>`, `<selectKey>` -- and \
their attributes byte for byte. The `test` attributes are Java, not SQL.
- Write only the one statement. No DDL, no transaction control, no comments \
explaining it.
- Preserve the semantics, not the text. If preserving them is impossible, say so \
in "not_translated" rather than writing something that merely runs."""

_DYNAMIC_NOTE = """THIS STATEMENT IS DYNAMIC
It carries MyBatis tags ({tags}), so its final text depends on the parameters \
supplied at runtime. Your rewrite must keep the tags so every branch still \
works -- you are rewriting a family of statements, not one."""


def _constructs_for_prompt(found: list[dict]) -> str:
    """What the model is told about each construct: the catalogue's own note.

    The note is written once, reviewed like the rest of the catalogue, and
    reused here rather than restated in the prompt -- so a correction to the
    catalogue reaches the model without anyone editing a prompt string.
    """
    lines = []
    for con in found:
        lines.append(json.dumps({
            "id": con["id"],
            "oracle": con["name"],
            "postgresql": con["postgres"],
            "why_it_is_not_a_text_swap": con["note"],
            "the_oracle_form_must_not_survive": bool(con.get("residue")),
        }, indent=2))
    return "\n".join(lines)


def bedrock_transform(stmt: dict, found: list[dict], client=None) -> dict:
    """Ask the model to rewrite one statement. The gates still decide.

    Raises TransformUnavailable when the model cannot answer or answers with
    something structurally unusable. Structural validation only: whether the
    rewrite is *correct* is the gates' job, and they are better at it than any
    check here would be.
    """
    from bedrock.client import BedrockClient, BedrockError

    drafted = [c for c in found if c["id"] not in NEVER_DRAFTED]
    if not drafted:
        raise TransformUnavailable(
            "every construct in this statement is one no model is asked about: "
            + ", ".join(c["id"] for c in found))

    dynamic_note = ""
    if stmt.get("dynamic"):
        dynamic_note = _DYNAMIC_NOTE.format(tags=", ".join(stmt["dynamic_tags"]))

    prompt = PROMPT.format(
        sql=stmt["sql"],
        constructs=_constructs_for_prompt(found),
        dynamic_note=dynamic_note,
    )

    client = client or BedrockClient()
    try:
        reply = client.complete("reasoning", prompt, max_tokens=2000)
    except BedrockError as exc:
        raise TransformUnavailable(f"the model call failed: {exc}") from exc

    text = (reply.get("text") or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, KeyError) as exc:
        raise TransformUnavailable(f"the reply was not JSON: {str(exc)[:80]}") from exc
    if not isinstance(parsed, dict):
        raise TransformUnavailable("the model returned JSON that is not an object")

    sql = (parsed.get("sql") or "").strip()
    if not sql:
        raise TransformUnavailable("the model returned no SQL")

    accounting = parsed.get("constructs")
    if not isinstance(accounting, list) or not accounting:
        raise TransformUnavailable("the model returned no construct accounting")
    cleaned = []
    for entry in accounting:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise TransformUnavailable("a construct entry has no id")
        if entry.get("state") not in ("translated", "not_translated"):
            raise TransformUnavailable(
                f"construct {entry['id']} has state {entry.get('state')!r}, which is "
                "neither translated nor not_translated")
        cleaned.append({k: v for k, v in entry.items()
                        if k in ("id", "state", "postgres", "reason")})

    return {
        "sql": sql,
        "constructs": cleaned,
        "source": "bedrock",
        "model_id": reply.get("model_id"),
        "tokens": {"in": reply.get("input_tokens"), "out": reply.get("output_tokens")},
        "caveat": (str(parsed["caveat"]).strip()[:400]
                   if parsed.get("caveat") else None),
        # The probe the parse gate sends. Derived from the model's SQL the same
        # way the extractor derives one from the source, so the two are
        # comparable.
        "probe_sql": sql,
    }


def manual_note(found: list[dict]) -> dict | None:
    """What a person is told about a statement no model is asked about.

    Returns the reason and the construct that caused it, or None when nothing
    in this statement is in the never-drafted set.
    """
    for con in found:
        if con["id"] in NEVER_DRAFTED:
            return {
                "construct_id": con["id"],
                "construct": con["name"],
                "why_no_draft": NEVER_DRAFTED[con["id"]],
                "postgres": con["postgres"],
            }
    return None


def transform(stmt: dict, model_mode: str = "off", client=None) -> dict:
    """Convert one statement, from whichever source can.

    `model_mode`:
        off     rules only; a declined statement routes to a person
        live    Bedrock drafts what the rules decline, and the gates decide

    Never raises: a statement that cannot be converted comes back with
    `source: None` and the reason, because a phase that dies on an
    unconvertible statement cannot report on the ones it did convert.
    """
    found = classify.scan(stmt["sql"])

    # A construct no model is asked about short-circuits everything, including
    # the rules -- the rules would decline it anyway, and this gives the
    # specific reason rather than a generic one.
    manual = manual_note(found)
    if manual:
        return {
            "sql": None, "constructs": [], "source": None,
            "model_id": None,
            "reason": f"{manual['construct']}: {manual['why_no_draft']}",
            "manual": manual,
        }

    try:
        conv = rules.convert(stmt["sql"], found)
        conv["probe_sql"] = conv["sql"]
        return conv
    except rules.Declined as exc:
        declined_because = str(exc)

    if model_mode != "live":
        return {
            "sql": None, "constructs": [], "source": None, "model_id": None,
            "reason": f"the rules declined ({declined_because}) and the model tier is off",
        }

    try:
        return bedrock_transform(stmt, found, client=client)
    except TransformUnavailable as exc:
        log.warning("appsql: no conversion for %s: %s", stmt["statement_id"], exc)
        return {
            "sql": None, "constructs": [], "source": None, "model_id": None,
            "reason": f"the rules declined ({declined_because}) and the model could "
                      f"not answer: {exc}",
        }
