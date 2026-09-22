"""Static stand-ins for model output, used while Bedrock invoke is blocked.

A static output is written by hand, per estate, against evidence checked at
authoring time. It occupies the seam the reasoning tier will fill, so the
pipeline runs end to end today -- but it is never presented as model output.
Everything served from here carries source="static_fixture" and model_id=None,
and the console labels it that way. If a client asks "did an AI write this?",
the screen has to give the true answer.

Two guards keep it honest:

* **Matching is exact** -- rule, owner and object. A finding with no authored
  entry gets nothing, so a collaborator's estate is never served another
  estate's answers.
* **Each entry records the finding detail it was written against.** If today's
  finding says something different, the entry is marked stale and the finding
  goes to a human, rather than receiving an answer to a question that has
  since changed.

A static SQL fix gets no shortcut: it passes the same five gates as a template,
including apply-and-rollback on the rehearsal copy.
"""

from __future__ import annotations

import json
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
SOURCE = "static_fixture"


def _key(rule_id: str | None, owner: str | None, object_name: str | None) -> tuple:
    return (rule_id or "", (owner or "").upper(), (object_name or "").upper())


def load_all(static_dir: Path = STATIC_DIR) -> list[dict]:
    """Every authored entry, each stamped with where it came from."""
    entries = []
    for path in sorted(static_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for i, raw in enumerate(doc["outputs"]):
            entry = dict(raw)
            entry["fixture_id"] = f"{path.stem}#{i}"
            entry["authored_on"] = doc.get("authored_on")
            entries.append(entry)
    return entries


def lookup(finding: dict, static_dir: Path = STATIC_DIR) -> dict | None:
    """The authored entry for exactly this finding, or None.

    Read from disk on every call. A long-running console must not keep serving
    an entry that has since been corrected, and at tens of findings a run the
    cost is nothing.
    """
    wanted = _key(finding["rule_id"], finding.get("owner"), finding.get("object_name"))
    for entry in load_all(static_dir):
        if _key(entry["rule_id"], entry.get("owner"), entry.get("object_name")) == wanted:
            entry["stale"] = (entry.get("written_against") or "") != (finding.get("detail") or "")
            return entry
    return None
