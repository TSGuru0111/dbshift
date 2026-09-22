"""Which tier each stored-code object goes to, decided by rule.

The catalogue in constructs.json is scanned against the source text. The
worst construct found decides the route: one manual construct and the object
is a person's; one model construct and it is the reasoning tier's; otherwise
the deterministic rules take it. An object that is already broken on the
source is not converted at all -- converting a compile error faithfully
produces a compile error."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .inventory import CODE_TYPES

CATALOGUE_PATH = Path(__file__).resolve().parent / "constructs.json"
_CATALOGUE: list[dict] | None = None

RULE, MODEL, MANUAL, ABSORBED, EXCLUDED = "RULE", "MODEL", "MANUAL", "ABSORBED", "EXCLUDED_BROKEN_ON_SOURCE"


def catalogue() -> list[dict]:
    global _CATALOGUE
    if _CATALOGUE is None:
        _CATALOGUE = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))["constructs"]
    return _CATALOGUE


def by_id() -> dict[str, dict]:
    return {c["id"]: c for c in catalogue()}


def code_only(text: str) -> str:
    """Source with comments removed and string literals blanked, so a construct
    mentioned in a comment or a message is not mistaken for one in use."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append("''")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def scan(text: str) -> list[dict]:
    """Every catalogued construct present in this text, with a count."""
    code = code_only(text)
    found = []
    for c in catalogue():
        hits = re.findall(c["pattern"], code, re.IGNORECASE | re.MULTILINE)
        if hits:
            found.append(
                {"id": c["id"], "name": c["name"], "tier": c["tier"], "postgres": c["postgres"],
                 "note": c["note"], "count": len(hits)}
            )
    return found


def route(obj: dict, inv: dict) -> dict:
    key = (obj["owner"], obj["object_type"], obj["object_name"])
    constructs = scan(obj["source_text"])
    base = {"constructs": constructs}

    errors = inv.get("errors_by_object", {}).get(key)
    if errors:
        return {**base, "route": EXCLUDED,
                "reason": f"compilation errors on the source ({len(errors)}): {errors[0]}"}

    if obj["object_type"] not in CODE_TYPES:
        return {**base, "route": MANUAL,
                "reason": f"{obj['object_type']} objects are not converted by this tool"}

    manual = [c for c in constructs if c["tier"] == "manual"]
    if manual:
        return {**base, "route": MANUAL,
                "reason": "no PostgreSQL equivalent: " + ", ".join(f"{c['name']} ({c['postgres']})" for c in manual)}

    model = [c for c in constructs if c["tier"] == "model"]
    if model:
        return {**base, "route": MODEL,
                "reason": "needs judgement: " + ", ".join(f"{c['name']} -> {c['postgres']}" for c in model)}

    if obj["object_type"] == "PACKAGE":
        return {**base, "route": ABSORBED,
                "reason": "PostgreSQL has no packages; the body's members become functions"}

    return {**base, "route": RULE, "reason": "every construct is in the deterministic tier"}
