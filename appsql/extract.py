"""Pulling SQL statements out of MyBatis mapper XML, without lying about them.

A naive extractor strips the dynamic tags and returns the text between them.
That produces SQL which parses, is not the SQL the application sends, and is
therefore worse than no extraction at all -- a converter fed it would rewrite a
statement that does not exist and a validator would compare against a query the
application never runs.

So three things are preserved rather than flattened:

  1. **Dynamic tags stay.** `<if>`, `<where>`, `<foreach>`, `<choose>` and
     `<trim>` are kept in the extracted text, marked as structure. A statement
     carrying them is recorded as *dynamic*, and the validate gate treats it
     differently: there is no single SQL string to EXPLAIN, there is a family of
     them.

  2. **`${}` and `#{}` are distinguished.** `#{}` is a bind parameter and `${}`
     is string interpolation. Conflating them loses the difference between a
     parameter and an injection site, and rewriting one into the other breaks
     the query at runtime rather than at conversion.

  3. **A renderable form is produced separately.** For the validate gate to run
     anything at all, the statement needs one concrete rendering: dynamic
     branches taken as false, `#{}` replaced by a typed placeholder. That
     rendering is recorded as `probe_sql` and clearly labelled as one branch of
     several -- never as "the statement".

Nothing here converts. Extraction is a reading of the source, and it is
deterministic: no model, no network, no database.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# The statement elements MyBatis executes. `<sql>` is a reusable fragment and is
# collected separately: it is not a statement, but an `<include>` refers to it
# and a statement that includes one is incomplete without it.
STATEMENT_TAGS = ("select", "insert", "update", "delete")
FRAGMENT_TAG = "sql"

# Tags that make a statement dynamic -- its final text depends on the parameters
# supplied at runtime, so there is no single SQL string to validate.
DYNAMIC_TAGS = ("if", "where", "set", "foreach", "choose", "when", "otherwise",
                "trim", "bind")

BIND_PARAM = re.compile(r"#\{\s*([^}]+?)\s*\}")
INTERPOLATION = re.compile(r"\$\{\s*(\w+)\s*\}")


def _text_with_structure(el: ET.Element) -> tuple[str, list[str]]:
    """The element's SQL text with dynamic tags kept inline as markers.

    Returns the text and the list of dynamic tag names encountered. The markers
    are written as `<if test="...">` exactly as they appear, because a person
    reading a converted statement has to see the branch that produced it.
    """
    parts: list[str] = []
    dynamic: list[str] = []

    def walk(node: ET.Element, depth: int = 0) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            tag = child.tag
            if tag in DYNAMIC_TAGS:
                dynamic.append(tag)
                attrs = " ".join(f'{k}="{v}"' for k, v in child.attrib.items())
                parts.append(f"<{tag}{' ' + attrs if attrs else ''}>")
                walk(child, depth + 1)
                parts.append(f"</{tag}>")
            elif tag == "include":
                # An include's target lives elsewhere in the file. Recorded as a
                # marker so a statement is never silently missing a fragment.
                parts.append(f"<include refid=\"{child.attrib.get('refid', '')}\"/>")
            elif tag == "selectKey":
                # Not SQL in the statement's own right: it is a separate
                # statement MyBatis runs before or after this one. Kept inline
                # because the construct catalogue needs to see it.
                attrs = " ".join(f'{k}="{v}"' for k, v in child.attrib.items())
                parts.append(f"<selectKey {attrs}>")
                walk(child, depth + 1)
                parts.append("</selectKey>")
            else:
                walk(child, depth + 1)
            if child.tail:
                parts.append(child.tail)

    walk(el)
    text = "".join(parts)
    # Collapse the whitespace XML indentation introduces, without joining lines
    # that were deliberately separate -- a MINUS on its own line must stay so,
    # because the catalogue's pattern anchors to it.
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln), dynamic


def _probe(sql: str) -> str:
    """One concrete rendering of a dynamic statement, for the validate gate.

    Dynamic branches are dropped (the `<if>` false case), `#{}` becomes a typed
    NULL cast so the statement parses, and `${}` is left as a literal marker
    because there is no honest substitution for a column name we do not know.

    This is one branch of several and is labelled as such everywhere it is
    used. It exists so a gate can EXPLAIN something, not so anyone can claim
    the statement was validated in full.
    """
    out = sql
    # Drop dynamic blocks and their contents. `<where>` and `<set>` contribute a
    # keyword even when every branch is false, so they are replaced rather than
    # removed -- except that an empty WHERE is invalid, so it goes too.
    out = re.sub(r"<(if|foreach|choose|when|otherwise|bind)\b[^>]*>.*?</\1>", "",
                 out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"</?(where|set|trim)\b[^>]*>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"<selectKey\b[^>]*>.*?</selectKey>", "", out,
                 flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<include\b[^>]*/?>", "", out, flags=re.IGNORECASE)
    # A bind parameter becomes NULL: the statement's shape is what is being
    # checked, not its result on any particular argument.
    out = BIND_PARAM.sub("NULL", out)
    # Interpolation is left visible. A gate must report this as unrenderable
    # rather than guess a column name.
    lines = [ln.strip() for ln in out.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def from_file(path: Path) -> dict:
    """Every statement in one mapper file, with its fragments."""
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    namespace = root.attrib.get("namespace") or path.stem

    fragments: dict[str, str] = {}
    for el in root:
        if el.tag == FRAGMENT_TAG and el.attrib.get("id"):
            fragments[el.attrib["id"]] = _text_with_structure(el)[0]

    statements: list[dict] = []
    for el in root:
        if el.tag not in STATEMENT_TAGS:
            continue
        sid = el.attrib.get("id")
        if not sid:
            continue
        sql, dynamic = _text_with_structure(el)
        binds = sorted(set(BIND_PARAM.findall(sql)))
        interps = sorted(set(INTERPOLATION.findall(sql)))
        includes = sorted(set(re.findall(r'<include\s+refid="([^"]+)"', sql)))
        probe = _probe(sql)
        statements.append({
            "file": path.name,
            "namespace": namespace,
            "statement_id": sid,
            "kind": el.tag,
            "sql": sql,
            "sql_sha256": _sha(sql),
            # A statement whose text depends on runtime parameters. There is no
            # single SQL string to validate, and a gate must say so.
            "dynamic": bool(dynamic),
            "dynamic_tags": sorted(set(dynamic)),
            "bind_params": binds,
            # Interpolation sites: an injection surface, and a place a
            # converter must not "fix" into a bind parameter.
            "interpolations": interps,
            "includes": includes,
            "unresolved_includes": [r for r in includes if r not in fragments],
            "probe_sql": probe,
            # Whether the probe can be sent to an engine at all. A statement
            # carrying interpolation cannot: a column name is missing, and
            # inventing one would validate a query nobody runs.
            "probe_renderable": not interps and not includes,
            "probe_unrenderable_because": (
                "carries ${} interpolation, so a column or table name is unknown"
                if interps else
                "includes a fragment that was not resolved" if includes else None),
        })

    return {"file": str(path), "namespace": namespace,
            "fragments": fragments, "statements": statements}


def from_dir(root: Path) -> dict:
    """Every mapper under a directory, in a stable order."""
    files = sorted(root.glob("**/*.xml"))
    mappers = [from_file(f) for f in files]
    statements = [s for m in mappers for s in m["statements"]]
    return {
        "root": str(root),
        "file_count": len(files),
        "files": [m["file"] for m in mappers],
        "statement_count": len(statements),
        "statements": statements,
        "dynamic_count": sum(1 for s in statements if s["dynamic"]),
        "unrenderable_count": sum(1 for s in statements if not s["probe_renderable"]),
    }
