"""Which engine to recommend, proposed. `validate_target.py` still decides.

`propose.py` sizes the box -- edition, instance class, storage -- and a model
has been allowed to do that since the phase was built, bounded by a rules
engine that recomputes every value. This module extends the same arrangement to
the larger question: **Oracle or PostgreSQL**.

Until now that recommendation was a fixed cascade inside `target.py`, which
could answer only in canned strings. The actual question it is trying to answer
is not arithmetic:

    No blocking feature. 46% of the stored code converts by rule, 6 objects
    need a model-drafted rewrite reviewed by a person, 1 must be written from
    nothing, and the prize is ending the Oracle licence.

    Is that worth it?

That is a weighing, with commercial context, and it is the kind of judgement the
project already lets a model make in exactly one place under exactly one
condition: it proposes, the rules decide, and the disagreement is recorded. So
the same shape is used here rather than a second one.

**Two things the model is never allowed to do.**

First, it is never asked about a blocked path. `target.choose()` refuses a
blocked target outright and `_recommend()` short-circuits before the model is
consulted, because a blocker is a fact about the estate -- RAC, Label Security,
Database Vault, Advanced Queuing -- and a model given room to weigh one would
eventually talk itself past it. The model only ever sees a choice that is
genuinely open.

Second, it cannot claim more confidence than the evidence carries. The
stored-code evidence arrives in one of three tiers and the prompt is told which:

    classified  `convert/project.py` routed the objects, nothing was converted
                or compiled. A projection.
    measured    Phase 4b converted by rule; no PostgreSQL target was configured,
                so nothing is proven to compile.
    proven      Phase 4b converted and compiled every object on a real
                PostgreSQL inside a rolled-back transaction.

A recommendation built on `classified` evidence must say so. The heuristic below
enforces that by construction; the model is instructed to and then checked by
`validate_target.py`, which is where the enforcement actually lives.

Anything the model gets structurally wrong -- an engine that is not one of the
two, a confidence phrase that is not in the vocabulary, a recommendation for a
blocked path -- falls back to the heuristic rather than failing the phase, and
the fallback is visible in `source`.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("sizing.propose_target")

ORACLE = "ORACLE"
POSTGRESQL = "POSTGRESQL"

# The confidence vocabulary. Fixed, because a recommendation a client reads
# twice must use the same words for the same state, and because
# `validate_target.py` checks the phrase against the evidence tier -- which it
# can only do if the set is closed.
HIGH = "high"
JUDGEMENT = "a judgement, not a verdict"
PROJECTION = "a projection, not a measurement"
INSUFFICIENT = "insufficient evidence"
CONFIDENCE = (HIGH, JUDGEMENT, PROJECTION, INSUFFICIENT)

# Which confidence phrases an evidence tier may carry. `classified` evidence can
# never reach `high`: routing predicts that the deterministic rules will cover
# an object, and a prediction is not a compile. This is the rule
# `validate_target.py` enforces against the model's answer.
ALLOWED_BY_BASIS = {
    "classified": (PROJECTION, JUDGEMENT, INSUFFICIENT),
    "measured": (JUDGEMENT, PROJECTION, INSUFFICIENT),
    "proven": (HIGH, JUDGEMENT, INSUFFICIENT),
    None: (INSUFFICIENT,),
}


def _basis(code: dict) -> str | None:
    """The evidence tier behind the stored-code numbers.

    A 4b plan does not carry a `basis` field -- it predates the tiers -- so it
    is inferred: a plan with nothing blocked on the compile gate is proven,
    a plan with blocked objects was never compiled against a target.
    """
    if not code:
        return None
    if code.get("basis"):
        return code["basis"]
    if not code.get("measured"):
        return None
    return "measured" if code.get("blocked") else "proven"


# --------------------------------------------------------------- heuristic


def heuristic_target_proposal(paths: dict, code: dict) -> dict:
    """The deterministic recommendation. Also the fallback for every model error.

    This is `target._recommend()`'s original cascade, with one change: it no
    longer refuses to answer on projected evidence. It recommends, and labels
    the recommendation a projection -- which is strictly more information than
    "insufficient evidence", and does not overclaim, because the label travels
    with the number.
    """
    pg = paths[POSTGRESQL]
    basis = _basis(code)

    if not pg["possible"]:
        names = ", ".join(b["subject"] for b in pg["blockers"])
        return {
            "source": "heuristic",
            "model_id": None,
            "target": ORACLE,
            "confidence": HIGH,
            "evidence_basis": basis,
            "reason": f"PostgreSQL is blocked outright by {names}. "
                      "The homogeneous path carries the estate as it stands.",
        }

    if basis is None:
        return {
            "source": "heuristic",
            "model_id": None,
            "target": None,
            "confidence": INSUFFICIENT,
            "evidence_basis": None,
            "reason": "No PostgreSQL blocker was found, but nothing is known about the "
                      "stored code -- not even how it classifies. Run the classifier "
                      "(`convert.project`) or Phase 4b, then read this again.",
        }

    if basis == "measured" and code.get("blocked"):
        return {
            "source": "heuristic",
            "model_id": None,
            "target": None,
            "confidence": INSUFFICIENT,
            "evidence_basis": basis,
            "reason": f"{code['blocked']} converted object(s) were never compiled, because no "
                      "PostgreSQL target was configured when Phase 4b ran. Compile them "
                      "before treating the heterogeneous path as costed.",
        }

    pct = code.get("pct_automatic")
    handwork = code.get("handwork") or 0
    model_tier = code.get("model_tier")
    manual = code.get("manual")

    # How the remaining work is described depends on what kind it is, because
    # "six drafts to review" and "six rewrites to author" are different costs
    # and a single "handwork" figure hides which one this is.
    if model_tier is not None and manual is not None and handwork:
        shape = []
        if model_tier:
            shape.append(f"{model_tier} need a model-drafted rewrite, reviewed and compiled")
        if manual:
            shape.append(f"{manual} must be written by a person from nothing")
        shape_text = "; ".join(shape)
    else:
        shape_text = f"{handwork} object(s) need a person"

    if handwork:
        confidence = PROJECTION if basis == "classified" else JUDGEMENT
        prefix = ("Projected from construct routing, not measured: " if basis == "classified"
                  else "")
        return {
            "source": "heuristic",
            "model_id": None,
            "target": None,
            "confidence": confidence,
            "evidence_basis": basis,
            "reason": f"{prefix}{pct}% of the stored code converts automatically, but "
                      f"{shape_text}. Whether that is worth the licence saving is a "
                      "commercial decision, not a technical one.",
        }

    if basis == "classified":
        return {
            "source": "heuristic",
            "model_id": None,
            "target": POSTGRESQL,
            "confidence": PROJECTION,
            "evidence_basis": basis,
            "reason": "No blocking feature, and every convertible stored object routes to "
                      "the deterministic rules. Nothing has been converted or compiled yet, "
                      "so this is a projection from construct routing -- Phase 4b confirms "
                      "or corrects it.",
        }

    return {
        "source": "heuristic",
        "model_id": None,
        "target": POSTGRESQL,
        "confidence": HIGH,
        "evidence_basis": basis,
        "reason": "No blocking feature, and every convertible stored object was rewritten "
                  "by rule and compiled on PostgreSQL. The heterogeneous path is open, and "
                  "it ends the Oracle licence.",
    }


# --------------------------------------------------------------- bedrock

PROMPT = """You are advising on an Oracle database migration to AWS. Two targets are \
possible and the client must choose one:

  ORACLE      Amazon RDS for Oracle       (homogeneous; the Oracle licence continues)
  POSTGRESQL  Amazon RDS for PostgreSQL   (heterogeneous; the Oracle licence ends)

