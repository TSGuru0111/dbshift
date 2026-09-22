"""Where a fix for an AWS SCT action item comes from.

Sources, tried in order -- the same order `generate.py` uses for the rules
engine, for the same reason:

    template   deterministic, no model, no cost. Preferred wherever an SCT item
               has exactly one correct remedy. SCT 5639 is the clean case: the
               fix is `CREATE EXTENSION postgres_fdw`, always, and asking a
               model would be slower, costlier and less reliable.
    bedrock    the model drafts SQL for items with no template. It is told which
               engine it is writing for, given SCT's own recommendation, and
               told plainly that its output is gated.
    none       nothing is drafted; the item routes to a person.

Whichever produced a fix is recorded on it. **Nothing here ever claims a model
ran when it did not**, and nothing here decides whether a fix may be applied --
that is `sct_plan.py` running the gates, and the gates do not negotiate.

**The prompt differs by engine, and that is the point of this module.** A
source-side fix is DDL against a client's production Oracle, so the model is
held to the narrow Oracle allow-list. A target-side fix is DDL against a
PostgreSQL database being built, so it is held to `pg_policy`. Giving the model
one prompt for both would produce Oracle syntax for the target and vice versa.
"""

from __future__ import annotations

import json
import logging

from sct import route as sct_route

log = logging.getLogger("remediate.sct_generate")


class GenerationUnavailable(RuntimeError):
    """No source could draft a fix for this action item."""


# --------------------------------------------------------------- templates
#
# One entry per SCT issue code with exactly one correct remedy. A template is
# preferred over the model wherever it exists: it is free, instant, identical
# every run, and cannot hallucinate. Keyed by SCT's code so it is obvious which
# item a template covers.


def _t_5639(item: dict) -> dict:
    """SCT 5639 -- an Oracle database link becomes a foreign-data wrapper.

    The entire fix is one extension, available on RDS for PostgreSQL. SCT's own
    recommendation says exactly this, which is why no model is involved.
    """
    return {
        "sql": "CREATE EXTENSION IF NOT EXISTS postgres_fdw",
        "rollback_sql": "DROP EXTENSION IF EXISTS postgres_fdw",
        "explain": (
            "Installs the foreign-data wrapper PostgreSQL uses in place of an Oracle "
            "database link. The server and user mapping are configured afterwards, per "
            "link, which needs the remote credentials and so is a separate step."
        ),
        "caveat": (
            "The extension alone does not recreate the link. A reviewer must confirm the "
            "CREATE SERVER and CREATE USER MAPPING for each link are planned, with "
            "credentials held outside the DDL."
        ),
    }


TEMPLATES = {
    "5639": _t_5639,
}


def template_fix(item: dict) -> dict | None:
    builder = TEMPLATES.get(str(item.get("issue_code")))
    return builder(item) if builder else None


# ------------------------------------------------------------------ the model

# The braces in the JSON example are **doubled on purpose**: these prompts are
# built with `str.format()`, which would otherwise read `{"sql": ...}` as a
# field name and raise KeyError. `generate.py` does the same for the same
# reason. Do not "tidy" them to single braces.
_COMMON_RULES = """
You are proposing, not applying. Every statement you return is checked against
an allow-list, executed against a throwaway copy, and approved by a named person
before it touches anything. Nothing you return is trusted, and a rejected fix
sends the item to a human -- so a careful answer helps and a bold one does not.

Reply with JSON only, no prose and no code fence:
{{"sql": "one or more statements, semicolon-separated",
 "rollback_sql": "the statements that undo it",
 "explain": "one or two sentences: what this does and why it addresses the item",
 "caveat": "what a reviewer must check before approving, or the empty string"}}

If the item cannot be fixed safely with the statements you are allowed to write,
return an empty "sql" and say why in "explain". **That is a correct answer, not
a failure** -- it routes the item to a person, which is where it belongs.
"""

SOURCE_PROMPT = """You are proposing a fix to an **Oracle** database that is
about to be migrated to Amazon RDS. AWS Schema Conversion Tool raised this
action item against it.

SCT ACTION ITEM
{item}

WHY THIS IS A SOURCE-SIDE FIX
{route_why}

WHAT CLEARS IT
{clears_when}

This database is in production and serving traffic. You may write only:
  CREATE INDEX / CREATE UNIQUE INDEX
  ALTER TABLE
  ALTER INDEX
  BEGIN DBMS_STATS...END

You may never write: DROP, TRUNCATE, DELETE, GRANT, REVOKE, ALTER USER,
ALTER SYSTEM, SHUTDOWN, STARTUP, DROP CONSTRAINT, or CREATE OR REPLACE of any
package, procedure, function or trigger. Those are refused by policy, so writing
one wastes the attempt.
""" + _COMMON_RULES

