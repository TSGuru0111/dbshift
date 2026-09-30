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
# One catalogue per source engine. MySQL's SQL/PSM shares almost no constructs
# with PL/SQL, and a shared file would let a MySQL routine be scanned for NVL.
CATALOGUE_PATHS = {
    "ORACLE": CATALOGUE_PATH,
    "MYSQL": Path(__file__).resolve().parent / "constructs_mysql.json",
}
_CATALOGUES: dict[str, list[dict]] = {}

RULE, MODEL, MANUAL, ABSORBED, EXCLUDED = "RULE", "MODEL", "MANUAL", "ABSORBED", "EXCLUDED_BROKEN_ON_SOURCE"


def _engine(source_engine: str | None) -> str:
    return "MYSQL" if (source_engine or "").upper() == "MYSQL" else "ORACLE"


def catalogue(source_engine: str | None = None) -> list[dict]:
    engine = _engine(source_engine)
    if engine not in _CATALOGUES:
        _CATALOGUES[engine] = json.loads(
            CATALOGUE_PATHS[engine].read_text(encoding="utf-8"))["constructs"]
    return _CATALOGUES[engine]


def by_id(source_engine: str | None = None) -> dict[str, dict]:
    return {c["id"]: c for c in catalogue(source_engine)}


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


def code_only_mysql(text: str) -> str:
    """MySQL source with comments removed and string literals blanked.

    MySQL differs from PL/SQL in three ways that matter here: `#` starts a
    comment, a double-quoted token is a STRING (not an identifier) under the
    default sql_mode, and a backslash escapes the next character inside a
    string. Backtick-quoted identifiers are code and are kept.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "#" or (ch == "-" and text.startswith("-- ", i)):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch in ("'", '"'):
            q, j = ch, i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == q:
                    if j + 1 < n and text[j + 1] == q:
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


def scan(text: str, source_engine: str | None = None) -> list[dict]:
    """Every catalogued construct present in this text, with a count."""
    code = code_only_mysql(text) if _engine(source_engine) == "MYSQL" else code_only(text)
    found = []
    for c in catalogue(source_engine):
        hits = re.findall(c["pattern"], code, re.IGNORECASE | re.MULTILINE)
        if hits:
            found.append(
                {"id": c["id"], "name": c["name"], "tier": c["tier"], "postgres": c["postgres"],
                 "note": c["note"], "count": len(hits)}
            )
    return found


def route(obj: dict, inv: dict) -> dict:
    key = (obj["owner"], obj["object_type"], obj["object_name"])
    engine = _engine(inv.get("source_engine"))
    constructs = scan(obj["source_text"], engine)
    base = {"constructs": constructs}
    if engine == "MYSQL":
        return _route_mysql(obj, inv, key, constructs, base)

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


def _route_mysql(obj: dict, inv: dict, key: tuple, constructs: list[dict], base: dict) -> dict:
    """MySQL has no rule tier: there is no deterministic SQL/PSM -> PL/pgSQL
    converter, so a routine goes to the reasoning tier -- and then through the
    same five gates as Oracle code -- unless a construct makes it a person's."""
    if obj["object_type"] not in ("FUNCTION", "PROCEDURE", "TRIGGER"):
        return {**base, "route": MANUAL,
                "reason": f"{obj['object_type']} objects are not converted by this tool"}
    manual = [c for c in constructs if c["tier"] == "manual"]
    if manual:
        return {**base, "route": MANUAL,
                "reason": "no PostgreSQL equivalent: " + ", ".join(f"{c['name']} ({c['postgres']})" for c in manual)}
    named = ", ".join(c["name"] for c in constructs if c["tier"] == "model") or "no catalogued construct"
    return {**base, "route": MODEL,
            "reason": ("no deterministic MySQL converter exists, so the reasoning tier drafts "
                       f"it and the five gates decide; constructs: {named}")}
