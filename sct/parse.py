"""Read SCT's own assessment CSV into the shape the console already renders.

Phase 2's console screen has a sortable table, severity chips and row-detail
built for `assess/`'s findings. This maps SCT's action items onto that same
shape so the SCT path reuses the screen instead of growing a second one.

**What is mapped, and what is deliberately not.** SCT grades an action item by
the *work a person must do* -- its complexity buckets -- not by how dangerous the
finding is. `assess/` grades by severity. These are different axes and collapsing
one into the other would misreport both, so a row carries **both** columns:
SCT's own complexity verbatim, and a severity derived from it only for sorting.
The derivation is named and reversible rather than presented as SCT's opinion.

**The column names are read, not assumed.** SCT's CSV header has varied across
versions, so this matches headers case-insensitively against a list of known
aliases and records any column it could not place in `unmapped_columns` rather
than dropping it silently. If SCT emits a header this does not know, the row
still arrives and the record says what was not understood.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

# SCT's four complexity buckets, in the order it reports them, with what each
# actually means for a person planning the work.
COMPLEXITY_ORDER = ["simple", "medium", "complex", "decision"]

COMPLEXITY_MEANING = {
    "simple": "automatic, or a one-line change",
    "medium": "a person edits and reviews it",
    "complex": "a person rewrites it",
    "decision": "not a code change -- someone must decide",
}

# Complexity -> a severity, for sorting the table only. Stated here so it is
# auditable: this is DBShift ordering SCT's buckets, not SCT assigning severity.
COMPLEXITY_TO_SEVERITY = {
    "decision": "CRITICAL",
    "complex": "HIGH",
    "medium": "MEDIUM",
    "simple": "LOW",
}

SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}

# Header aliases, normalised to the console's keys. Matched case-insensitively
# with punctuation and spacing ignored.
#
# **Confirmed against SCT 1.0.677's real output on 2026-09-17**, which corrected
# four guesses that each mattered:
#
#   - `Action item` is SCT's **issue code** (a number like 9994), not a title.
#     It was originally mapped as an unknown column while `issue_code` went
#     unfilled, so every row displayed with a blank code.
#   - `Occurrence` is **one object's tree path**, not a count. SCT emits one row
#     per occurrence, so the count is the number of rows -- reading it as a
#     number gave 0 for every row.
#   - There is no object-name column. The object is the **leaf of that tree
#     path**, and its type is the path segment before it.
#   - `Group` is the human-readable problem statement and `Category` is a slug
#     (`queuing-table`, `scheduler-job`), which is the opposite of the
#     assumption that `Category` was the label to show.
_ALIASES: dict[str, tuple[str, ...]] = {
    # SCT 1.0.677 header      -> console key
    "issue_code": ("actionitem", "issuecode", "code", "actionitemcode", "issueid"),
    "occurrence_path": ("occurrence", "occurrences"),
    "complexity": ("estimatedcomplexity", "complexity", "conversioncomplexity"),
    "category_slug": ("category",),
    "group": ("group", "issuetype"),
    "subject": ("subject",),
    "description": ("description", "issuedescription", "message", "details", "detail"),
    "recommendation": ("recommendedaction", "recommendation", "action", "suggestedaction"),
    "schema": ("schemaname", "schema", "owner"),
    "database": ("databasename", "database"),
    "docs": ("documentationreferences",),
    "line": ("line", "linenumber", "lineno"),
    "position": ("position",),
    "source_platform": ("source",),
    "target_platform": ("target",),
    "server": ("serveripaddressandport",),
    "filtered": ("filtered",),
    # Older/other SCT shapes, kept so a different version still parses.
    "object_name": ("objectname", "object", "name"),
    "object_type": ("objecttype", "objectcategory"),
    "count": ("count", "numberofoccurrences"),
}

# Columns that carry no information for the console table. Recorded as mapped
# rather than listed as "not understood", because an operator reading
# `unmapped_columns` should see genuine gaps, not deliberate omissions.
_IGNORED = {"docs", "position", "filtered", "source_platform", "target_platform", "server"}


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").strip().lower())


def _map_headers(fieldnames: list[str]) -> tuple[dict[str, str], list[str]]:
    """CSV header -> console key. Also returns headers it could not place."""
    lookup: dict[str, str] = {}
    for key, aliases in _ALIASES.items():
        for alias in aliases:
            lookup[alias] = key

    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    for raw in fieldnames or []:
        key = lookup.get(_norm(raw))
        if key and key not in mapping.values():
            mapping[raw] = key
        elif key:
            # A second header mapping to a key already filled. Keep the first
            # and report the duplicate rather than overwriting silently.
            unmapped.append(raw)
        else:
            unmapped.append(raw)
    # Columns deliberately carried but not shown are mapped, so they do not
    # appear as gaps in `unmapped_columns`.
    return mapping, [u for u in unmapped if lookup.get(_norm(u)) not in _IGNORED]


def _complexity(value: str) -> str:
    """SCT's complexity, normalised to one of the four buckets."""
    v = (value or "").strip().lower()
    for bucket in COMPLEXITY_ORDER:
        if bucket in v:
            return bucket
    # SCT has also used numeric weights and "N/A" here.
    if v in {"1", "low"}:
        return "simple"
    if v in {"2"}:
        return "medium"
    if v in {"3", "4", "high"}:
        return "complex"
    return "decision" if v else "simple"


