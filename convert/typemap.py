"""Oracle -> PostgreSQL type mapping. Data lives in types.json; the only logic
here is the NUMBER precision rule and identifier handling."""

from __future__ import annotations

import json
import re
from pathlib import Path

TYPES_PATH = Path(__file__).resolve().parent / "types.json"
_CFG: dict | None = None

IDENT = r'"?[A-Za-z_][A-Za-z0-9_$#]*"?'


class Unmappable(ValueError):
    """There is no safe mapping. The object goes to a person, not to a guess."""


def config() -> dict:
    global _CFG
    if _CFG is None:
        _CFG = json.loads(TYPES_PATH.read_text(encoding="utf-8"))
    return _CFG


def map_type(
    oracle: str,
    *,
    owner: str | None = None,
    known_types: set[str] | None = None,
) -> tuple[str, str | None]:
    """Return (postgres_type, note). Raises Unmappable rather than guessing."""
    s = " ".join(oracle.strip().split())
    u = s.upper()
    cfg = config()

    for suffix in ("%ROWTYPE", "%TYPE"):
        if u.endswith(suffix):
            return s[: -len(suffix)].lower() + suffix, None

    m = re.fullmatch(r"NUMBER\s*\(\s*(\d+)\s*(?:,\s*(-?\d+))?\s*\)", u)
    if m:
        p, sc = int(m.group(1)), int(m.group(2) or 0)
        if sc < 0:
            raise Unmappable(f"{s}: negative scale has no equivalent")
        if sc == 0:
            for limit, pg in cfg["number_integer_thresholds"]:
                if p < limit:
                    return pg, f"NUMBER({p}) holds at most {p} digits; {pg} is exact and smaller"
            return f"NUMERIC({p})", None
        return f"NUMERIC({p},{sc})", None

    m = re.fullmatch(r"(N?VARCHAR2|VARCHAR)\s*\(\s*(\d+)\s*(CHAR|BYTE)?\s*\)", u)
    if m:
        note = "BYTE length semantics became character semantics" if m.group(3) == "BYTE" else None
        return f"VARCHAR({m.group(2)})", note

    m = re.fullmatch(r"N?CHAR\s*\(\s*(\d+)\s*(CHAR|BYTE)?\s*\)", u)
    if m:
        return f"CHAR({m.group(1)})", None

    if re.fullmatch(r"RAW\s*\(\s*\d+\s*\)", u):
        return "BYTEA", "the RAW length limit is not carried"

    m = re.fullmatch(r"TIMESTAMP\s*(?:\(\s*(\d)\s*\))?(\s+WITH\s+(?:LOCAL\s+)?TIME\s+ZONE)?", u)
    if m:
        prec = f"({m.group(1)})" if m.group(1) else ""
        return (f"TIMESTAMPTZ{prec}" if m.group(2) else f"TIMESTAMP{prec}"), None

    if u in cfg["scalar"]:
        entry = cfg["scalar"][u]
        return entry["postgres"], entry.get("note")
    if u in cfg["unmappable"]:
        raise Unmappable(f"{s}: {cfg['unmappable'][u]}")

    m = re.fullmatch(rf"(?:({IDENT})\.)?({IDENT})", s)
    if m:
        t_owner, t_name = m.group(1), m.group(2)
        if '"' in (t_owner or "") or '"' in t_name:
            raise Unmappable(f"{s}: quoted identifier needs a naming decision")
        if t_owner and t_owner.upper() in cfg["system_owners"]:
            raise Unmappable(f"{s}: type owned by {t_owner.upper()} has no equivalent")
        if known_types is not None and t_name.upper() not in known_types:
            raise Unmappable(f"{s}: not a type discovery knows about")
        qualifier = t_owner or owner
        pg = f"{qualifier.lower()}.{t_name.lower()}" if qualifier else t_name.lower()
        return pg, "user-defined type, converted separately"

    raise Unmappable(f"no mapping for {s}")
