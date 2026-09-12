"""The model seam for PL/SQL conversion.

Two sources fill it, in the same shape the rule tier produces:

* **static_fixture** -- hand-written output in bedrock/static/convert/, keyed
  on owner, object type and name, and written against the source SHA-256. A
  changed source refuses the entry as stale. Never presented as model output:
  source="static_fixture", model_id=None.
* **bedrock** -- the reasoning tier, asked for strict JSON against the schema
  below. Every response passes validate_output() before anything downstream
  sees it, and every call writes one audit row. Not reachable while invoke is
  blocked on this account; the path is exercised with a stub client in the
  self-test so the seam is real, not implied.

Whatever produced a conversion, it passes exactly the same gates the rule tier
does. A model-authored conversion gets no shortcut."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import classify, policy

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "bedrock" / "static" / "convert"
STATIC_SOURCE = "static_fixture"
AUDIT_PATH = Path(__file__).resolve().parent / "output" / "agent_decisions.jsonl"


class ModelOutputInvalid(ValueError):
    """The model's answer did not match the contract. It is not used."""


# ---------------------------------------------------------------- static fixtures

def _key(owner, object_type, object_name) -> tuple:
    return ((owner or "").upper(), (object_type or "").upper(), (object_name or "").upper())


def load_all(static_dir: Path = STATIC_DIR) -> list[dict]:
    entries = []
    if not static_dir.exists():
        return entries
    for path in sorted(static_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for i, raw in enumerate(doc.get("outputs", [])):
            entry = dict(raw)
            entry["fixture_id"] = f"convert/{path.stem}#{i}"
            entry["authored_on"] = doc.get("authored_on")
            entries.append(entry)
    return entries


def lookup(obj: dict, static_dir: Path = STATIC_DIR) -> dict | None:
    wanted = _key(obj["owner"], obj["object_type"], obj["object_name"])
    for entry in load_all(static_dir):
        if _key(entry.get("owner"), entry.get("object_type"), entry.get("object_name")) == wanted:
            entry["stale"] = (entry.get("written_against") or "") != (obj.get("source_sha256") or "")
            return entry
    return None


def static_convert(obj: dict, static_dir: Path = STATIC_DIR) -> tuple[dict | None, str]:
    entry = lookup(obj, static_dir)
    if entry is None:
        return None, "no_static_output"
    if entry["stale"]:
        return None, (f"static_output_stale: {entry['fixture_id']} was written against a different "
                      "source text (SHA-256 differs)")
    statements = entry.get("statements") or policy.split_statements(entry.get("ddl") or "")
    conv = {
        "statements": statements,
        "ddl": "\n\n".join(statements),
        "creates": policy.created_names("\n".join(statements)),
        "constructs": entry.get("constructs") or [],
        "explain": entry.get("explain"),
        "caveat": entry.get("caveat"),
        "evidence": entry.get("evidence"),
        "fixture_id": entry["fixture_id"],
        "source": STATIC_SOURCE,
        "model_id": None,
    }
    return conv, STATIC_SOURCE


# ---------------------------------------------------------------- live model

SYSTEM_PROMPT = """You convert one Oracle PL/SQL object to PostgreSQL PL/pgSQL.
Rules that are not negotiable:
- Output ONLY a JSON object matching the schema you are given. No prose, no markdown fences.
- Create only the object(s) that correspond to the source object. Never DROP, GRANT, ALTER, or create tables.
- Never add SECURITY DEFINER. Record Oracle's definer-rights default as not_translated.
- SELECT ... INTO must become SELECT ... INTO STRICT.
- A row-level trigger function must RETURN NEW (or OLD for DELETE).
- Every construct in the list you are given must appear in "constructs" as translated (with the PostgreSQL form) or not_translated (with a reason). Do not silently drop behaviour.
- If you are not confident a construct has a faithful translation, mark it not_translated and explain; do not guess.
"""

OUTPUT_SCHEMA = {
    "statements": ["CREATE ... ;", "..."],
    "constructs": [{"oracle": "<construct id from the list>", "handling": "translated|not_translated",
                    "postgres": "<PostgreSQL form or null>", "note": "<why>"}],
    "explain": "<one paragraph: what the object does and how the conversion preserves it>",
    "caveat": "<what a reviewer must check, or null>",
    "confidence": 0.0,
    "assumptions": ["<anything assumed about the schema or the caller>"],
}


def build_prompt(obj: dict, constructs: list[dict], column_types: dict[str, list[str]]) -> str:
    lines = [
        f"Source object: {obj['owner']}.{obj['object_name']} ({obj['object_type']}), SHA-256 {obj['source_sha256']}",
        "",
        "Constructs detected (ids you must account for):",
    ]
    for c in constructs:
        lines.append(f"- {c['id']}: {c['name']} -> {c['postgres']}. {c['note']}")
    if column_types:
        lines.append("")
        lines.append("Referenced tables and their PostgreSQL column types (already mapped):")
        for tbl, cols in column_types.items():
            lines.append(f"- {tbl}: " + ", ".join(cols))
    lines += [
        "",
        "Target schema name: " + obj["owner"].lower(),
        "Naming: package members become <package>$<member>; a trigger becomes <name>_fn() plus CREATE TRIGGER <name>.",
        "",
        "Return JSON with exactly this shape:",
        json.dumps(OUTPUT_SCHEMA, indent=2),
        "",
        "Oracle source:",
        obj["source_text"],
    ]
    return "\n".join(lines)


def validate_output(text: str, obj: dict, constructs: list[dict]) -> dict:
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelOutputInvalid(f"not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ModelOutputInvalid("top level is not an object")
    stmts = payload.get("statements")
    if not isinstance(stmts, list) or not stmts or not all(isinstance(s, str) and s.strip() for s in stmts):
        raise ModelOutputInvalid("statements must be a non-empty list of strings")
    cs = payload.get("constructs")
    if not isinstance(cs, list):
        raise ModelOutputInvalid("constructs must be a list")
    known = set(classify.by_id())
    for c in cs:
        if not isinstance(c, dict) or c.get("oracle") not in known:
            raise ModelOutputInvalid(f"unknown construct in output: {c!r}")
        if c.get("handling") not in ("translated", "not_translated"):
            raise ModelOutputInvalid(f"construct {c.get('oracle')} has no valid handling")
        if c["handling"] == "not_translated" and not (c.get("note") or "").strip():
            raise ModelOutputInvalid(f"construct {c['oracle']} marked not_translated without a reason")
    for c in constructs:
        if c["tier"] in ("rule", "model") and c["id"] not in {x["oracle"] for x in cs}:
            raise ModelOutputInvalid(f"construct {c['id']} present in the source is not accounted for")
    conf = payload.get("confidence")
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        raise ModelOutputInvalid("confidence must be a number between 0 and 1")
    ddl = "\n\n".join(s.strip() for s in stmts)
    return {
        "statements": [s.strip() for s in stmts],
        "ddl": ddl,
        "creates": policy.created_names(ddl),
        "constructs": cs,
        "explain": payload.get("explain"),
        "caveat": payload.get("caveat"),
        "confidence": conf,
        "assumptions": payload.get("assumptions") or [],
    }


def _audit(row: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def live_convert(obj: dict, constructs: list[dict], column_types: dict, *, client=None,
                 tier: str = "reasoning", audit_path: Path = AUDIT_PATH) -> tuple[dict, str]:
    """Ask the reasoning tier. Raises on transport failure or invalid output.

    `client` is anything with .complete(tier, prompt, system=..., max_tokens=...)
    returning {"text", "model_id", "input_tokens", "output_tokens"} -- the real
    BedrockClient, or a stub in the self-test."""
    if client is None:
        from bedrock.client import BedrockClient
        client = BedrockClient()
    prompt = build_prompt(obj, constructs, column_types)
    input_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    response = client.complete(tier, prompt, system=SYSTEM_PROMPT, max_tokens=4096, temperature=0)
    verdict, error, conv = "accepted", None, None
    try:
        conv = validate_output(response["text"], obj, constructs)
    except ModelOutputInvalid as exc:
        verdict, error = "rejected", str(exc)
    _audit(
        {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "phase": "convert",
            "object": f"{obj['owner']}.{obj['object_name']} ({obj['object_type']})",
            "source_sha256": obj["source_sha256"],
            "model_id": response.get("model_id"),
            "tier": tier,
            "input_sha256": input_hash,
            "input_tokens": response.get("input_tokens"),
            "output_tokens": response.get("output_tokens"),
            "validator_verdict": verdict,
            "validator_error": error,
            "confidence": conv.get("confidence") if conv else None,
        },
        audit_path,
    )
    if conv is None:
        raise ModelOutputInvalid(error or "invalid output")
    conv["source"] = "bedrock"
    conv["model_id"] = response.get("model_id")
    conv["input_tokens"] = response.get("input_tokens")
    conv["output_tokens"] = response.get("output_tokens")
    return conv, "bedrock"