def _int(value: str) -> int:
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return 0


# Oracle's own internal tables, which appear in SCT's report because they live
# in the user's schema but are not part of the application and do not migrate.
# The same patterns `assess/loader.py` filters -- kept in step with that list
# deliberately: a finding the rules engine hides and SCT shows is a discrepancy
# a client will ask about.
INTERNAL_PREFIXES = ("DR$", "AQ$", "MLOG$", "RUPD$", "SYS_IOT")


def is_internal_object(name: str) -> bool:
    """True for an Oracle-generated object that is not the client's to migrate.

    `DR$IX_COMM_NOTES_TEXT$I` is Oracle Text's index storage; `AQ$_..._H` is
    Advanced Queuing's history table. Neither is something a person can act on,
    and on DBMIG_APP **12 of 13 occurrences of SCT 5984 were these** -- so a
    row saying "specify precision on 12 objects" was 12 objects nobody owns.
    """
    n = str(name or "").upper()
    return any(n.startswith(p) for p in INTERNAL_PREFIXES)


def _split_occurrence(path: str) -> tuple[str, str, str]:
    """An object name, its type, and its parent out of SCT's tree path.

    SCT locates an occurrence as a path rather than naming the object:

        Schemas.DBMIG_APP.Queuing.Tables.LOAN_EVENT_QTAB
            -> LOAN_EVENT_QTAB, Tables, ""
        Schemas.DBMIG_APP.Tables.LOAN.Columns.LEGACY_SCORE
            -> LOAN.LEGACY_SCORE, Columns, LOAN

    **The parent matters.** A column occurrence's leaf is a bare column name --
    `STATUS`, `ADDRESS#` -- which is useless on its own: the console showed a
    list of column names with no table, and no reader could act on it. The
    returned name is qualified with its parent where there is one.
    """
    parts = [p for p in str(path or "").split(".") if p]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""

    leaf, kind = parts[-1], parts[-2]
    # `Schemas.<owner>.Tables.<table>.Columns.<column>`: the grandparent is the
    # table, the segment before the type. Guard the index -- a shorter path is
    # a top-level object with no parent.
    parent = parts[-3] if len(parts) >= 3 and parts[-3] not in ("Schemas",) else ""
    if parent and kind.lower() in ("columns", "constraints", "indexes", "triggers",
                                    "partitions", "attributes"):
        return f"{parent}.{leaf}", kind, parent
    return leaf, kind, parent


