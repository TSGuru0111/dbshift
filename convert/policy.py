"""What a converted object is allowed to be.

A conversion -- from a rule, a static fixture or a model -- may only CREATE the
object it was asked to convert. It may not drop, grant, alter, escalate or
reach outside its own definition. The checks run on the statement headers with
the dollar-quoted bodies masked, so an INSERT inside a procedure body is fine
and a top-level DELETE is not. Deterministic; no model consulted."""

from __future__ import annotations

import re

I = re.IGNORECASE

ALLOWED = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(FUNCTION|PROCEDURE|TRIGGER)\b|^\s*CREATE\s+(TYPE|DOMAIN)\b", I
)

PROHIBITED = [
    (r"\bDROP\b", "drops an object"),
    (r"\bTRUNCATE\b", "truncates data"),
    (r"\bDELETE\s+FROM\b", "deletes rows outside a routine body"),
    (r"\bGRANT\b", "alters privileges"),
    (r"\bREVOKE\b", "alters privileges"),
    (r"\bALTER\b", "alters an existing object"),
    (r"\bCREATE\s+(ROLE|USER|EXTENSION|SCHEMA|DATABASE|TABLE|INDEX|VIEW|SEQUENCE)\b", "creates something other than the converted object"),
    (r"\bCOPY\b", "moves data"),
    (r"\bSECURITY\s+DEFINER\b", "escalates privileges (SECURITY DEFINER)"),
    (r"\bSET\s+(ROLE|SESSION\s+AUTHORIZATION)\b", "changes identity"),
    (r"^\s*DO\b", "runs an anonymous block"),
    (r"\bLANGUAGE\s+(?!plpgsql\b|sql\b)\w+", "uses a language other than plpgsql or sql"),
]


def split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons, respecting strings, dollar quotes and comments."""
    out, cur, i, n = [], [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            cur.append(sql[i:j])
            i = j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            cur.append(sql[i:j])
            i = j
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            cur.append(sql[i:j + 1])
            i = j + 1
            continue
        if ch == "$":
            m = re.match(r"\$([A-Za-z_]\w*)?\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j < 0 else j + len(tag)
                cur.append(sql[i:j])
                i = j
                continue
        if ch == ";":
            stmt = "".join(cur).strip()
            if stmt:
                out.append(stmt)
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def mask_bodies(stmt: str) -> str:
    """Replace dollar-quoted bodies with $$...$$ so only the header is inspected."""
    return re.sub(r"\$([A-Za-z_]\w*)?\$.*?\$\1\$", "$$...$$", stmt, flags=re.DOTALL)


def created_names(sql: str) -> list[str]:
    names = []
    for stmt in split_statements(sql):
        head = mask_bodies(stmt)
        m = re.match(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE|TRIGGER|TYPE|DOMAIN)\s+(\"?[\w$#]+\"?(?:\.\"?[\w$#]+\"?)?)", head, I)
        if m:
            names.append(m.group(1).split(".")[-1].strip('"').lower())
    return names


def check(sql: str) -> list[str]:
    """Every reason this conversion may not exist. Empty means allowed."""
    stmts = split_statements(sql)
    if not stmts:
        return ["no statements"]
    violations = []
    for i, stmt in enumerate(stmts, 1):
        head = " ".join(mask_bodies(stmt).split())
        if not ALLOWED.match(head):
            violations.append(f"statement {i} is not a CREATE of a function, procedure, trigger, type or domain")
        for pattern, reason in PROHIBITED:
            if re.search(pattern, head, I):
                violations.append(f"statement {i} {reason}")
    return violations