Neither target is blocked -- that has already been established, and a blocked \
target would not have reached you. Your job is to weigh the cost of the \
heterogeneous path against what it saves.

EVIDENCE
{evidence}

HOW THE STORED-CODE NUMBERS WERE OBTAINED: {basis_explanation}

Reply with JSON and nothing else:

{{"target": "ORACLE" or "POSTGRESQL" or null,
  "confidence": one of {confidence_options},
  "reason": "two to four sentences a client can act on: what the numbers are, \
what they cost, and what the choice turns on"}}

Rules you must follow:
- `null` for target means the evidence does not favour either path. Use it when \
the decision is genuinely commercial rather than technical -- it is a valid and \
often correct answer, not a failure.
- Choose `confidence` from the list given. It is constrained by how the evidence \
was obtained, and the list you were handed already reflects that constraint.
- Cite only numbers that appear in the evidence. Do not estimate, extrapolate or \
invent a figure.
- Say plainly if the stored-code evidence is a projection rather than a measurement.
- Do not recommend PostgreSQL on the grounds that the remaining work is small if \
the evidence says it is not."""

def _construct_count() -> str:
    """How many constructs the classifier scans, read from the catalogue.

    Hardcoding the number here would let the prompt drift away from the
    catalogue silently, and a prompt that misstates its own evidence is the one
    thing this module exists to prevent.

    An unreadable catalogue degrades to "many" rather than raising: the count is
    context for the prompt, not evidence the recommendation rests on, and losing
    it must not take the whole proposal down. It is logged so the degradation is
    visible rather than silent.
    """
    try:
        from convert import classify
        return str(len(classify.catalogue()))
    except (OSError, ValueError, KeyError, ImportError) as exc:
        log.warning("sizing.propose_target: construct catalogue unreadable (%s); "
                    "the prompt will say 'many' instead of a count", exc)
        return "many"


_BASIS_EXPLANATION = {
    "classified": (
        "Every stored-code object was routed by scanning its source against a "
        "catalogue of {constructs} Oracle constructs. NOTHING WAS CONVERTED OR "
        "COMPILED. These figures are a projection of what conversion would "
        "achieve, not a measurement of what it did achieve."
    ),
    "measured": (
        "Phase 4b converted the stored code with deterministic rules, but no "
        "PostgreSQL target was configured, so no conversion is proven to compile."
    ),
    "proven": (
        "Phase 4b converted every object and created each one on a real "
        "PostgreSQL 16 inside a transaction that was then rolled back. The "
        "conversions are proven to compile."
    ),
}


def bedrock_target_proposal(paths: dict, code: dict, model_id: str = "", client=None) -> dict:
    """Ask the model which engine to recommend. The rules still decide.

    A blocked PostgreSQL path never reaches the model: it is decided by the
    heuristic and returned before the client is constructed, so no tokens are
    spent and no model is given the opportunity to weigh a blocker.
    """
    from bedrock.client import BedrockClient, BedrockError

    pg = paths[POSTGRESQL]
    basis = _basis(code)

    # A blocker is not a judgement call, and neither is a total absence of
    # evidence. Both are answered by rule, before the model exists.
    if not pg["possible"] or basis is None:
        return heuristic_target_proposal(paths, code)

    allowed = ALLOWED_BY_BASIS[basis]
    evidence = {
        "oracle_path": {
            "effort_points": paths[ORACLE]["effort_points"],
            "effort": [{"subject": i["subject"], "detail": i["detail"]}
                       for i in paths[ORACLE]["effort"]],
        },
        "postgresql_path": {
            "effort_points": pg["effort_points"],
            "effort": [{"subject": i["subject"], "detail": i["detail"]}
                       for i in pg["effort"]],
        },
        "stored_code": {k: code.get(k) for k in (
            "basis", "convertible", "ready", "handwork", "model_tier", "manual",
            "blocked", "excluded_broken_on_source", "pct_automatic")},
    }

    client = client or BedrockClient()
    try:
        reply = client.complete(
            "reasoning",
            PROMPT.format(
                evidence=json.dumps(evidence, indent=2),
                basis_explanation=_BASIS_EXPLANATION[basis].format(
                    constructs=_construct_count()),
                confidence_options=json.dumps(list(allowed)),
            ),
            max_tokens=700)
    except BedrockError as exc:
        out = heuristic_target_proposal(paths, code)
        out["source"] = "heuristic_after_model_error"
        out["model_error"] = str(exc)[:200]
        return out

    text = (reply.get("text") or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, KeyError) as exc:
        out = heuristic_target_proposal(paths, code)
        out["source"] = "heuristic_after_unparseable_reply"
        out["model_error"] = f"reply was not JSON: {str(exc)[:80]}"
        return out

    # Structural validation only -- whether the *judgement* is sound is
    # `validate_target.py`'s job. Three things are structural here.
    target = parsed.get("target")
    if target not in (ORACLE, POSTGRESQL, None):
        out = heuristic_target_proposal(paths, code)
        out["source"] = "heuristic_after_unknown_target"
        out["model_error"] = f"proposed {target!r}, which is not a target"
        return out

    # A model recommending a blocked path is a structural error, not a
    # disagreement to record: it was told the path was open and it was.
    if target and not paths[target]["possible"]:
        out = heuristic_target_proposal(paths, code)
        out["source"] = "heuristic_after_blocked_target"
        out["model_error"] = f"proposed {target}, which this estate blocks"
        return out

    confidence = parsed.get("confidence")
    if confidence not in allowed:
        out = heuristic_target_proposal(paths, code)
        out["source"] = "heuristic_after_unallowed_confidence"
        out["model_error"] = (f"proposed confidence {confidence!r}, which {basis} "
                              f"evidence cannot carry")
        return out

    return {
        "source": "bedrock",
        "model_id": reply.get("model_id"),
        "tokens": {"in": reply.get("input_tokens"), "out": reply.get("output_tokens")},
        "target": target,
        "confidence": confidence,
        "evidence_basis": basis,
        "reason": str(parsed.get("reason") or "").strip()[:900],
    }


def propose_target(paths: dict, code: dict, use_bedrock: bool = False,
                   model_id: str | None = None, client=None) -> dict:
    if use_bedrock:
        return bedrock_target_proposal(paths, code, model_id or "", client=client)
    return heuristic_target_proposal(paths, code)
