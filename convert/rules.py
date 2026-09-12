"""Deterministic PL/SQL -> PL/pgSQL conversion for a bounded subset.

This is the template tier of Phase 4b: where a construct has exactly one
correct translation, a model adds cost and risk but no information. Everything
here is a rewrite with a recorded reason, and anything outside the subset
raises Declined -- a routing decision, never a best effort. A half-converted
object that compiles is worse than one that was refused.

Two semantic traps this tier exists to encode, because a text-level
translation gets them silently wrong:

* `SELECT ... INTO` must become `INTO STRICT`, or NO_DATA_FOUND is never raised
  and every Oracle exception handler becomes dead code.
* A PostgreSQL BEFORE trigger must `RETURN NEW`, or the row change is cancelled
  without an error.

And one it deliberately does not translate: Oracle runs stored code with the
definer's rights by default, PostgreSQL with the caller's. Adding SECURITY
DEFINER widens privileges on the target; that is a person's decision and is
recorded as `not_translated` on every function, so it cannot be forgotten."""

from __future__ import annotations

import re

from . import typemap
from .typemap import Unmappable

I = re.IGNORECASE
S = re.DOTALL
RULE_VERSION = "2026-09-12"

AUTHID_NOTE = (
    "Oracle runs stored code with the definer's rights by default (AUTHID DEFINER); PostgreSQL "
    "runs it with the caller's (SECURITY INVOKER). Not translated: adding SECURITY DEFINER "
    "widens privileges on the target and is a decision for a person, not a conversion."
)


class Declined(Exception):
    """The rules do not cover this object. A routing decision, not a failure."""


def _c(oracle: str, handling: str, postgres: str | None = None, note: str | None = None) -> dict:
    return {"oracle": oracle, "handling": handling, "postgres": postgres, "note": note}


# ---------------------------------------------------------------- text utilities

def _segments(text: str):
    """Yield (is_string, segment) so rewrites never touch a string literal."""
    i, n, start = 0, len(text), 0
    while i < n:
        if text[i] == "'":
            if i > start:
                yield False, text[start:i]
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            yield True, text[i:j + 1]
            i = j + 1
            start = i
            continue
        i += 1
    if start < n:
        yield False, text[start:]


def _sub(pattern: str, repl, text: str, flags=I) -> str:
    return "".join(seg if is_str else re.sub(pattern, repl, seg, flags=flags)
                   for is_str, seg in _segments(text))


def _search(pattern: str, text: str, flags=I) -> bool:
    return any(re.search(pattern, seg, flags) for is_str, seg in _segments(text) if not is_str)


def _take_parens(text: str, pos: int) -> tuple[str, int]:
    """text[pos] must be '('. Return (inner, index after the matching ')')."""
    assert text[pos] == "("
    depth, i, n, in_str = 0, pos, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[pos + 1:i], i + 1
        i += 1
    raise Declined("unbalanced parentheses")


def _split_top(text: str, sep: str = ",") -> list[str]:
    parts, depth, cur, in_str = [], 0, [], False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            cur.append(ch)
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=S)
    return re.sub(r"--[^\n]*", "", text)


def _rewrite_calls(text: str, name: str, build) -> str:
    """Replace every `name(args)` with build(args), matching parentheses."""
    out, i = [], 0
    pat = re.compile(r"\b" + re.escape(name) + r"\s*\(", I)
    while True:
        m = pat.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        inner, end = _take_parens(text, m.end() - 1)
        out.append(build(inner))
        i = end
    return "".join(out)


# ---------------------------------------------------------------- code rewrites