TARGET_PROMPT = """You are proposing a fix to an **Amazon RDS for PostgreSQL**
target that is being built to receive an Oracle migration. AWS Schema Conversion
Tool raised this action item, and it is the target that must absorb it -- the
Oracle source is not at fault and must not be changed.

SCT ACTION ITEM
{item}

WHY THIS IS A TARGET-SIDE FIX
{route_why}

WHAT CLEARS IT
{clears_when}

Write **PostgreSQL**, not Oracle. You may write only:
  CREATE EXTENSION
  CREATE TABLE / SEQUENCE / TYPE / INDEX
  CREATE OR REPLACE VIEW
  ALTER TABLE / ALTER INDEX / ALTER SEQUENCE
  COMMENT ON, ANALYZE, CLUSTER

You may never write: DROP TABLE/DATABASE/SCHEMA/ROLE, TRUNCATE, DELETE, UPDATE,
GRANT, REVOKE, ALTER ROLE/USER/SYSTEM, COPY FROM PROGRAM, or an untrusted
procedural language. Do not write a dollar-quoted function body -- converted
PL/pgSQL is Phase 4b's job and is compiled and gated there, separately.
""" + _COMMON_RULES


def _item_for_prompt(item: dict) -> dict:
    """What the model is told about the item. SCT's own words, not ours."""
    return {
        "sct_issue_code": item.get("issue_code"),
        "sct_says": item.get("title"),
        "sct_recommended_action": item.get("recommendation"),
        "sct_estimated_complexity": item.get("complexity"),
        "category": item.get("category"),
        "schema": item.get("owner"),
        "object": item.get("object_name"),
        "object_type": item.get("object_type"),
        # A grouped item covers several objects; the model needs to know whether
        # it is writing one statement or a pattern.
        "affected_objects": (item.get("objects") or [])[:40],
        "occurrences": item.get("occurrences"),
    }


def bedrock_fix(item: dict, route_row: dict, client=None) -> dict:
    """Ask the model to draft a fix for one SCT action item.

    Raises GenerationUnavailable when the model cannot answer or answers with
    something unusable, so the item routes to a person rather than to a guess.
    """
    from bedrock.client import BedrockClient, BedrockError

    is_source = route_row["where"] == sct_route.SOURCE
    prompt = (SOURCE_PROMPT if is_source else TARGET_PROMPT).format(
        item=json.dumps(_item_for_prompt(item), indent=2),
        route_why=route_row["why"],
        clears_when=route_row["clears_when"],
    )

    client = client or BedrockClient()
    # `complete(tier, prompt)` -- tier first, positionally. Calling it as
    # `complete(prompt, tier=...)` raised TypeError, which the broad handler
    # below then reported as "the model call failed", i.e. a code defect
    # disguised as an unavailable model. Every item came back undrafted on a run
    # where Bedrock was verified working.
    try:
        reply = client.complete("reasoning", prompt)
    except BedrockError as exc:
        raise GenerationUnavailable(f"the model tier is unavailable: {exc}") from exc
    except (TypeError, AttributeError):
        # A wrong call or a changed client contract is this project's bug, not a
        # model outage. Re-raised so it is seen and fixed, rather than silently
        # routing every item to a person and looking like the model declined.
        raise
    except Exception as exc:  # noqa: BLE001 -- a genuine runtime failure routes to a human
        raise GenerationUnavailable(f"the model call failed: {exc}") from exc

    text = (reply.get("text") if isinstance(reply, dict) else str(reply)) or ""
    text = text.strip()
    # Models sometimes fence JSON despite being told not to. Tolerate it rather
    # than discarding an otherwise good answer.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise GenerationUnavailable(
            f"the model did not return JSON: {text[:160]!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise GenerationUnavailable("the model returned JSON that is not an object")

    return {
        "sql": (parsed.get("sql") or "").strip(),
        "rollback_sql": (parsed.get("rollback_sql") or "").strip(),
        "explain": (parsed.get("explain") or "").strip(),
        "caveat": (parsed.get("caveat") or "").strip(),
        "model_id": (reply.get("model_id") if isinstance(reply, dict) else None),
    }


# ------------------------------------------------------------------ dispatch


def build_fix(item: dict, route_row: dict, model_mode: str = "off") -> tuple[dict | None, str]:
    """A fix for one SCT item, and where it came from.

    Returns `(None, source)` when nothing could be drafted -- the caller routes
    the item to a person. A template always wins: free, instant, identical every
    run, and incapable of hallucinating.
    """
    templated = template_fix(item)
    if templated is not None:
        return {**templated, "model_id": None}, "template"

    if model_mode == "off":
        return None, "none"

    if model_mode in ("live", "on"):
        try:
            return bedrock_fix(item, route_row), "bedrock"
        except GenerationUnavailable as exc:
            log.info("SCT %s: no model fix (%s)", item.get("issue_code"), exc)
            return None, "none"

    # An unrecognised mode must not silently behave like "off" -- that would
    # look like the model declined rather than never being asked.
    log.warning("unknown model_mode %r; nothing drafted for SCT %s",
                model_mode, item.get("issue_code"))
    return None, "none"
