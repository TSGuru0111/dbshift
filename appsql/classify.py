"""Which tier each application SQL statement goes to, decided by rule.

Same arrangement as `convert/classify.py`, over a different catalogue: the
worst construct found decides the route. One manual construct and the statement
is a person's; one model construct and it needs judgement; otherwise the
deterministic rules take it.

Two differences from the PL/SQL classifier, both forced by the material:

  1. **A statement is never "excluded because it is broken."** Stored code has
     compilation errors recorded in the data dictionary, so 4b can exclude an
     object Oracle itself rejects. Application SQL has no such signal -- it is
     text in a file, and whether Oracle accepts it is unknown until it runs.
     A statement that will not parse is a finding, not an exclusion.

  2. **A statement may be unroutable for a MyBatis reason rather than an Oracle
     one.** `${}` interpolation and an unresolved `<include>` both mean the
     text is incomplete, which is a different problem from an Oracle construct
     having no PostgreSQL equivalent. Both route to a person, and the reason
     says which.

Comments and string literals are blanked before scanning, for the same reason
4b does it: `'SYSDATE'` inside a message is not a use of SYSDATE. The
difference here is that an Oracle **hint** is a comment and must survive the
blanking, because a dropped hint is the finding.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CATALOGUE_PATH = Path(__file__).resolve().parent / "constructs.json"
_CATALOGUE: list[dict] | None = None

RULE, MODEL, MANUAL, INCOMPLETE = "RULE", "MODEL", "MANUAL", "INCOMPLETE"

# A hint is a comment. Blanking comments would erase the one construct whose
# whole point is that PostgreSQL silently ignores it, so hints are lifted out
# before comments are stripped and spliced back as an opaque token.
HINT = re.compile(r"/\*\+[\s\S]*?\*/")
_HINT_TOKEN = "/*+HINT*/"


def catalogue() -> list[dict]:
    global _CATALOGUE
    if _CATALOGUE is None:
        _CATALOGUE = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))["constructs"]
    return _CATALOGUE


def by_id() -> dict[str, dict]:
    return {c["id"]: c for c in catalogue()}


def code_only(text: str) -> str:
    """SQL with comments removed and string literals blanked.

    Oracle hints are preserved as an opaque token: they are syntactically
    comments, and the catalogue has to still see one.

    MyBatis dynamic tags are left in place. A `test` attribute is Java, not
    SQL, and could in principle mention a construct name -- but stripping the
    tags is worse, because it loses the structure a converter must preserve.
    Attribute values are blanked instead, which keeps the tag and drops its
    expression.
    """
    hints = HINT.findall(text)
    out = HINT.sub(_HINT_TOKEN, text)

    # Blank the values of dynamic-tag attributes: `test="customerId != null"`
    # is Java and must not be scanned as SQL.
    out = re.sub(r'(<\w+\s[^>]*?=")[^"]*(")', r"\1\2", out)

    res, i, n = [], 0, len(out)
    while i < n:
        ch = out[i]
        if ch == "-" and out.startswith("--", i):
            j = out.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and out.startswith("/*", i):
            if out.startswith(_HINT_TOKEN, i):
                res.append(_HINT_TOKEN)
                i += len(_HINT_TOKEN)
                continue
            j = out.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if out[j] == "'":
                    if j + 1 < n and out[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            # A literal's *content* is blanked so a message mentioning SYSDATE
            # is not counted as a use of it -- but a non-empty literal must not
            # become an empty one. `'Active'` blanked to `''` made
            # EMPTY_STRING_NULL fire on every statement carrying any string at
            # all, which inflated the manual tier from 4 statements to 9 and
            # would have flattered this phase's own numbers. The placeholder
            # keeps the two cases distinct: `''` in the source stays `''`,
            # anything else becomes `'x'`.
            res.append("''" if j == i + 1 else "'x'")
            i = j + 1
            continue
        res.append(ch)
        i += 1

    text_out = "".join(res)
    # Splice the real hints back so a reader of the scanned text sees what was
    # actually there, not a token.
    for h in hints:
        text_out = text_out.replace(_HINT_TOKEN, h, 1)
    return text_out


def scan(sql: str) -> list[dict]:
    """Every catalogued construct present in this statement, with a count."""
    code = code_only(sql)
    found = []
    for c in catalogue():
        hits = re.findall(c["pattern"], code, re.IGNORECASE | re.MULTILINE)
        if hits:
            found.append({
                "id": c["id"], "name": c["name"], "tier": c["tier"],
                "postgres": c["postgres"], "marker": c.get("marker"),
                "residue": c.get("residue", True), "note": c["note"],
                "count": len(hits),
            })
    return found


def route(stmt: dict) -> dict:
    """Where one extracted statement goes, and why."""
    constructs = scan(stmt["sql"])
    base = {"constructs": constructs}

    # Incompleteness is decided before Oracle constructs are weighed: a
    # statement whose text is not fully known cannot be rewritten correctly
    # whatever its constructs are.
    if stmt.get("unresolved_includes"):
        return {**base, "route": INCOMPLETE,
                "reason": "includes a fragment that is not in this mapper: "
                          + ", ".join(stmt["unresolved_includes"])}

    manual = [c for c in constructs if c["tier"] == "manual"]
    if manual:
        return {**base, "route": MANUAL,
                "reason": "no correct automatic rewrite: "
                          + ", ".join(f"{c['name']} ({c['postgres']})" for c in manual)}

    model = [c for c in constructs if c["tier"] == "model"]
    if model:
        return {**base, "route": MODEL,
                "reason": "needs judgement: "
                          + ", ".join(f"{c['name']} -> {c['postgres']}" for c in model)}

    if not constructs:
        return {**base, "route": RULE,
                "reason": "no Oracle-specific construct found; the statement may port unchanged"}

    return {**base, "route": RULE,
            "reason": "every construct is in the deterministic tier"}