def parse_csv_text(text: str) -> dict:
    """SCT's assessment CSV as issues plus a by-complexity rollup."""
    # SCT writes UTF-8, sometimes with a BOM.
    if text.startswith("﻿"):
        text = text[1:]

    reader = csv.DictReader(io.StringIO(text))
    mapping, unmapped = _map_headers(list(reader.fieldnames or []))

    # SCT emits **one row per occurrence**, not one per issue with a count. So
    # rows carrying the same action-item code and the same problem are folded
    # together and counted -- the same grouping `assess/engine.group_findings`
    # does, for the same reason: at 90 objects one row per object is readable,
    # at 10,000 it is thousands of rows nobody triages.
    grouped: dict[tuple, dict] = {}
    order: list[tuple] = []

    for raw_row in reader:
        row = {mapping[k]: (v or "").strip() for k, v in raw_row.items() if k in mapping}
        if not any(row.values()):
            continue

        complexity = _complexity(row.get("complexity", ""))
        obj, obj_type, parent = _split_occurrence(row.get("occurrence_path", ""))
        if not obj and row.get("object_name"):
            obj, obj_type, parent = row["object_name"], row.get("object_type", ""), ""

        # Is this occurrence about an Oracle-generated object? Checked on the
        # parent too: a column of DR$..._B is as unactionable as the table.
        internal = is_internal_object(obj.split(".")[0]) or is_internal_object(parent)

        code = row.get("issue_code", "")
        # `Group` is SCT's human-readable problem statement; `Description` is
        # usually the same sentence, and `Category` is a slug. Prefer the one a
        # person can read, and never fall back to the bare code as a title.
        title = row.get("group") or row.get("description") or row.get("category_slug") or code

        key = (code, title, complexity)
        if key not in grouped:
            grouped[key] = {
                "source": "aws-sct",
                "issue_code": code,
                "title": title,
                # The slug, tidied for display: SCT writes `queuing-table`.
                "category": (row.get("category_slug") or "conversion").replace("-", " "),
                # SCT's own grading, verbatim.
                "complexity": complexity,
                "complexity_meaning": COMPLEXITY_MEANING[complexity],
                # Derived for sorting only -- see COMPLEXITY_TO_SEVERITY.
                "severity": COMPLEXITY_TO_SEVERITY.get(complexity, "INFO"),
                "severity_is_derived": True,
                "occurrences": 0,
                "object_name": obj,
                "object_type": obj_type,
                "owner": row.get("schema", ""),
                "recommendation": row.get("recommendation", ""),
                "detail": row.get("description", ""),
                "line": row.get("line", ""),
                # Every object the item was raised against, so a grouped row can
                # still show what it covers.
                "objects": [],
                # Oracle-generated objects (DR$, AQ$ ...) held apart: real, but
                # not the client's to act on.
                "internal_objects": [],
                "internal_occurrences": 0,
            }
            order.append(key)

        g = grouped[key]
        # A row may declare its own count in other SCT shapes; default to 1.
        n = _int(row.get("count", "")) or 1
        g["occurrences"] += n
        if internal:
            # Counted separately, not dropped. SCT genuinely reported it, and
            # an item that is *entirely* Oracle internals is worth seeing as
            # such rather than silently vanishing -- but it must not be mixed
            # into the number a person is asked to act on.
            g["internal_occurrences"] += n
            if obj and obj not in g["internal_objects"]:
                g["internal_objects"].append(obj)
        elif obj and obj not in g["objects"]:
            g["objects"].append(obj)

    issues = [grouped[k] for k in order]
    for i in issues:
        # Occurrences a person can actually act on, which is what the console
        # should count. The raw total stays in `occurrences` so the number can
        # still be reconciled against SCT's own report.
        i["actionable_occurrences"] = i["occurrences"] - i["internal_occurrences"]
        i["all_internal"] = (i["actionable_occurrences"] == 0
                             and i["internal_occurrences"] > 0)

        if i["all_internal"]:
            i["object_name"] = f"{len(i['internal_objects'])} Oracle internal objects"
        elif len(i["objects"]) > 1:
            i["object_name"] = f"{len(i['objects'])} objects"
        elif i["objects"]:
            i["object_name"] = i["objects"][0]

    issues.sort(key=lambda i: (
        SEVERITY_RANK.get(i["severity"], 9), -i["occurrences"], str(i["issue_code"]),
    ))

    by_complexity = {c: 0 for c in COMPLEXITY_ORDER}
    occurrences_by_complexity = {c: 0 for c in COMPLEXITY_ORDER}
    for i in issues:
        by_complexity[i["complexity"]] += 1
        occurrences_by_complexity[i["complexity"]] += i["occurrences"]

    return {
        "issues": issues,
        "action_item_count": len(issues),
        "occurrence_count": sum(i["occurrences"] for i in issues),
        "by_complexity": by_complexity,
        "occurrences_by_complexity": occurrences_by_complexity,
        "complexity_meaning": dict(COMPLEXITY_MEANING),
        # Anything SCT emitted that this parser did not understand. Recorded
        # rather than dropped, so a version change is visible instead of silent.
        "unmapped_columns": unmapped,
        "mapped_columns": sorted(set(mapping.values())),
    }


def parse_csv_file(path: str | Path) -> dict:
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    out = parse_csv_text(text)
    out["csv_file"] = p.name
    return out