def convert_code(text: str, *, kind: str) -> tuple[str, list[dict]]:
    """Rewrite one executable/declaration section. kind: function|procedure|trigger."""
    cs: list[dict] = []
    t = text

    if _search(r"\bNVL\s*\(", t):
        t = _sub(r"\bNVL\s*\(", "COALESCE(", t)
        cs.append(_c("NVL", "translated", "COALESCE()"))
    if _search(r"\bSYSDATE\b", t):
        t = _sub(r"\bSYSDATE\b", "date_trunc('second', LOCALTIMESTAMP)", t)
        cs.append(_c("SYSDATE", "translated", "date_trunc('second', LOCALTIMESTAMP)",
                     "Oracle DATE has second precision; truncating keeps comparisons identical"))
    if _search(r"\bSYSTIMESTAMP\b", t):
        t = _sub(r"\bSYSTIMESTAMP\b", "CURRENT_TIMESTAMP", t)
        cs.append(_c("SYSTIMESTAMP", "translated", "CURRENT_TIMESTAMP"))
    if _search(r":(NEW|OLD)\b", t):
        t = _sub(r":(NEW|OLD)\b", lambda m: m.group(1).upper(), t)
        cs.append(_c("BIND_NEW_OLD", "translated", "NEW / OLD"))

    select_into = r"(\bSELECT\b(?:(?!\bINSERT\b|;)[\s\S])*?\bINTO\s+)(?!STRICT\b)"
    if _search(select_into, t):
        t = _sub(select_into, r"\1STRICT ", t)
        cs.append(_c("SELECT_INTO", "translated", "SELECT ... INTO STRICT",
                     "STRICT is what raises NO_DATA_FOUND and TOO_MANY_ROWS, as Oracle does"))

    if _search(r"\bRAISE_APPLICATION_ERROR\s*\(", t):
        rae = r"\bRAISE_APPLICATION_ERROR\s*\(\s*(-?\d+)\s*,\s*('(?:[^']|'')*')\s*(?:,\s*(TRUE|FALSE))?\s*\)"
        if not re.search(rae, t, I):
            raise Declined("RAISE_APPLICATION_ERROR with a non-literal message")
        t = re.sub(rae, lambda m: f"RAISE EXCEPTION {m.group(2)} USING ERRCODE = 'P0001', "
                                  f"DETAIL = 'ORA{m.group(1)}'", t, flags=I)
        cs.append(_c("RAISE_APPLICATION_ERROR", "translated", "RAISE EXCEPTION ... USING ERRCODE",
                     "the Oracle error number is kept in DETAIL"))

    if _search(r"\bDBMS_OUTPUT\.PUT_LINE\s*\(", t):
        t = _rewrite_calls(t, "DBMS_OUTPUT.PUT_LINE", lambda a: f"RAISE NOTICE '%', {a.strip()}")
        cs.append(_c("DBMS_OUTPUT", "translated", "RAISE NOTICE"))
    if _search(r"\bDUP_VAL_ON_INDEX\b", t):
        t = _sub(r"\bDUP_VAL_ON_INDEX\b", "unique_violation", t)
        cs.append(_c("DUP_VAL_ON_INDEX", "translated", "unique_violation"))

    if _search(r"\b(COMMIT|ROLLBACK)\s*;", t):
        if kind != "procedure":
            raise Declined(f"COMMIT/ROLLBACK inside a {kind}: PostgreSQL functions cannot control transactions")
        cs.append(_c("COMMIT", "translated", "COMMIT / ROLLBACK",
                     "allowed in a PostgreSQL procedure only when CALLed outside a transaction block"))

    for cid, pat, pg in (
        ("PERCENT_TYPE", r"%TYPE\b", "%TYPE"),
        ("PERCENT_ROWTYPE", r"%ROWTYPE\b", "%ROWTYPE"),
        ("NO_DATA_FOUND", r"\bNO_DATA_FOUND\b", "NO_DATA_FOUND"),
        ("TOO_MANY_ROWS", r"\bTOO_MANY_ROWS\b", "TOO_MANY_ROWS"),
        ("WHEN_OTHERS", r"\bWHEN\s+OTHERS\b", "WHEN OTHERS"),
        ("RETURNING_INTO", r"\bRETURNING\b[^;]*\bINTO\b", "RETURNING ... INTO"),
    ):
        if _search(pat, t):
            cs.append(_c(cid, "translated", pg, "same syntax in PL/pgSQL"))
    return t, cs


