"""Deterministic rewrites for MySQL application SQL, and an explicit refusal otherwise.

The same contract as `rules.py`, whose protected-segment machinery is reused so
a rewrite never edits a string literal, a MyBatis tag or a `${}` site. Only
the six `tier=rule` constructs in `constructs_mysql.json` are rewritten here;
every model- and manual-tier construct declines the statement WHOLE, because
half-converting it produces text that looks finished and is not.

The one rewrite worth reading closely is `LIMIT a, b`. MySQL puts the OFFSET
first, so the rewrite is `LIMIT b OFFSET a` -- and a swap done by eye is the
classic pagination bug, because it still runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from .rules import Declined, _c, _search, _sub

_CATALOGUE = json.loads((Path(__file__).resolve().parent / "constructs_mysql.json")
                        .read_text(encoding="utf-8"))["constructs"]


def _r_backtick(sql: str) -> tuple[str, dict | None]:
    """`name` -> name. Phase 4c created every name lower-case and unquoted.

    A reserved word in backticks never reaches here: RESERVED_WORD_OBJECT is
    manual and declines the statement first, because 4c renamed that object.
    """
    if not _search(r"`\w+`", sql):
        return sql, None
    out, _ = _sub(r"`(\w+)`", lambda m: m.group(1).lower(), sql)
    return out, _c("MY_BACKTICK", "translated", "unquoted lower-case name")


def _r_ifnull(sql: str) -> tuple[str, dict | None]:
    if not _search(r"\bIFNULL\s*\(", sql):
        return sql, None
    out, _ = _sub(r"\bIFNULL\s*\(", "COALESCE(", sql)
    return out, _c("MY_IFNULL", "translated", "COALESCE()")


def _r_limit_offset(sql: str) -> tuple[str, dict | None]:
    pattern = r"\bLIMIT\s+(#\{[^}]+\}|\d+)\s*,\s*(#\{[^}]+\}|\d+)"
    if not _search(pattern, sql):
        return sql, None
    out, _ = _sub(pattern, lambda m: f"LIMIT {m.group(2)} OFFSET {m.group(1)}", sql)
    return out, _c("MY_LIMIT_OFFSET", "translated",
                   "LIMIT count OFFSET offset -- MySQL's first argument is the offset")


def _r_curdate(sql: str) -> tuple[str, dict | None]:
    if not _search(r"\bCURDATE\s*\(\s*\)", sql):
        return sql, None
    out, _ = _sub(r"\bCURDATE\s*\(\s*\)", "CURRENT_DATE", sql)
    return out, _c("MY_CURDATE", "translated", "CURRENT_DATE")


def _r_now(sql: str) -> tuple[str, dict | None]:
    if not _search(r"\bNOW\s*\(\s*\)", sql):
        return sql, None
    out, _ = _sub(r"\bNOW\s*\(\s*\)", "LOCALTIMESTAMP", sql)
    return out, _c("MY_NOW", "translated", "LOCALTIMESTAMP -- a zone-less DATETIME, as NOW() is")


def _r_lock_share(sql: str) -> tuple[str, dict | None]:
    if not _search(r"\bLOCK\s+IN\s+SHARE\s+MODE\b", sql):
        return sql, None
    out, _ = _sub(r"\bLOCK\s+IN\s+SHARE\s+MODE\b", "FOR SHARE", sql)
    return out, _c("MY_LOCK_SHARE", "translated", "FOR SHARE")


_RULES = (_r_backtick, _r_ifnull, _r_limit_offset, _r_curdate, _r_now, _r_lock_share)

# Every construct these rules do not rewrite, with the catalogue's own reason.
# Built from the catalogue rather than restated, so a new model- or
# manual-tier row declines automatically instead of being silently ignored.
NOT_COVERED = {c["id"]: c["note"] for c in _CATALOGUE if c["tier"] != "rule"}


def convert(sql: str, constructs: list[dict]) -> dict:
    """Rewrite one MySQL statement by rule, or decline with a reason."""
    blocking = [c for c in constructs if c["id"] in NOT_COVERED]
    if blocking:
        first = blocking[0]
        raise Declined(f"{first['name']}: {NOT_COVERED[first['id']]}")

    out, accounting = sql, []
    for rule in _RULES:
        out, entry = rule(out)
        if entry:
            accounting.append(entry)

    claimed = {e["id"] for e in accounting}
    for con in constructs:
        if con["id"] not in claimed:
            accounting.append(_c(con["id"], "not_translated",
                                 reason="no rule claimed this construct and it is not in "
                                        "the declined list; review the catalogue"))
    if not accounting:
        raise Declined("no catalogued construct found, so there is nothing to rewrite "
                       "and nothing to prove; the statement may port unchanged")
    return {
        "sql": " ".join(out.split()) if "\n" not in out else out.strip(),
        "constructs": accounting,
        "source": "rule",
        "model_id": None,
    }