def convert_declarations(decl: str, *, owner: str, known_types: set[str] | None) -> tuple[str, list[dict]]:
    """Variable declarations only. Anything richer is declined."""
    cs: list[dict] = []
    body = _strip_comments(decl).strip()
    if not body:
        return "", cs
    if re.search(r"^\s*(CURSOR|TYPE|SUBTYPE|PROCEDURE|FUNCTION|PRAGMA|EXCEPTION)\b", body, I | re.MULTILINE):
        raise Declined("declaration section holds more than variables (cursor, type, pragma or nested subprogram)")
    lines, changed = [], False
    for item in _split_top(body, ";"):
        item = item.strip()
        if not item:
            continue
        m = re.match(r"^(\w+)\s+(CONSTANT\s+)?(.+?)(?:\s*(?::=|\bDEFAULT\b)\s*(.+))?$", item, I | S)
        if not m:
            raise Declined(f"declaration not understood: {item[:60]}")
        name, const, otype, default = m.group(1), m.group(2), m.group(3).strip(), m.group(4)
        try:
            pg, _ = typemap.map_type(otype, owner=owner, known_types=known_types)
        except Unmappable as exc:
            raise Declined(str(exc)) from exc
        if pg.upper() != otype.upper():
            changed = True
        dflt = ""
        if default:
            dval, dcs = convert_code(default.strip(), kind="function")
            cs.extend(dcs)
            dflt = f" := {dval}"
        lines.append(f"  {name.lower()} {'CONSTANT ' if const else ''}{pg}{dflt};")
    if changed:
        cs.append(_c("ORACLE_DECLARED_TYPES", "translated", "mapped by convert/types.json"))
    for cid, pat, pg in (("PERCENT_TYPE", r"%TYPE\b", "%TYPE"), ("PERCENT_ROWTYPE", r"%ROWTYPE\b", "%ROWTYPE")):
        if re.search(pat, body, I):
            cs.append(_c(cid, "translated", pg, "same syntax; resolved against the shadow schema at creation"))
    return "\n".join(lines), cs


def _params(paramstr: str | None, *, owner: str, known_types: set[str] | None) -> tuple[str, list[dict], bool]:
    if not paramstr or not paramstr.strip():
        return "", [], False
    parts, cs, has_out = [], [], False
    for p in _split_top(_strip_comments(paramstr)):
        p = " ".join(p.split())
        m = re.match(r"^(\w+)\s+(IN\s+OUT|IN|OUT)?\s*(NOCOPY\s+)?(.+?)(?:\s+(?:DEFAULT|:=)\s+(.+))?$", p, I)
        if not m:
            raise Declined(f"parameter not understood: {p[:60]}")
        name, mode, otype, default = m.group(1), (m.group(2) or "IN").upper(), m.group(4), m.group(5)
        mode = {"IN": "IN", "OUT": "OUT", "IN OUT": "INOUT"}[" ".join(mode.split())]
        has_out = has_out or mode != "IN"
        try:
            pg, _ = typemap.map_type(otype, owner=owner, known_types=known_types)
        except Unmappable as exc:
            raise Declined(str(exc)) from exc
        if pg.upper() != otype.upper():
            cs.append(_c("ORACLE_DECLARED_TYPES", "translated", "mapped by convert/types.json"))
        dflt = ""
        if default:
            dval, dcs = convert_code(default, kind="function")
            cs.extend(dcs)
            dflt = f" DEFAULT {dval}"
        parts.append(f"{mode} {name.lower()} {pg}{dflt}")
    return ", ".join(parts), cs, has_out


def _dedupe(cs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in cs:
        if c["oracle"] in seen:
            continue
        seen.add(c["oracle"])
        out.append(c)
    return out


def _split_body(rest: str, expected_name: str | None) -> tuple[str, str]:
    """rest is everything after IS/AS. Returns (declarations, body between BEGIN and the last END)."""
    m = re.search(r"\bBEGIN\b", rest, I)
    if not m:
        raise Declined("no BEGIN found")
    decl = rest[:m.start()]
    tail = rest[m.end():]
    end = None
    for end in re.finditer(r"\bEND\b\s*(\"?[\w$#]+\"?)?\s*;?\s*$", tail, I):
        pass
    if end is None:
        raise Declined("no terminating END found")
    if end.group(1) and expected_name and end.group(1).strip('"').upper() != expected_name.upper():
        raise Declined(f"END {end.group(1)} does not close {expected_name}")
    return decl, tail[:end.start()]


def _q(owner: str, name: str) -> str:
    return f"{owner.lower()}.{name.lower()}"


def _indent(block: str) -> str:
    lines = [ln.rstrip() for ln in block.strip("\n").splitlines()]
    return "\n".join(("  " + ln.lstrip()) if ln.strip() else "" for ln in lines)


# ---------------------------------------------------------------- subprograms

def convert_subprogram(text: str, *, owner: str, known_types: set[str] | None,
                       name_override: str | None = None) -> dict:
    """FUNCTION or PROCEDURE text (a standalone object or a package member)."""
    m = re.match(r"^\s*(FUNCTION|PROCEDURE)\s+(\"?)([\w$#]+)\2\s*", text, I)
    if not m:
        raise Declined("not a FUNCTION or PROCEDURE header")
    kind, name = m.group(1).upper(), m.group(3)
    if m.group(2):
        raise Declined("quoted subprogram name needs a naming decision")
    pos = m.end()
    params = None
    if pos < len(text) and text[pos] == "(":
        params, pos = _take_parens(text, pos)
    rest = text[pos:]

    returns = None
    if kind == "FUNCTION":
        m2 = re.match(r"\s*RETURN\s+(.+?)\s+((?:(?:DETERMINISTIC|PIPELINED|PARALLEL_ENABLE|RESULT_CACHE|AUTHID\s+\w+)\s+)*)(IS|AS)\b", rest, I | S)
        if not m2:
            raise Declined("function header not understood")
        returns, modifiers = m2.group(1).strip(), m2.group(2).upper()
    else:
        m2 = re.match(r"\s*((?:(?:AUTHID\s+\w+)\s+)*)(IS|AS)\b", rest, I | S)
        if not m2:
            raise Declined("procedure header not understood")
        modifiers = m2.group(1).upper()
    if "PIPELINED" in modifiers or "RESULT_CACHE" in modifiers:
        raise Declined("PIPELINED / RESULT_CACHE functions are model tier")
    authid = re.search(r"AUTHID\s+(\w+)", modifiers)
    rest = rest[m2.end():]

    decl, body = _split_body(rest, name)
    if re.search(r"\b(PROCEDURE|FUNCTION)\s+\w+", _strip_comments(decl), I):
        raise Declined("nested subprogram: PL/pgSQL has none, so the split into separate functions is model tier")

    pg_params, cs, has_out = _params(params, owner=owner, known_types=known_types)
    if kind == "FUNCTION" and has_out:
        raise Declined("function with OUT parameters: PostgreSQL returns them as a record, which changes the call site")
    pg_decl, dcs = convert_declarations(decl, owner=owner, known_types=known_types)
    cs.extend(dcs)
    pg_body, bcs = convert_code(body, kind=kind.lower())
    cs.extend(bcs)

    pg_name = _q(owner, name_override or name)
    lines = []
    if kind == "FUNCTION":
        try:
            pg_ret, _ = typemap.map_type(returns, owner=owner, known_types=known_types)
        except Unmappable as exc:
            raise Declined(str(exc)) from exc
        lines.append(f"CREATE OR REPLACE FUNCTION {pg_name}({pg_params})")
        lines.append(f"RETURNS {pg_ret}")
    else:
        lines.append(f"CREATE OR REPLACE PROCEDURE {pg_name}({pg_params})")
    lines.append("LANGUAGE plpgsql")
    lines.append("AS $$")
    if pg_decl:
        lines.append("DECLARE")
        lines.append(pg_decl)
    lines.append("BEGIN")
    lines.append(_indent(pg_body))
    lines.append("END;")
    lines.append("$$;")

    if authid and authid.group(1).upper() == "CURRENT_USER":
        cs.append(_c("AUTHID_DEFINER", "translated", "SECURITY INVOKER (PostgreSQL default)",
                     "the source already ran with the caller's rights"))
    else:
        cs.append(_c("AUTHID_DEFINER", "not_translated", None, AUTHID_NOTE))

    return {
        "statements": ["\n".join(lines)],
        "creates": [pg_name],
        "constructs": _dedupe(cs),
        "explain": f"{kind.title()} {name} becomes {pg_name} in PL/pgSQL; every parameter and declared "
                   f"type mapped by convert/types.json.",
        "caveat": None,
    }


# ---------------------------------------------------------------- triggers

def convert_trigger(text: str, *, owner: str, known_types: set[str] | None) -> dict:
    m = re.match(r"^\s*TRIGGER\s+(\"?)([\w$#]+)\1\s+(BEFORE|AFTER|INSTEAD\s+OF)\s+(.+?)\s+ON\s+(\"?)([\w$#.]+)\5\s*",
                 text, I | S)
    if not m:
        raise Declined("trigger header not understood (compound, DDL and system triggers are model tier)")
    if m.group(1) or m.group(5):
        raise Declined("quoted trigger or table name needs a naming decision")
    name, timing, events_str, table = m.group(2), " ".join(m.group(3).upper().split()), m.group(4), m.group(6)
    if timing == "INSTEAD OF":
        raise Declined("INSTEAD OF trigger: view semantics need a person")

    events, has_delete = [], False
    for ev in re.split(r"\bOR\b", events_str, flags=I):
        ev = " ".join(ev.split())
        em = re.fullmatch(r"(INSERT|DELETE|UPDATE)(?:\s+OF\s+([\w$#\s,]+))?", ev, I)
        if not em:
            raise Declined(f"trigger event not understood: {ev}")
        kind = em.group(1).upper()
        has_delete = has_delete or kind == "DELETE"
        cols = em.group(2)
        events.append(kind + (f" OF {', '.join(c.strip().lower() for c in cols.split(','))}" if cols else ""))

    rest = text[m.end():]
    rm = re.match(r"REFERENCING\s+(.*?)(?=\bFOR\s+EACH\b|\bWHEN\b|\bDECLARE\b|\bBEGIN\b)", rest, I | S)
    if rm:
        if not re.fullmatch(r"(?:(?:NEW|OLD)\s+AS\s+(?:NEW|OLD)\s*)+", " ".join(rm.group(1).split()) + " ", I):
            raise Declined("REFERENCING clause renames NEW/OLD")
        rest = rest[rm.end():]
    row_level = bool(re.match(r"\s*FOR\s+EACH\s+ROW\b", rest, I))
    if row_level:
        rest = re.sub(r"^\s*FOR\s+EACH\s+ROW\b", "", rest, flags=I)
    when = None
    wm = re.match(r"\s*WHEN\s*", rest, I)
    if wm and rest[wm.end():wm.end() + 1] == "(":
        when, endpos = _take_parens(rest, wm.end())
        rest = rest[endpos:]
    rest = re.sub(r"^\s*DECLARE\b", "", rest, flags=I)

    decl, body = _split_body(rest, name)
    if re.search(r"\bEXCEPTION\b", _strip_comments(body), I):
        raise Declined("exception section in a trigger body: every handler needs its own RETURN")
    if not row_level and _search(r":(NEW|OLD)\b", body):
        raise Declined("statement-level trigger references :NEW/:OLD")
    if timing == "AFTER" and re.search(r":NEW\.\w+\s*:=", body, I):
        raise Declined("AFTER trigger assigns to :NEW, which PostgreSQL ignores")

    pg_decl, cs = convert_declarations(decl, owner=owner, known_types=known_types)
    pg_body, bcs = convert_code(body, kind="trigger")
    cs.extend(bcs)
    pg_when = None
    if when:
        pg_when, wcs = convert_code(when, kind="trigger")
        cs.extend(wcs)

    if row_level:
        if has_delete and len(events) > 1:
            ret = "IF TG_OP = 'DELETE' THEN\n    RETURN OLD;\n  END IF;\n  RETURN NEW;"
        elif has_delete:
            ret = "RETURN OLD;"
        else:
            ret = "RETURN NEW;"
        cs.append(_c("TRIGGER_ROW", "translated", "trigger function RETURNS trigger + CREATE TRIGGER",
                     "the RETURN is added: without it a BEFORE trigger cancels the row change silently"))
    else:
        ret = "RETURN NULL;"
        cs.append(_c("TRIGGER_STATEMENT", "translated", "FOR EACH STATEMENT"))

    fn = _q(owner, f"{name}_fn")
    tbl = table.lower() if "." in table else _q(owner, table)
    lines = [f"CREATE OR REPLACE FUNCTION {fn}()", "RETURNS trigger", "LANGUAGE plpgsql", "AS $$"]
    if pg_decl:
        lines += ["DECLARE", pg_decl]
    lines += ["BEGIN", _indent(pg_body), "  " + ret, "END;", "$$;"]
    stmt1 = "\n".join(lines)
    stmt2 = (f"CREATE TRIGGER {name.lower()}\n{timing} {' OR '.join(events)} ON {tbl}\n"
             f"FOR EACH {'ROW' if row_level else 'STATEMENT'}\n"
             + (f"WHEN ({pg_when.strip()})\n" if pg_when else "")
             + f"EXECUTE FUNCTION {fn}();")
    return {
        "statements": [stmt1, stmt2],
        "creates": [fn, name.lower()],
        "constructs": _dedupe(cs),
        "explain": f"Trigger {name} on {table} becomes a trigger function {fn} and a CREATE TRIGGER binding it.",
        "caveat": "Fires under the same events and timing; the RETURN is added because PostgreSQL requires it.",
    }


# ---------------------------------------------------------------- types

def convert_type(text: str, *, owner: str, known_types: set[str] | None) -> dict:
    src = _strip_comments(text).strip().rstrip("/").strip()
    om = re.match(r"^\s*TYPE\s+(\"?)([\w$#]+)\1\s+(?:IS|AS)\s+OBJECT\s*\(", src, I | S)
    if om:
        if om.group(1):
            raise Declined("quoted type name needs a naming decision")
        name = om.group(2)
        inner, end = _take_parens(src, om.end() - 1)
        trailer = src[end:].strip().rstrip(";").strip().upper()
        if "NOT FINAL" in trailer or "NOT INSTANTIABLE" in trailer:
            raise Declined("type inheritance or abstract type: composite types cannot model it")
        attrs, cs, notes = [], [], []
        for a in _split_top(inner):
            a = " ".join(a.split())
            if re.match(r"^(MEMBER|MAP|ORDER|CONSTRUCTOR|STATIC)\b", a, I):
                raise Declined("type with methods needs a decision on where the methods go")
            am = re.match(r"^(\"?)([\w$#]+)\1\s+(.+)$", a, I)
            if not am:
                raise Declined(f"attribute not understood: {a[:60]}")
            if am.group(1):
                raise Declined(f"quoted attribute name {am.group(2)} needs a naming decision")
            try:
                pg, note = typemap.map_type(am.group(3), owner=owner, known_types=known_types)
            except Unmappable as exc:
                raise Declined(str(exc)) from exc
            if note:
                notes.append(f"{am.group(2)}: {note}")
            attrs.append(f"  {am.group(2).lower()} {pg}")
        cs.append(_c("OBJECT_TYPE", "translated", "CREATE TYPE ... AS (composite)"))
        cs.append(_c("ORACLE_DECLARED_TYPES", "translated", "mapped by convert/types.json"))
        pg_name = _q(owner, name)
        return {
            "statements": [f"CREATE TYPE {pg_name} AS (\n" + ",\n".join(attrs) + "\n);"],
            "creates": [pg_name],
            "constructs": cs,
            "explain": f"Object type {name} becomes composite type {pg_name} with the same attributes.",
            "caveat": "; ".join(notes) or None,
        }

    vm = re.match(r"^\s*TYPE\s+(\"?)([\w$#]+)\1\s+(?:IS|AS)\s+(?:VARRAY|VARYING\s+ARRAY)\s*\(\s*(\d+)\s*\)\s+OF\s+(.+?)\s*(NOT\s+NULL)?\s*;?\s*$",
                  src, I | S)
    nm = re.match(r"^\s*TYPE\s+(\"?)([\w$#]+)\1\s+(?:IS|AS)\s+TABLE\s+OF\s+(.+?)\s*(NOT\s+NULL)?\s*;?\s*$", src, I | S)
    if vm or nm:
        m2 = vm or nm
        if m2.group(1):
            raise Declined("quoted type name needs a naming decision")
        name = m2.group(2)
        limit = int(vm.group(3)) if vm else None
        elem = (vm.group(4) if vm else nm.group(3)).strip()
        if re.search(r"\bINDEX\s+BY\b", src, I):
            raise Declined("associative array")
        try:
            pg_elem, note = typemap.map_type(elem, owner=owner, known_types=known_types)
        except Unmappable as exc:
            raise Declined(str(exc)) from exc
        pg_name = _q(owner, name)
        if limit is not None:
            stmt = (f"CREATE DOMAIN {pg_name} AS {pg_elem}[]\n"
                    f"  CONSTRAINT {name.lower()}_max_{limit} CHECK (cardinality(VALUE) <= {limit});")
            cs = [_c("VARRAY", "translated", "DOMAIN over an array with a cardinality CHECK",
                     f"the VARRAY limit of {limit} is enforced by the domain")]
            explain = f"VARRAY({limit}) OF {elem} becomes domain {pg_name} over {pg_elem}[] with the limit as a CHECK."
        else:
            stmt = f"CREATE DOMAIN {pg_name} AS {pg_elem}[];"
            cs = [_c("NESTED_TABLE", "translated", "DOMAIN over an array")]
            explain = f"TABLE OF {elem} becomes domain {pg_name} over {pg_elem}[]."
        if pg_elem.upper() != elem.upper():
            cs.append(_c("ORACLE_DECLARED_TYPES", "translated", "mapped by convert/types.json"))
        return {"statements": [stmt], "creates": [pg_name], "constructs": cs, "explain": explain, "caveat": note}

    raise Declined("type declaration not understood")


# ---------------------------------------------------------------- packages

def package_members(spec_text: str) -> list[tuple[str, str]]:
    """(kind, name) for every subprogram a package specification declares."""
    src = _strip_comments(spec_text)
    return [(m.group(1).upper(), m.group(2).upper()) for m in re.finditer(r"\b(PROCEDURE|FUNCTION)\s+(\w+)", src, I)]


def check_package_spec(spec_text: str) -> None:
    src = _strip_comments(spec_text)
    m = re.match(r"^\s*PACKAGE\s+(\"?)([\w$#]+)\1\s+(IS|AS)\b(.*)\bEND\b", src, I | S)
    if not m:
        raise Declined("package specification not understood")
    inner = m.group(4)
    for item in _split_top(inner, ";"):
        item = " ".join(item.split())
        if not item or re.match(r"^(PROCEDURE|FUNCTION|PRAGMA\s+RESTRICT_REFERENCES)\b", item, I):
            continue
        raise Declined(f"package-level declaration has no PostgreSQL equivalent: {item[:60]}")


def convert_package_body(text: str, *, owner: str, known_types: set[str] | None,
                         spec_text: str | None = None) -> dict:
    src = text
    hm = re.match(r"^\s*PACKAGE\s+BODY\s+(\"?)([\w$#]+)\1\s+(IS|AS)\b", src, I)
    if not hm:
        raise Declined("package body header not understood")
    if hm.group(1):
        raise Declined("quoted package name needs a naming decision")
    pkg = hm.group(2)
    rest = src[hm.end():]
    em = None
    for em in re.finditer(r"\bEND\s+(\"?)" + re.escape(pkg) + r"\1\s*;?\s*$", rest, I):
        pass
    if em is None:
        raise Declined(f"package body does not end with END {pkg}")
    inner = rest[:em.start()]

    members, pos, statements, creates, cs, names = [], 0, [], [], [], []
    stripped_inner = _strip_comments(inner)
    while True:
        mm = re.search(r"\b(PROCEDURE|FUNCTION)\s+(\"?)([\w$#]+)\2", inner[pos:], I)
        if not mm:
            trailing = _strip_comments(inner[pos:]).strip()
            if trailing:
                raise Declined(f"package-level code has no PostgreSQL equivalent: {trailing[:60]}")
            break
        gap = _strip_comments(inner[pos:pos + mm.start()]).strip()
        if gap:
            raise Declined(f"package-level declaration (state) has no PostgreSQL equivalent: {gap[:60]}")
        mname = mm.group(3)
        endm = re.search(r"\bEND\s+(\"?)" + re.escape(mname) + r"\1\s*;", inner[pos + mm.start():], I)
        if not endm:
            raise Declined(f"member {mname} does not close with END {mname}; unnamed ENDs are model tier")
        member_text = inner[pos + mm.start(): pos + mm.start() + endm.end()]
        conv = convert_subprogram(member_text, owner=owner, known_types=known_types,
                                  name_override=f"{pkg}${mname}")
        statements += conv["statements"]
        creates += conv["creates"]
        cs += conv["constructs"]
        names.append((mm.group(1).upper(), mname.upper()))
        pos = pos + mm.start() + endm.end()

    if not names:
        raise Declined("package body declares no members")
    if spec_text is not None:
        check_package_spec(spec_text)
        declared = set(package_members(spec_text))
        missing = declared - set(names)
        if missing:
            raise Declined("specification declares members the body does not define: "
                           + ", ".join(f"{k} {n}" for k, n in sorted(missing)))
        private = [n for n in names if n not in declared]
    else:
        private = []
    cs.append(_c("PACKAGE_BODY", "translated", "one function or procedure per member, named package$member",
                 "PostgreSQL has no packages; members are flattened into the schema"))
    caveat = None
    if private:
        caveat = ("package-private in Oracle, schema-visible in PostgreSQL: "
                  + ", ".join(n for _, n in private))
    return {
        "statements": statements,
        "creates": creates,
        "constructs": _dedupe(cs),
        "explain": f"Package {pkg} is flattened into {len(names)} routine(s): "
                   + ", ".join(f"{pkg.lower()}${n.lower()}" for _, n in names) + ".",
        "caveat": caveat,
    }


# ---------------------------------------------------------------- entry point

def expected_names(obj: dict, spec_text: str | None = None) -> set[str]:
    """Every name a faithful conversion of this object may create (unqualified, lower)."""
    name = obj["object_name"].lower()
    t = obj["object_type"]
    if t == "TRIGGER":
        return {name, f"{name}_fn"}
    if t == "PACKAGE BODY":
        text = _strip_comments(obj["source_text"])
        return {f"{name}${m.group(2).lower()}" for m in re.finditer(r"\b(PROCEDURE|FUNCTION)\s+(\w+)", text, I)}
    return {name}


def referenced_tables(text: str, tables: set[str]) -> set[str]:
    """Tables named after FROM/JOIN/UPDATE/INTO/ON in the source, limited to known tables."""
    found = set()
    for m in re.finditer(r"\b(?:FROM|JOIN|UPDATE|INTO|ON)\s+(\"?[\w$#]+\"?(?:\.\"?[\w$#]+\"?)?)", _strip_comments(text), I):
        cand = m.group(1).split(".")[-1].strip('"').upper()
        if cand in tables:
            found.add(cand)
    return found


def convert(obj: dict, *, owner: str | None = None, known_types: set[str] | None = None,
            spec_text: str | None = None, trigger_meta: dict | None = None) -> dict:
    owner = owner or obj["owner"]
    t = obj["object_type"]
    text = obj["source_text"]
    if t in ("FUNCTION", "PROCEDURE"):
        conv = convert_subprogram(text, owner=owner, known_types=known_types)
    elif t == "TRIGGER":
        conv = convert_trigger(text, owner=owner, known_types=known_types)
        if trigger_meta and trigger_meta.get("status") and trigger_meta["status"].upper() != "ENABLED":
            conv["caveat"] = (conv.get("caveat") or "") + f" Source trigger status is {trigger_meta['status']}; created enabled here."
    elif t == "TYPE":
        conv = convert_type(text, owner=owner, known_types=known_types)
    elif t == "PACKAGE BODY":
        conv = convert_package_body(text, owner=owner, known_types=known_types, spec_text=spec_text)
    else:
        raise Declined(f"{t} is not converted by the rule tier")
    conv["ddl"] = "\n\n".join(conv["statements"])
    conv["source"] = "rule"
    conv["model_id"] = None
    conv["rule_version"] = RULE_VERSION
    return conv
